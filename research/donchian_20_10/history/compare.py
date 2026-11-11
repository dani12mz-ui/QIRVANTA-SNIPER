"""Compare the fixed Donchian runs on identical OANDA-derived calendar windows."""

import argparse
from bisect import bisect_right
from datetime import datetime, timezone
from gzip import GzipFile
from hashlib import sha256
from math import isclose
from pathlib import Path
import json
from zipfile import ZipFile

from ..data import read_bars, utc
from ..evaluate import parity
from ..metrics import _closed_stats, _drawdown, _exposure, summarize
from ..replay import BASE, CUTOFF, INITIAL_CAPITAL, SCENARIOS, START, WARMUP, Instrument, run


FRAMES = ("D1", "H4")
DAY_MS = 86_400_000
SYMBOL = Instrument(.001, .01, 1.0)


def load_pair(market_dir, manifest_path):
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    direct_check = manifest.get("same_source_H4_to_D1")
    if direct_check and direct_check["status"] != "CROSS_CHECK_MATCH":
        raise ValueError("Review official-feed H4/D1 discrepancies before a same-quality comparison")
    result = {}
    for frame in FRAMES:
        path = market_dir / f"{frame}.csv"
        if sha256(path.read_bytes()).hexdigest() != manifest["frames"][frame]["sha256"]:
            raise ValueError(f"{frame} OHLC does not match the selected data manifest")
        result[frame] = read_bars(path)
        if len(result[frame]) <= WARMUP:
            raise ValueError(f"{frame} cannot satisfy the fixed warmup")
    return result, sha256(manifest_path.read_bytes()).hexdigest()


def common_window(pair):
    threshold = max(START, *(pair[frame][WARMUP].close_ms for frame in FRAMES))
    daily, hourly = ({b.close_ms for b in pair[frame] if threshold <= b.close_ms <= CUTOFF} for frame in FRAMES)
    shared = daily & hourly
    if len(shared) < 2:
        raise ValueError("No common completed calendar window after both warmups")
    return min(shared), max(shared)


def _month_edges(start_ms, end_ms):
    current = datetime.fromtimestamp(start_ms / 1000, timezone.utc)
    year, month = current.year, current.month
    while True:
        year += month == 12
        month = month % 12 + 1
        edge = int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp() * 1000)
        if edge >= end_ms:
            return
        yield edge


def rolling_windows(shared_marks, calendar_start, end, days=90):
    shared_marks = sorted(set(shared_marks))
    selected = set()
    for nominal_end in (*_month_edges(calendar_start, end), end):
        end_i = bisect_right(shared_marks, nominal_end) - 1
        if end_i < 0:
            continue
        actual_end = shared_marks[end_i]
        start_i = bisect_right(shared_marks, actual_end - days * DAY_MS) - 1
        if start_i >= 0 and shared_marks[start_i] >= calendar_start:
            selected.add((shared_marks[start_i], actual_end))
    return sorted(selected)


def period(result, start, end):
    by_time = {mark.time_ms: mark for mark in result.equity}
    if start not in by_time or end not in by_time:
        raise ValueError("Rolling marks must come from observed bars")
    opening = by_time[start]
    initial = opening.equity_usd
    final = by_time[end].equity_usd
    marks = tuple(m for m in result.equity if start < m.time_ms <= end)
    closed = tuple(t for t in result.trades if start < t.exit_seen_ms <= end)
    financing = by_time[end].financing_usd - opening.financing_usd
    return {
        "first_mark_utc": utc(start), "last_mark_utc": utc(end), "elapsed_days": (end - start) / DAY_MS,
        "mark_to_market_pnl_usd": final - initial, "return_pct": 100 * (final / initial - 1) if initial > 0 else None,
        "financing_charged_usd": financing, **_closed_stats(closed), **_drawdown(start, initial, marks),
        **_exposure(result, start, end, marks),
        "by_direction": {name: _closed_stats([t for t in closed if t.side == side])
                         for name, side in (("BUY", 1), ("SELL", -1))},
    }


def cash_bridge(result, last_bar):
    gross = sum(t.gross_usd for t in result.trades)
    mark = result.equity[-1]
    spread = mark.cumulative_spread_usd
    position = result.open_trade
    if position:
        quantity = position.signal.qty * result.instrument.pointvalue
        gross += position.signal.side * (last_bar.close - position.entry_mid) * quantity
        if not result.native:
            spread += result.costs.spread / 2 * quantity
    net = gross - spread - mark.cumulative_slippage_usd - mark.cumulative_commission_usd - mark.financing_usd
    observed = mark.equity_usd - INITIAL_CAPITAL
    if not isclose(net, observed, rel_tol=0, abs_tol=1e-7):
        raise ValueError(f"Gross-to-net accounting does not reconcile: {net} != {observed}")
    return {
        "gross_price_movement_usd": gross, "spread_including_mark_usd": spread,
        "executed_slippage_usd": mark.cumulative_slippage_usd, "commission_usd": mark.cumulative_commission_usd,
        "financing_usd": mark.financing_usd, "net_mark_to_market_usd": observed,
    }


def compare_pair(pair, instrument=SYMBOL):
    start, end = common_window(pair)
    local = {frame: [bar for bar in pair[frame] if bar.close_ms <= end] for frame in FRAMES}
    comparisons = {}
    scenarios = (("native_cost_model", BASE, True), *((c.name, c, False) for c in SCENARIOS))
    for name, costs, native in scenarios:
        runs = {frame: run(local[frame], instrument, costs, start_ms=start, native=native) for frame in FRAMES}
        shared = {x.time_ms for x in runs["D1"].equity} & {x.time_ms for x in runs["H4"].equity}
        windows = {days: rolling_windows(shared, start, end, days) for days in (90, 365)}
        comparisons[name] = {
            frame: {
                "gross_to_net": cash_bridge(result, local[frame][-1]),
                **summarize(result),
                "common_period": period(result, start, end),
                **{f"rolling_{days}_day_observed_marks": [period(result, lo, hi) for lo, hi in spans]
                   for days, spans in windows.items()},
            }
            for frame, result in runs.items()
        }
    return {
        "first_common_eligible_close_utc": utc(start), "last_common_completed_close_utc": utc(end),
        "window": "Both start flat with USD 100,000 at the common eligibility threshold, retaining pre-window ATR and channels. Compare common_period statistics; full and calendar_years_utc retain the replay's first-bar-open accounting.",
        "rolling": "Monthly endpoints and final cutoff, aligned to shared confirmed marks. Trailing start at the last shared mark at or before 90 or 365 calendar days. Outcomes counted by first recorded close.",
        "scenarios": comparisons,
    }


def exact_2026_bridge(pair, archive_path):
    output = {}
    with ZipFile(archive_path) as archive:
        for frame in FRAMES:
            bars = pair[frame]
            instrument, native_qa = parity(frame, bars, archive.read(f"{frame}-matched.csv"))
            if native_qa["status"] != "MATCH":
                raise ValueError(f"Archived Pine events do not match replay for {frame}")
            native = cash_bridge(run(bars, instrument, BASE, native=True), bars[-1])
            economic = cash_bridge(run(bars, instrument, BASE), bars[-1])
            output[frame] = {
                "native_qa": native_qa,
                "native_costs_same_tape": native, "all_base_costs_same_tape": economic,
                "all_costs_minus_native_usd": economic["net_mark_to_market_usd"] - native["net_mark_to_market_usd"],
                "gross_path_and_size_delta_usd": economic["gross_price_movement_usd"] - native["gross_price_movement_usd"],
            }
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--private-dir", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--native", type=Path, help="Archived TradingView QA ZIP, only for exact 2026 tape")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    pair, manifest_hash = load_pair(args.private_dir, args.manifest)
    report = {"data_manifest_sha256": manifest_hash, **compare_pair(pair)}
    if args.native:
        report["original_frame_specific_2026_windows"] = exact_2026_bridge(pair, args.native)
    raw = (json.dumps(report, indent=2) + "\n").encode("utf-8")
    if args.output.suffix == ".gz":
        with args.output.open("wb") as target, GzipFile(filename="", mode="wb", fileobj=target, mtime=0) as zipped:
            zipped.write(raw)
    else:
        args.output.write_bytes(raw)


if __name__ == "__main__":
    main()
