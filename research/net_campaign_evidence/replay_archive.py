"""Reprice the registered S5 admissions and preserve each campaign and liquidation mark."""

import argparse
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone
from gzip import compress
from hashlib import sha256
import json
from pathlib import Path
from statistics import median

from gold_pro.archive import native_outcomes
from gold_pro.direct_breakout_research import (
    BASE, EVENT_CONFIG, FORWARD_SEAL, HOUR, STRESS, WARMUP, independent_hours, journal, pinned, portfolio,
)
from research.data.loader import load_bars
from research.net_campaign_evidence.metrics import campaign_metrics, marked_drawdown
from unified.tools.audit_premium_s5 import verify_bars
from unified.tools.audit_tv_observations import inspect


ROOT = Path(__file__).resolve().parents[2]
REGISTRATION = ROOT / "docs/premium_evidence/s5_screen_registration.json"
REGISTRATION_SHA = "9ef45b538670332e797e5bcbb0660b912f910c54fdf94fab3eb899f5165e6d97"
FRAMES = (5, 15, 30)


def read_verified(path, digest):
    payload = path.read_bytes()
    if sha256(payload).hexdigest() != digest:
        raise ValueError(f"Unexpected registered input: {path}")
    return payload


def load_archive(data_root, native_root):
    registration = json.loads(read_verified(REGISTRATION, REGISTRATION_SHA))
    sealed_start = json.loads(pinned(FORWARD_SEAL))["forward_start_ms"]
    archive = {}
    for frame in FRAMES:
        source = registration["frames"][str(frame)]
        ohlc_path = data_root / Path(source["local_bars"]).parent.name / "bars.csv"
        native_path = native_root / source["log_name"]
        read_verified(ohlc_path, source["bars_sha256"])
        read_verified(native_path, source["log_sha256"])
        bars, native = load_bars(ohlc_path), inspect(native_path)
        if native["mode"] != "S5" or native["run"]["timeframe"] != str(frame) or native["unfilled"]:
            raise ValueError(f"M{frame}: unsupported original S5 control")
        if bars[-1]["time_close"] >= sealed_start:
            raise ValueError("Outside permitted historical cutoff")
        verify_bars(bars, native["run"])
        old_outcomes = native_outcomes(native, BASE)
        plans = [replace(item.plan, model="S5_RAW") for item in old_outcomes]
        if len({plan.id for plan in plans}) != len(plans) or not all(plan.units == 3 for plan in plans):
            raise ValueError(f"M{frame}: unexpectedly changed plan identity or units")
        archive[frame] = bars, plans, source
    return archive


def comparable_window(archive):
    m5, m15 = (archive[frame][0] for frame in (5, 15))
    matched = 0
    by_open = {row["time"]: row for row in m5}
    for bar in m15:
        block = [by_open.get(bar["time"] + minute * 60_000) for minute in (0, 5, 10)]
        if None in block:
            continue
        values = (block[0]["open"], max(b["high"] for b in block), min(b["low"] for b in block), block[-1]["close"])
        if values != tuple(bar[key] for key in ("open", "high", "low", "close")):
            raise ValueError(f"Unequal complete M5/M15 aggregate at {bar['time']}")
        matched += 1
    if matched != 1855:
        raise ValueError(f"Expected 1855 complete registered common aggregates, found {matched}")
    start = max(m5[WARMUP]["time"], m15[WARMUP]["time"])
    end = min(m5[-1]["time_close"], m15[-1]["time_close"])
    pivot = m5[WARMUP + (len(m5) - WARMUP) * 3 // 5]["time"]
    return {"full": (start, end), "early_60_seen": (start, pivot), "later_40_seen": (pivot, end)}, matched


def mark_series(outcomes, bars, start, end):
    """Mark equity through the cutoff and carry the starting open exposure into the window."""
    changes = {}
    for outcome in outcomes:
        previous = 0.
        for index, value in outcome.marked:
            stamp = bars[index]["time_close"]
            if stamp > end:
                break
            changes[stamp] = changes.get(stamp, 0.) + value - previous
            previous = value
    equity = sum(delta for stamp, delta in changes.items() if stamp <= start)
    marks = [(start, equity)]
    for bar in bars:
        stamp = bar["time_close"]
        if start < stamp <= end:
            equity += changes.get(stamp, 0.)
            marks.append((stamp, equity))
    return marks


def exposure_at_cutoff(outcome, bars, cutoff):
    if outcome.entry_bar is None or bars[outcome.entry_bar]["time"] >= cutoff:
        return None
    if outcome.state == "CLOSED" and bars[outcome.release]["time_close"] <= cutoff:
        return None
    active = [part for part in outcome.exits if bars[part["bar"]]["time_close"] <= cutoff]
    mark = next((x for i, x in reversed(outcome.marked) if bars[i]["time_close"] <= cutoff), None)
    if mark is None:
        raise ValueError("Opened campaign lacks the cutoff liquidation mark")
    realized = sum(part["net_usd"] for part in active)
    return {
        "id": outcome.plan.id, "open_units": outcome.plan.units * (3 - len(active)) / 3,
        "realized_net_usd": realized, "unrealized_net_usd": mark - realized,
        "total_liquidation_net_usd": mark, "exit_mark_includes_open_position_closing_cost": True,
    }


def event_score(outcomes, bars, events, start, end):
    rows = []
    candidates = {}
    fills = {}
    for out in outcomes:
        key = bars[out.plan.bar]["time_close"] // HOUR * HOUR, out.plan.side
        candidates.setdefault(key, []).append(out)
        if out.entry_bar is not None:
            stamp = bars[out.entry_bar]["time"]
            fills.setdefault((stamp // HOUR * HOUR, out.plan.side), []).append((stamp, out.plan.id))
    for event in events:
        hour, side = event["start"], event["side"]
        if hour < start or event["end"] > end:
            continue
        offers, entries = candidates.get((hour, side), []), fills.get((hour, side), [])
        entries.sort()
        delay = None if not entries else (entries[0][0] - hour) / 60_000
        rows.append({
            "hour_open_ms": hour, "side": side, "delay_minutes": delay,
            "timely": delay is not None and delay <= 30, "same_direction_fills": entries,
            "admitted_plan_dispositions": [{"id": x.plan.id, "state": x.state} for x in offers],
        })
    delays = [r["delay_minutes"] for r in rows if r["delay_minutes"] is not None]
    return {
        "hours": len(rows), "same_direction_fill_hours": len(delays),
        "timely_fill_hours": sum(r["timely"] for r in rows),
        "median_fill_delay_minutes_when_covered": median(delays) if delays else None,
        "by_side": {
            label: {"hours": sum(r["side"] == side for r in rows),
                    "fill_hours": sum(r["side"] == side and r["delay_minutes"] is not None for r in rows),
                    "timely_hours": sum(r["side"] == side and r["timely"] for r in rows)}
            for label, side in (("BUY", 1), ("SELL", -1))
        },
        "events": rows,
    }


def summarize(outcomes, bars, start, end, events):
    utc_day = lambda stamp: datetime.fromtimestamp(stamp / 1000, timezone.utc).date()
    days = sorted({utc_day(bar["time_close"]) for bar in bars if start < bar["time_close"] <= end})
    selected = [x for x in outcomes if x.entry_bar is not None and start <= bars[x.entry_bar]["time"] < end]
    closed = [{"id": x.plan.id, "close_ms": bars[x.release]["time_close"], "net_usd": x.net,
               "net_r": x.net / x.risk_usd, "side": x.plan.side}
              for x in selected if x.state == "CLOSED" and bars[x.release]["time_close"] <= end]
    closed.sort(key=lambda x: (x["close_ms"], x["id"]))
    exposure = [x for outcome in outcomes if (x := exposure_at_cutoff(outcome, bars, end)) is not None]
    marks = mark_series(outcomes, bars, start, end)
    counts = Counter(x.state for x in outcomes if start <= bars[x.plan.bar]["time_close"] < end)
    return {
        "bounds_ms": [start, end], "filled": len(selected), "calendar_days": len(days),
        "campaigns": campaign_metrics(closed, days),
        "campaigns_by_side": {
            label: campaign_metrics((x for x in closed if x["side"] == side), days)
            for label, side in (("BUY", 1), ("SELL", -1))
        },
        "plan_states": dict(counts), "open_at_cutoff": exposure,
        "quote_side_marked_equity": marked_drawdown(marks, initial_equity=marks[0][1]),
        "exit_part_reasons": dict(Counter(part["reason"] for x in selected for part in x.exits
                                         if bars[part["bar"]]["time_close"] <= end)),
        "movement_coverage": event_score(outcomes, bars, events, start, end),
    }


def evaluate(data_root, native_root):
    archive = load_archive(data_root, native_root)
    common, matched = comparable_window(archive)
    rules = json.loads(pinned(EVENT_CONFIG))["event"]
    shared_events, _, _ = independent_hours(archive[5][0], 5, rules)
    if sum(common["full"][0] <= e["start"] and e["end"] <= common["full"][1] for e in shared_events) != 33:
        raise ValueError("Original independent event cohort changed")
    summary, tapes = {}, {}
    for frame, (bars, plans, source) in archive.items():
        own = frame == 30
        events = independent_hours(bars, frame, rules)[0] if own else shared_events
        bounds = {"full": (bars[WARMUP]["time"], bars[-1]["time_close"])} if own else common
        name = f"M{frame}"
        summary[name] = {
            "source": source, "scope": "own_period" if own else "common_M5_M15",
            "archive_first_open_ms": bars[0]["time"], "archive_last_close_ms": bars[-1]["time_close"],
            "registered_plans": len(plans), "scenarios": {},
        }
        tapes[name] = {}
        for label, cost in (("normal", BASE), ("stress", STRESS)):
            outcomes = portfolio(plans, bars, cost, use_s5_lock=True)
            summary[name]["scenarios"][label] = {
                "cost": asdict(cost),
                "windows": {cohort: summarize(outcomes, bars, *limits, events)
                            for cohort, limits in bounds.items()},
            }
            tapes[name][label] = [
                {**journal(x, bars), "submitted_close_ms": bars[x.plan.bar]["time_close"],
                 "release_close_ms": bars[x.release]["time_close"] if x.release < len(bars) else None,
                 "units": x.plan.units, "atr": x.plan.atr, "maximum_hold_bars": x.plan.max_hold,
                 "net_r": x.net / x.risk_usd if x.state == "CLOSED" else None,
                 "filled_mark_to_market": [(bars[i]["time_close"], value) for i, value in x.marked]}
                for x in outcomes
            ]
    return {"exact_complete_aggregates": matched, "frames": summary}, tapes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--native-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    metrics, journal_data = evaluate(args.data_root, args.native_root)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "archive-metrics.json").write_bytes(
        (json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode())
    (args.output / "campaign-ledger.json.gz").write_bytes(
        compress(json.dumps(journal_data, sort_keys=True, allow_nan=False).encode(), mtime=0))


if __name__ == "__main__":
    main()
