"""Audit native Pine decisions and distinguish emulator fills from quote-side replay."""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
from hashlib import sha256
from itertools import zip_longest
import json
from math import isclose
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from gold_pro.direct_breakout_research import BASE, simulate
from gold_pro.direct_trend_breakout import scan
from gold_pro.precision_entry import assess
from gold_pro.precision_entry_research import precision_portfolio
from research.direct_trend_breakout.shadow_native import event_differences, read_native, utc


PLAN = "PE_PLAN"
SUBMIT = "PE_NATIVE_SUBMIT"
FILL = "PE_NATIVE_FILL"
PART = "PE_NATIVE_PART"
TAG_STAMP = {PLAN: "confirmation_ms", SUBMIT: "confirmation_ms", FILL: "seen_ms", PART: "seen_ms"}
DEFAULT_START = "2026-11-06T03:00:00Z"
DEFAULT_END = "2026-11-11T17:30:00Z"


def _price(a, b):
    return a is not None and b is not None and isclose(a, b, rel_tol=0, abs_tol=2e-8)


def _probe(path):
    shadow = read_native(path)
    records = defaultdict(list)
    market = previous_index = None
    batch, previous = [], []

    def flush():
        nonlocal batch, previous_index, previous
        if market is not None:
            index = market["index"]
            if previous_index == index:
                if previous != batch:
                    raise ValueError(f"{path.name}: inconsistent repeated Precision batch at {index}")
            else:
                for tag, record in batch:
                    records[tag].append(record)
                previous_index, previous = index, batch
        batch = []

    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream, strict=True):
            for line in row["Message"].splitlines():
                tag, _, payload = line.partition("|")
                if tag in ("DT_MARKET", "DT_END"):
                    flush()
                    market = json.loads(payload) if tag == "DT_MARKET" else None
                elif tag in TAG_STAMP:
                    record = json.loads(payload)
                    stamp = record.get(TAG_STAMP[tag])
                    if (market is None or type(stamp) is not int or stamp != market["time_close"]
                            or (tag == PLAN and record.get("bar") != market["index"])):
                        raise ValueError(f"{path.name}: {tag} does not belong to its confirmed market bar")
                    batch.append((tag, record))
    flush()
    return shadow, records


def _expected(item):
    plan, event = item.plan, item.event
    return {
        "plan_id": f"DT|{item.source}|{event['side']}|{item.confirmation_ms}",
        "model": item.source, "side": event["side"], "bar": event["bar"],
        "detection_ms": item.detection_ms, "confirmation_ms": item.confirmation_ms,
        "origin_confirmation_ms": item.origin_confirmation_ms,
        "source_ms": event["source_ms"], "level_known_ms": event["level_known_ms"],
        "next_open_ms": item.next_executable_ms, "expiry_ms": item.expires_ms,
        "level": event["level"], "reference": event["close"],
        "stop": plan.stop if plan is not None else event["close"] - event["side"] * item.stop_risk,
        "targets": plan.targets if plan is not None else (),
        "prior_atr": event["prior_atr"], "net_room_r": item.net_room_r,
        "accepted": item.accepted, "reasons": list(item.reasons),
    }


def _compare_probe(shadow, records):
    events, _ = scan(shadow.bars)
    decisions = assess(shadow.bars, events, BASE)
    dt = event_differences(shadow.events, events, shadow.first_index)
    pe = event_differences(records[PLAN], [_expected(row) for row in decisions], shadow.first_index)
    complete = shadow.first_index == 0 and shadow.end is not None
    return decisions, {
        "status": "MATCH" if complete and decisions and not (dt or pe) else "MISMATCH_OR_INCOMPLETE",
        "file": shadow.path.name, "sha256": shadow.sha256, "bars": len(shadow.bars),
        "first_index": shadow.first_index, "historical_end": shadow.end,
        "first_open_utc": utc(shadow.bars[0]["time"]), "last_close_utc": utc(shadow.bars[-1]["time_close"]),
        "identical_repeated_batches": shadow.repeated_batches,
        "original_shadow": {"native": len(shadow.events), "python": len(events), "first_difference": dt},
        "precision": {"native": len(records[PLAN]), "python": len(decisions), "first_difference": pe,
                      "accepted": sum(row["accepted"] for row in records[PLAN]),
                      "rejected": sum(not row["accepted"] for row in records[PLAN]),
                      "accepted_by_model": dict(Counter(row["model"] for row in records[PLAN] if row["accepted"])),
                      "accepted_by_side": dict(Counter(str(row["side"]) for row in records[PLAN] if row["accepted"]))},
        "strategy_log_rows": {tag: len(records[tag]) for tag in (SUBMIT, FILL, PART)},
    }


def _common(indicator, tester, indicator_rows, tester_rows, decisions):
    opening = max(indicator.bars[0]["time"], tester.bars[0]["time"])
    closing = min(indicator.bars[-1]["time_close"], tester.bars[-1]["time_close"])

    def overlap(probe):
        return [bar for bar in probe.bars if opening <= bar["time"] and bar["time_close"] <= closing]

    def normalized(rows, stamp):
        return [{key: value for key, value in row.items() if key != "bar"}
                for row in rows if opening < row[stamp] <= closing]

    bars = overlap(indicator)
    if not bars or bars != overlap(tester):
        return {"status": "NONIDENTICAL_OVERLAP_OHLC", "common_window_utc": [utc(opening), utc(closing)]}
    warmup_equal = indicator.bars[0]["time"] == tester.bars[0]["time"]
    ind, strat = (normalized(rows[PLAN], "confirmation_ms") for rows in (indicator_rows, tester_rows))
    i_dt, t_dt = (normalized(probe.events, "time_ms") for probe in (indicator, tester))
    plan_pairs = list(zip_longest(ind, strat))
    first_plan_difference = next(((a, b) for a, b in plan_pairs if a != b), None)
    tested = 0
    for length in sorted({row.event["bar"] + 1 for row in decisions if row.confirmation_ms <= closing}):
        if assess(tester.bars[:length]) != tuple(row for row in decisions if row.event["bar"] < length):
            break
        tested += 1
    else:
        length = None
    return {
        "status": "DIFFERENT_WARMUP" if not warmup_equal else
                  "MATCH" if ind == strat and i_dt == t_dt and length is None else "MISMATCH",
        "common_bars": len(bars), "first_open_utc": utc(opening), "last_close_utc": utc(closing),
        "indicator_extra_bars": len(indicator.bars) - len(bars), "tester_extra_bars": len(tester.bars) - len(bars),
        "indicator_warmup_starts_utc": utc(indicator.bars[0]["time"]),
        "tester_warmup_starts_utc": utc(tester.bars[0]["time"]),
        "common_original_shadow_records": [len(i_dt), len(t_dt)],
        "differing_original_shadow_records": sum(a != b for a, b in zip_longest(i_dt, t_dt)),
        "common_precision_records": [len(ind), len(strat)],
        "differing_precision_records": sum(a != b for a, b in plan_pairs),
        "changed_accepted_plan_ids": sorted({a["plan_id"] for a in ind if a["accepted"]}
                                             ^ {b["plan_id"] for b in strat if b["accepted"]}),
        "first_precision_difference": first_plan_difference,
        "python_identical_decision_prefixes": tested, "python_first_failing_prefix": length,
    }


def _exit_volume(parts, prices=False):
    """Native TIME_EXIT can group several one-unit target legs into one trade-list row."""
    result = Counter()
    for part in parts:
        key = (part["exit_ms"], part["reason"])
        if prices:
            key += (round(part["price"], 8),)
        result[key] += abs(part["qty"])
    return result


def _execution(tester, records, decisions, start, end):
    historical_bars = tester.bars[:tester.end["index"] - tester.first_index + 1] if tester.end else tester.bars
    candidates = {item.plan.id: item for item in decisions if item.accepted
                  and start <= item.confirmation_ms <= end and item.plan.bar < len(historical_bars)}
    portfolio = {x.plan.id: x for x in precision_portfolio([x.plan for x in candidates.values()], historical_bars, BASE)}
    native, fills = {}, {}
    parts = defaultdict(list)
    for tag, target in ((SUBMIT, native), (FILL, fills)):
        for row in records[tag]:
            if row["plan_id"] in target:
                raise ValueError(f"{tester.path.name}: duplicate {tag} for {row['plan_id']}")
            target[row["plan_id"]] = row
    for row in records[PART]:
        parts[row["plan_id"]].append(row)
    if native.keys() - candidates.keys() or (fills.keys() | parts.keys()) - native.keys():
        raise ValueError(f"{tester.path.name}: out-of-window or unlinked strategy execution record")

    campaign_rows = []
    for identifier, submit in native.items():
        item, fill, exits = candidates[identifier], fills.get(identifier), parts[identifier]
        if submit["confirmation_ms"] != item.confirmation_ms or submit["side"] != item.plan.side:
            raise ValueError(f"{tester.path.name}: inconsistent {SUBMIT} for {identifier}")
        stamps = {row["entry_ms"] for row in exits}
        if fill:
            stamps.add(fill["entry_ms"])
        if len(stamps) > 1 or any(row["qty"] * item.plan.side <= 0 for row in exits):
            raise ValueError(f"{tester.path.name}: inconsistent entry timing or exit quantity for {identifier}")
        native_entry = next(iter(stamps), None)
        first = item.plan.bar + 1
        next_bar = historical_bars[first] if first < len(historical_bars) else None
        next_open = next_bar["time"] if next_bar else None
        expected_mid_fill = next_bar["open"] + item.plan.side * BASE.slip if next_bar else None
        python = simulate(item.plan, historical_bars, BASE)
        py_entry = historical_bars[python.entry_bar]["time"] if python.entry_bar is not None else None
        py_parts = [{**part, "exit_ms": historical_bars[part["bar"]]["time"], "qty": item.plan.units / 3}
                    for part in python.exits]
        matched_exits = sum((_exit_volume(exits) & _exit_volume(py_parts)).values())
        matched_prices = sum((_exit_volume(exits, prices=True) & _exit_volume(py_parts, prices=True)).values())
        native_units = sum(abs(part["qty"]) for part in exits)
        native_realized = None if not fill else round(sum(
            (item.plan.side * (part["price"] - fill["price"]) * BASE.pointvalue - 2 * BASE.commission)
            * abs(part["qty"]) for part in exits), 9)
        campaign_rows.append({
            "id": identifier, "side": item.direction, "confirmation_utc": utc(item.confirmation_ms),
            "strict_contiguous_next_open_utc": utc(item.next_executable_ms),
            "next_available_open_utc": utc(next_open) if next_open is not None else None,
            "native_entry_utc": utc(native_entry) if native_entry is not None else None,
            "entry_timestamp_source": FILL if fill else PART if stamps else None,
            "native_fill_price": fill["price"] if fill else None,
            "native_matches_next_available_open": native_entry == next_open if stamps else None,
            "native_matches_contiguous_open": native_entry == item.next_executable_ms if stamps else None,
            "native_fill_matches_mid_open_plus_slippage": _price(fill["price"], expected_mid_fill) if fill else None,
            "native_closed_units": native_units,
            "native_closed_parts_realized_usd_recomputed_without_spread": native_realized,
            "native_exit_parts": exits, "research_portfolio_state": portfolio[identifier].state,
            "research_isolated_state": python.state, "research_isolated_entry_utc": utc(py_entry) if py_entry is not None else None,
            "research_isolated_fill_price": python.entry,
            "native_minus_research_fill_price": round(fill["price"] - python.entry, 9) if fill and python.entry is not None else None,
            "native_matches_research_entry_timestamp": native_entry == py_entry if stamps and py_entry is not None else None,
            "research_exit_parts": py_parts, "research_closed_parts_realized_usd": round(python.net, 9),
            "exit_units_matching_timestamp_and_reason": matched_exits,
            "exit_units_also_matching_price": matched_prices,
        })

    research_filled = {identifier for identifier, x in portfolio.items() if x.entry_bar is not None}
    native_filled = fills.keys() | parts.keys()
    timestamp_checks = [row for row in campaign_rows if row["native_matches_research_entry_timestamp"] is not None]
    fully_closed = [row for row in campaign_rows if row["native_closed_units"] == candidates[row["id"]].plan.units]
    priced_closed = [row for row in fully_closed if row["native_closed_parts_realized_usd_recomputed_without_spread"] is not None]
    matched_open = sum(row["native_matches_next_available_open"] is True for row in campaign_rows)
    return {
        "status": "NO_NATIVE_FILLS" if not native_filled else
                  "NATIVE_NEXT_AVAILABLE_OPEN_MATCH" if matched_open == len(native_filled) else "NATIVE_TIMING_MISMATCH",
        "signal_close_window_inclusive_utc": [utc(start), utc(end)],
        "historical_bars_used_for_replay": len(historical_bars),
        "chart_costs": {"tick": BASE.tick, "slippage_ticks": BASE.ticks,
                        "commission_each_side_per_unit": BASE.commission,
                        "native_emulated_spread": 0, "python_quote_spread": BASE.spread},
        "accepted_in_window": len(candidates), "native_submits": len(native), "native_explicit_fills": len(fills),
        "native_part_only_entry_timestamps": len(parts.keys() - fills.keys()),
        "native_submits_without_observed_fill_or_part": sorted(native.keys() - native_filled),
        "native_closed_parts": len(records[PART]),
        "native_fully_closed_campaigns": len(fully_closed),
        "native_exit_units": sum(row["native_closed_units"] for row in campaign_rows),
        "native_priced_fully_closed_campaigns": len(priced_closed),
        "native_fully_closed_net_usd_recomputed_without_spread": round(sum(
            row["native_closed_parts_realized_usd_recomputed_without_spread"] for row in priced_closed), 9),
        "research_fully_closed_net_usd_in_portfolio": round(sum(x.net for x in portfolio.values() if x.state == "CLOSED"), 9),
        "native_filled_campaigns_not_fully_closed": sum(row["entry_timestamp_source"] is not None and
            row["native_closed_units"] < candidates[row["id"]].plan.units for row in campaign_rows),
        "native_fills_matching_next_available_open": matched_open,
        "native_fills_matching_contiguous_open": sum(row["native_matches_contiguous_open"] is True for row in campaign_rows),
        "native_fill_prices_matching_mid_open_plus_slippage": sum(
            row["native_fill_matches_mid_open_plus_slippage"] is True for row in campaign_rows),
        "research_portfolio_states": dict(Counter(x.state for x in portfolio.values())),
        "research_portfolio_fills": len(research_filled),
        "native_and_research_portfolio_fill_ids": len(native_filled & research_filled),
        "native_only_fill_ids": sorted(native_filled - research_filled),
        "research_only_fill_ids": sorted(research_filled - native_filled),
        "research_isolated_entry_timestamps_compared": len(timestamp_checks),
        "research_isolated_entry_timestamps_matched": sum(row["native_matches_research_entry_timestamp"] for row in timestamp_checks),
        "research_isolated_exit_units_matching_timestamp_and_reason": sum(
            row["exit_units_matching_timestamp_and_reason"] for row in campaign_rows),
        "research_isolated_exit_units_also_matching_price": sum(row["exit_units_also_matching_price"] for row in campaign_rows),
        "research_only_fills": [{"id": identifier, "entry_utc": utc(historical_bars[portfolio[identifier].entry_bar]["time"]),
                                 "state": portfolio[identifier].state, "fill": portfolio[identifier].entry}
                                for identifier in sorted(research_filled - native_filled)],
        "campaigns": campaign_rows,
    }


def _files(folder, kind, frame):
    preferred = [f"{kind}-matched-M{frame}"]
    if kind == "tester":
        preferred.append(f"tester-final-M{frame}")
    for name in [*preferred, f"{kind}-M{frame}"]:
        found = [path for path in (folder / (name + ".csv"), folder / name) if path.is_file()]
        if len(found) > 1 and found[0].read_bytes() != found[1].read_bytes():
            raise ValueError(f"Multiple {name} TradingView CSVs")
        if found:
            return found[0]
    raise ValueError(f"Missing {kind} M{frame} TradingView CSV")


def _audit(folder, start, end):
    frames = {}
    for frame in (5, 15):
        indicator, ind_rows = _probe(_files(folder, "indicator", frame))
        tester, tester_rows = _probe(_files(folder, "tester", frame))
        if indicator.timeframe != frame or tester.timeframe != frame:
            raise ValueError(f"M{frame} source file reports a different chart timeframe")
        _, ind_comparison = _compare_probe(indicator, ind_rows)
        decisions, tester_comparison = _compare_probe(tester, tester_rows)
        frames[f"M{frame}"] = {
            "indicator": ind_comparison, "tester": tester_comparison,
            "indicator_vs_tester": _common(indicator, tester, ind_rows, tester_rows, decisions),
            "execution": _execution(tester, tester_rows, decisions, start, end),
        }
    comparisons = [frame[key]["status"] for frame in frames.values()
                   for key in ("indicator", "tester", "indicator_vs_tester")]
    status = "MATCH" if set(comparisons) == {"MATCH"} else (
        "DIFFERENT_WARMUP" if set(comparisons) <= {"MATCH", "DIFFERENT_WARMUP"} else "MISMATCH")
    return {
        "symbol": "OANDA:XAUUSD", "chart": "standard candles", "pine_version": 6,
        "decision_status": status,
        "execution_scope": "Historical TradingView broker emulator vs independent replay. Pine market orders fill at the next available open. Replay requires a contiguous next open, applies executable quote spread and risk repricing, and resolves ambiguous OHLC stop before target. Compare fill and exit timestamps separately from prices and realized P&L.",
        "forward_status": "Retrospective exported chart data only. No live or broker execution and no independent holdout claim.",
        "frames": frames,
    }


def audit(source, start, end):
    if source.is_dir():
        return _audit(source, start, end)
    with ZipFile(source) as archive, TemporaryDirectory() as temp:
        folder = Path(temp)
        for member in archive.infolist():
            name = Path(member.filename).name
            if name.removesuffix(".csv") not in {
                f"{kind}-M{frame}" for kind in ("indicator", "tester", "tester-final", "indicator-matched", "tester-matched")
                for frame in (5, 15)
            }:
                continue
            destination = folder / name
            if destination.exists():
                raise ValueError(f"Duplicate log basename in archive: {name}")
            destination.write_bytes(archive.read(member))
        report = _audit(folder, start, end)
        report["source_archive_sha256"] = sha256(source.read_bytes()).hexdigest()
        return report


def _stamp(iso):
    value = datetime.fromisoformat(iso)
    if value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise argparse.ArgumentTypeError("Expected a UTC timestamp with Z suffix or +00:00 offset")
    return int(value.timestamp() * 1000)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("native", type=Path, help="TradingView Pine Logs directory or ZIP")
    parser.add_argument("--start-utc", type=_stamp, default=DEFAULT_START)
    parser.add_argument("--end-utc", type=_stamp, default=DEFAULT_END)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.start_utc >= args.end_utc:
        parser.error("Start must be before end")
    result = audit(args.native, args.start_utc, args.end_utc)
    rendered = json.dumps(result, indent=2, default=float) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
    else:
        print(rendered, end="")
    return 0 if result["decision_status"] == "MATCH" else 1


if __name__ == "__main__":
    raise SystemExit(main())
