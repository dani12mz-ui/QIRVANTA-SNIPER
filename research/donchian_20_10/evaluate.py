"""Generate trade ledgers, cost tables, and like-for-like native parity evidence."""

import argparse
from collections import Counter
import csv
from dataclasses import asdict, fields
from hashlib import sha256
from math import floor, isclose
from pathlib import Path
import json
from zipfile import ZipFile

from .data import PROTOCOL, ms, read_bars, utc
from .metrics import summarize
from .replay import BASE, CUTOFF, SCENARIOS, Instrument, Trade, run


KINDS = {"D20_SIGNAL", "D20_FILL", "D20_CHANNEL", "D20_EXIT", "D20_REJECT"}
TOLERANCE = 2e-8


def read_journal(raw):
    reader = csv.DictReader(raw.decode("utf-8-sig").splitlines())
    if reader.fieldnames != ["Date", "Message"]:
        raise ValueError("Expected the TradingView Pine Logs CSV header")
    result = []
    for row in reader:
        tag, sep, body = row["Message"].partition("|")
        if not tag.startswith("D20_") or not sep:
            raise ValueError(f"Unexpected QA record {tag}")
        result.append((tag, json.loads(body)))
    return result


def fingerprint(bars):
    a = b = 0
    for bar in bars:
        cell = bar.time_ms // 1000 + 3 * (bar.close_ms // 1000)
        cell += sum(weight * floor(price * 10000 + 0.5)
                    for weight, price in zip((5, 7, 11, 13), (bar.open, bar.high, bar.low, bar.close)))
        a = (a * 65599 + cell) % 1_000_000_007
        b = (b * 32749 + cell) % 1_000_000_009
    return {"bars": len(bars), "a": a, "b": b}


def one(rows, tag):
    row, = (data for kind, data in rows if kind == tag)
    return row


def near(observed, expected):
    if type(expected) is float:
        return type(observed) in (float, int) and isclose(observed, expected, rel_tol=0, abs_tol=TOLERANCE)
    return observed == expected


def parity(frame, bars, raw):
    rows = read_journal(raw)
    meta, end, native_hash = (one(rows, "D20_" + tag) for tag in ("META", "END", "HASH"))
    instrument = Instrument(meta["mintick"], meta["mincontract"], meta["pointvalue"])
    if (meta["symbol"], meta["timeframe"], meta["timezone"], meta["currency"]) != (
            "OANDA:XAUUSD", {"D1": 1440, "H4": 240}[frame], "America/New_York", "USD"):
        raise ValueError("Unexpected native chart context")
    tape_hash = fingerprint(bars)
    match_input = (tape_hash == native_hash and meta["first_time"] == bars[0].time_ms
                   and meta["first_index"] == 0)
    result = run(bars, instrument, BASE, native=True)
    observed = [(tag, row) for tag, row in rows if tag in KINDS
                and row.get("seen_ms", row.get("time_ms")) <= CUTOFF]
    expected = [(e.tag, e.data) for e in result.events if e.tag in KINDS]
    mismatches = []
    for i, (a, b) in enumerate(zip(observed, expected)):
        if a[0] != b[0]:
            mismatches.append({"row": i, "field": "event", "native": a[0], "python": b[0]})
            continue
        for field, value in b[1].items():
            if field not in a[1] or not near(a[1][field], value):
                mismatches.append({"row": i, "tag": a[0], "id": a[1].get("id"), "field": field,
                                   "native": a[1].get(field), "python": value})
        if a[0] == "D20_EXIT":
            reason = a[1]["reason"]
            exit_id = f"Close entry(s) order {a[1]['id']}" if reason == "CHANNEL" else a[1]["id"] + "|SL"
            if a[1]["exit_id"] != exit_id:
                mismatches.append({"row": i, "tag": a[0], "id": a[1]["id"], "field": "exit_id",
                                   "native": a[1]["exit_id"], "python": exit_id})
    if len(observed) != len(expected):
        mismatches.append({"field": "event_count", "native": len(observed), "python": len(expected)})
    prefixes = sorted({e.data["bar"] + 1 for e in result.events if "bar" in e.data})
    for n in prefixes:
        short = run(bars[:n], instrument, BASE, native=True)
        if short.events != tuple(e for e in result.events if e.data.get("bar", len(bars)) < n):
            raise ValueError(f"Historical event prefix changed on {frame} at {n} bars")
    actual_end_equity = end["equity"]
    can_compare_end = (end["time_close"] == bars[-1].close_ms
                       or result.open_trade is None and result.pending is None
                       and not any(t in KINDS and r.get("seen_ms", r.get("time_ms")) > CUTOFF for t, r in rows))
    if can_compare_end and not near(actual_end_equity, result.equity[-1].equity_usd):
        mismatches.append({"field": "equity", "native": actual_end_equity, "python": result.equity[-1].equity_usd})
    return instrument, {
        "status": "MATCH" if match_input and not mismatches else "MISMATCH",
        "loaded_start_utc": utc(bars[0].time_ms), "score_end_utc": utc(bars[-1].close_ms),
        "native_log_sha256": sha256(raw).hexdigest(), "native_hash": native_hash, "python_hash": tape_hash,
        "matching_fingerprint_and_start": match_input, "confirmed_prefixes_checked": len(prefixes),
        "native_events": dict(Counter(tag for tag, _ in observed)),
        "python_events": dict(Counter(tag for tag, _ in expected)),
        "closed_campaigns": len(result.trades), "open_campaign": result.open_trade.signal.id if result.open_trade else None,
        "same_bar_stops": sum(t.entry_bar == t.exit_bar and t.reason == "SL" for t in result.trades),
        "closed_commission_usd": sum(t.commission_usd for t in result.trades),
        "closed_net_usd": sum(t.net_usd for t in result.trades),
        "native_mark_at_chart_history_end_usd": actual_end_equity,
        "matched_mark_equity_usd": result.equity[-1].equity_usd if can_compare_end else None,
        "first_difference": mismatches[0] if mismatches else None, "differences": mismatches,
    }


def write_csv(path, columns, rows):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def evaluate(private_dir, native_archive, manifest, output):
    registry = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    common_start, common_end = map(ms, registry["evaluation"]["common_control_window_utc"])
    source = json.loads(manifest.read_text(encoding="utf-8"))
    metric_report, trade_rows, breakdown = {}, [], []
    with ZipFile(native_archive) as archive:
        for frame in ("D1", "H4"):
            path = private_dir / f"{frame}.csv"
            if sha256(path.read_bytes()).hexdigest() != source["frames"][frame]["sha256"]:
                raise ValueError(f"{frame} input hash differs from data manifest")
            bars = read_bars(path)
            instrument, native = parity(frame, bars, archive.read(f"{frame}-matched.csv"))
            report = {"native_aligned_2026": native, "scenarios": {}, "common_control_window": {}}
            for costs in SCENARIOS:
                result = run(bars, instrument, costs)
                detail = summarize(result)
                report["scenarios"][costs.name] = detail
                report["common_control_window"][costs.name] = summarize(
                    run(bars, instrument, costs, start_ms=common_start, cutoff_ms=common_end))
                for trade in result.trades:
                    row = asdict(trade)
                    trade_rows.append({"frame": frame, "scenario": costs.name, "direction": "BUY" if trade.side > 0 else "SELL",
                                       "signal_utc": utc(trade.signal_ms), "entry_utc": utc(trade.entry_ms),
                                       "exit_chart_bar_open_utc": utc(trade.exit_ms), "exit_known_utc": utc(trade.exit_seen_ms),
                                       **row})
                mark = detail["full"]
                breakdown.append({"frame": frame, "scenario": costs.name, "closed": len(result.trades),
                                  "open": int(result.open_trade is not None),
                                  "closed_net_usd": sum(t.net_usd for t in result.trades),
                                  "end_mark_pnl_usd": mark["net_mark_to_market_usd"],
                                  **detail["costs_actually_charged_including_open"]})
            metric_report[frame] = report
    output.mkdir(parents=True, exist_ok=True)
    columns = ("frame", "scenario", "direction", "signal_utc", "entry_utc",
               "exit_chart_bar_open_utc", "exit_known_utc", *(f.name for f in fields(Trade)))
    write_csv(output / "trades.csv", columns, trade_rows)
    for frame in ("D1", "H4"):
        write_csv(output / f"costs_{frame}.csv", tuple(breakdown[0]),
                  (row for row in breakdown if row["frame"] == frame))
    payload = {"scope": "OANDA 2026 reconstructed aggregate tape, same native warmup and bar hashes. "
                        "Three re-sized economic scenarios. Full-history native report is a separate, cost-incomplete diagnostic.",
               "control_comparison": "INCONCLUSIVE", "frames": metric_report}
    (output / "results.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--native", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    evaluate(args.private_dir, args.native, args.manifest, args.output)


if __name__ == "__main__":
    main()
