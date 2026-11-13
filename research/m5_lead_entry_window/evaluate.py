"""Reproduce the frozen OANDA M5 lead study on archival chart and control tapes."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from gzip import compress
from hashlib import sha256
from io import BytesIO
import json
from math import isfinite
from pathlib import Path
from statistics import mean, median
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from gold_pro.direct_breakout_research import (
    BASE, EVENT_CONFIG, FORWARD_SEAL, HOUR, PINS, STRESS, WARMUP, WSB_ARCHIVE,
    independent_hours, journal, pinned, portfolio,
)
from gold_pro.rc2_research import early_observations
from research.data.loader import load_bars
from research.direct_trend_breakout.forensic_timeline import ms, utc
from research.direct_trend_breakout.shadow_native import read_native
from research.m5_lead_entry_window.replay import execute, replay
from research.professional_signal_selector.evaluate import (
    checked, cohort_statistics, extract, old_s5, require, tagged,
)
from research.validation.statistics import block_bootstrap, quantile, wilson
from unified.tools.gold_market_data import context_rows


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PROTOCOL_SHA = "e7adbaedc1e9519256729439e59b09156aab8e9876901e40aac18ff5b564462c"
VISUAL_ARCHIVE = ROOT / "research/luminous_visual_v2/evidence/native-pairs.zip"
VISUAL_SHA = "fec83cd615923b210b6815852a69b74ed457f791ac41b3913a7c189f7d58f952"
M15_SHA = "6c89015105a406c2a0c1567f0e9a5e445cb31ee7604a985d727f26892d8ba4b7"
M30_SHA = "1b31dc5dad532b9362c5340511211fbc13a33c64983d4a49485c7442a269c5d5"
CASE_START = ms("2026-11-11T00:00:00Z")
CASE_END = ms("2026-11-11T17:30:00Z")
FIXTURE_START = ms("2026-11-11T02:45:00Z")
FIXTURE_END = ms("2026-11-11T04:00:00Z")
COSTS = {"normal": BASE, "stress": STRESS}
SIDE = {-1: "SELL", 1: "BUY"}
OHLC = ("open", "high", "low", "close")
ENTRY_EVENTS = {"ENTRY_CANDIDATE", "REJECTED"}


def exact_aggregation(low, high):
    by_open = {bar["time"]: bar for bar in low}
    matched = 0
    for bar in high:
        parts = [by_open.get(stamp) for stamp in range(bar["time"], bar["time_close"], 300_000)]
        if not parts or any(row is None for row in parts):
            continue
        expected = (parts[0]["open"], max(row["high"] for row in parts),
                    min(row["low"] for row in parts), parts[-1]["close"])
        require(expected == tuple(bar[key] for key in OHLC), f"M5/M15 OHLC mismatch at {utc(bar['time'])}")
        matched += 1
    require(matched > 0, "No M5/M15 provider overlap")
    return matched


def m15_trends(bars):
    """Same directional formula as Visual V2, with this pinned archive's EMA seed."""
    fast = []
    slow = None
    source = []
    for row in bars:
        close = row["close"]
        fast.append(close if not fast else fast[-1] + (close - fast[-1]) / 11)
        slow = close if slow is None else slow + (close - slow) / 28
        current = fast[-1]
        bias = None
        if len(fast) >= 3:
            bias = 1 if close > current > slow and current >= fast[-3] else (
                -1 if close < current < slow and current <= fast[-3] else 0)
        source.append((row["time_close"], bias))
    return source


def add_context(m5, m15, m30):
    trend = m15_trends(m15)
    closes = [stamp for stamp, _ in trend]
    earlier = [bar for bar in m30 if bar["time_close"] <= m5[0]["time"]]
    attached = []
    for bar, context in zip(m5, context_rows(m5, earlier=earlier)):
        source = bisect_right(closes, bar["time"]) - 1
        known, side = trend[source] if source >= 0 else (None, None)
        require(known is None or known <= bar["time"], "Future M15 data")
        attached.append({**bar, "m15": side, "m15_known_ms": known, "h1": context.h1.side, "h4": context.h4.side,
                         "h1_known_ms": context.h1.known_at, "h4_known_ms": context.h4.known_at})
    return attached


def require_context_prefix(m5, m15, m30, whole):
    cuts = sorted({250, len(m5) // 2, 3 * len(m5) // 4, len(m5) - 1})
    for size in cuts:
        last_open = m5[size - 1]["time"]
        prefix_m15 = [bar for bar in m15 if bar["time_close"] <= last_open]
        prefix = add_context(m5[:size], prefix_m15, m30)
        require(prefix == whole[:size], f"MTF lookahead at prefix {size}")
    return cuts


def case_archive():
    with TemporaryDirectory() as scratch, ZipFile(BytesIO(checked(VISUAL_ARCHIVE, VISUAL_SHA))) as archive:
        folder = Path(scratch)
        paths = {frame: extract(archive, f"candidate-M{frame}.csv", folder) for frame in (5, 15)}
        tapes = {frame: read_native(path) for frame, path in paths.items()}
        # M5 appends one confirmed row past DT_END; all case rows precede that marker.
        tags = {frame: tagged(path.read_bytes()) for frame, path in paths.items()}
        return tapes, tags


def native_case_slots(rows, bars):
    opening = {row["time"]: i for i, row in enumerate(bars)}
    closing = {row["time_close"]: i for i, row in enumerate(bars)}
    by_plan = defaultdict(list)
    for event in map(json.loads, rows["DTV_PAPER"]):
        by_plan[event["plan_id"]].append(event)
    slots = []
    for pid, tape in by_plan.items():
        queued = next((e for e in tape if e["event"] == "QUEUED"), None)
        end = next((e for e in tape if e["event"] in {"CLOSED", "CANCELLED"}), None)
        if queued is None:
            entered = next((e for e in tape if e["event"] == "SIM_ENTRY"), None)
            if entered and opening.get(entered["effective_time"], 0) > 0:
                start = bars[opening[entered["effective_time"]] - 1]["time_close"]
            else:
                continue
        else:
            start = queued["known_time"]
        if start in closing:
            slots.append((start, end["known_time"] if end else bars[-1]["time_close"] + 300_000, pid))
    return sorted(slots)


def timestamps_slots(slots, bars):
    cutoff = bars[-1]["time_close"] + 300_000
    return sorted((bars[first]["time_close"], bars[release]["time_close"] if release < len(bars) else cutoff, pid)
                  for first, release, pid in slots)


def baseline_case_observations(tags):
    early = []
    for value in tags["RC2_EARLY"]:
        _, symbol, frame, known, side, close = value.split("|")
        require((symbol, frame) == ("OANDA:XAUUSD", "5"), "Unexpected native EARLY source")
        early.append({"known_ms": int(known), "side": 1 if side == "BUY" else -1, "price": float(close)})
    return early


def approved_entry_slot(candidate, slots):
    at = candidate["entry_ms"]
    return next((pid for start, stop, pid in slots if start <= at < stop), None)


def next_reservation(candidate, slots):
    at = candidate["entry_ms"]
    return next(((start, pid) for start, _, pid in slots if start > at), (None, None))


def usable_geometry(event, prices, cost):
    at = event.get("entry_ms")
    values = [event.get(name) for name in ("entry", "stop", "obstacle")]
    if at not in prices or any(type(value) not in (int, float) or not isfinite(value) for value in values):
        return False
    midpoint, stop, obstacle = values
    side = event["side"]
    return (prices[at]["open"] == midpoint and side * (midpoint - side * cost.spread / 2 - stop) > 0
            and side * (obstacle - midpoint - side * (cost.spread / 2 + cost.slip)) > 0)


def run_portfolio(events, bars, costs, bounds, slots=()):
    start, end = bounds
    prices = {bar["time"]: bar for bar in bars}
    index_by_open = {bar["time"]: i for i, bar in enumerate(bars)}
    offers = sorted((item for item in events if item["event"] == "ENTRY_CANDIDATE" and item["entry_ms"] is not None
                     and start <= item["entry_ms"] < end), key=lambda item: (item["entry_ms"], item["id"]))
    accepted, excluded = [], []
    occupied_until, occupant = -1, None
    for offer in offers:
        at = offer["entry_ms"]
        protected = approved_entry_slot(offer, slots)
        if protected or at < occupied_until:
            excluded.append({"id": offer["id"], "side": offer["side"], "entry_ms": at,
                             "reason": "APPROVED_SLOT_OCCUPIED" if protected else "RESEARCH_SLOT_OCCUPIED",
                             "occupied_by": protected or occupant})
            continue
        require(prices[at]["open"] == offer["entry"], f"Source midpoint disagrees with observed open: {offer['id']}")
        priority, approved = next_reservation(offer, slots)
        outcome = execute(offer, bars, costs, priority_close=priority, index_by_open=index_by_open)
        require(outcome is not None, f"Execution returned no result for source candidate {offer['id']}")
        if outcome["status"] == "UNEXECUTABLE":
            excluded.append({"id": offer["id"], "side": offer["side"], "entry_ms": at,
                             "reason": outcome["reason"], "occupied_by": None})
            continue
        exit_index = outcome.get("exit_bar")
        release = bars[exit_index]["time_close"] if type(exit_index) is int and exit_index < len(bars) else end + 300_000
        accepted.append({**outcome, "id": offer["id"], "side": offer["side"], "release_ms": release,
                         "entry_ms": at, "reference": offer["entry"], "stop": offer["stop"], "target": offer["obstacle"],
                         "quote_side_marks": marked_trade(outcome, bars, index_by_open[at], costs),
                         "next_priority_id": approved, "next_priority_close_ms": priority})
        occupied_until, occupant = release, offer["id"]
    return accepted, excluded


def is_closed(row):
    return row.get("status") == "CLOSED" and row.get("net_r") is not None and row.get("exit_ms") is not None


def cash_r(row):
    risk = row["initial_risk"]
    return row["net_r"] * risk


def marked_trade(trade, bars, first, costs):
    risk, fill, side = trade["initial_risk"], trade["fill"], trade["side"]
    last = trade["exit_bar"] if is_closed(trade) else len(bars)
    marks = []
    for bar in bars[first:last]:
        liquidation = bar["close"] - side * (costs.spread / 2 + costs.slip)
        net = side * (liquidation - fill) - 2 * costs.commission
        marks.append((bar["time_close"], net / risk, net))
    if is_closed(trade):
        marks.append((bars[last]["time_close"], trade["net_r"], cash_r(trade)))
    return marks


def statistics(rows, start, end, side=0, bootstrap=False):
    filled = [row for row in rows if start <= row["entry_ms"] < end and (not side or row["side"] == side)]
    closed = [row for row in filled if is_closed(row) and row["release_ms"] <= end]
    closed.sort(key=lambda row: (row["release_ms"], row["id"]))
    rs, cash = [row["net_r"] for row in closed], [cash_r(row) for row in closed]
    wins, losses = [v for v in rs if v > 0], [v for v in rs if v < 0]
    cash_wins, cash_losses = [v for v in cash if v > 0], [v for v in cash if v < 0]
    total = peak = drawdown = 0.
    for value in rs:
        total += value
        peak = max(peak, total)
        drawdown = max(drawdown, peak - total)
    equity_r = equity_cash = peak_r = peak_cash = dd_r = dd_cash = 0.
    for trade in sorted(filled, key=lambda row: (row["entry_ms"], row["id"])):
        for stamp, r, value in trade["quote_side_marks"]:
            if stamp > end:
                break
            peak_r = max(peak_r, equity_r + r)
            peak_cash = max(peak_cash, equity_cash + value)
            dd_r = max(dd_r, peak_r - equity_r - r)
            dd_cash = max(dd_cash, peak_cash - equity_cash - value)
        if is_closed(trade) and trade["release_ms"] <= end:
            equity_r += trade["net_r"]
            equity_cash += cash_r(trade)
    return {
        "filled": len(filled), "closed": len(closed), "open_or_straddling": len(filled) - len(closed),
        "wins": len(wins), "losses": len(losses), "win_rate": len(wins) / len(closed) if closed else None,
        "wilson_95": wilson(len(wins), len(closed)),
        "net_r": sum(rs), "expectancy_r": mean(rs) if rs else None,
        "net_usd_per_unit": sum(cash), "pf_r": -sum(wins) / sum(losses) if losses else None,
        "pf_usd_per_unit": -sum(cash_wins) / sum(cash_losses) if cash_losses else None,
        "average_win_r": mean(wins) if wins else None, "average_loss_r": mean(losses) if losses else None,
        "avg_win_to_loss": -mean(wins) / mean(losses) if wins and losses else None,
        "worst_r": min(rs) if rs else None, "p05_r": quantile(rs, .05),
        "mean_worst_five_percent_r": mean(sorted(rs)[:max(1, (len(rs) + 19) // 20)]) if rs else None,
        "realized_campaign_close_max_drawdown_r": drawdown,
        "marked_chart_close_max_drawdown_r": dd_r,
        "marked_chart_close_max_drawdown_usd_per_unit": dd_cash,
        "expectancy_block_bootstrap": block_bootstrap(rs) if bootstrap else None,
        "close_reasons": dict(Counter(row["reason"] for row in closed)),
    }


def classify_counterfactuals(events, bars, costs, exclusions):
    prices = {bar["time"]: bar for bar in bars}
    by_open = {bar["time"]: i for i, bar in enumerate(bars)}
    blocked = {row["id"]: row["reason"] for row in exclusions}
    controls = []
    for row in events:
        if row["event"] not in ENTRY_EVENTS or row.get("entry_ms") is None:
            continue
        if row["event"] == "ENTRY_CANDIDATE" and row["id"] not in blocked:
            continue
        reason = blocked.get(row["id"], row["reason"])
        known = usable_geometry(row, prices, costs) and row["reason"] not in {"ENTRY_GAP", "OPEN_THROUGH_STOP"}
        outcome = execute({**row, "event": "ENTRY_CANDIDATE"}, bars, costs, index_by_open=by_open) if known else None
        state = ("UNCOMPARABLE_GEOMETRY" if not known or outcome is None or outcome["status"] == "UNEXECUTABLE"
                 else "CENSORED" if not is_closed(outcome) else "WIN" if outcome["net_r"] > 0
                 else "LOSS" if outcome["net_r"] < 0 else "FLAT")
        controls.append({"id": row["id"], "side": row["side"], "known_at_utc": utc(row["known_ms"]),
                         "reason": reason, "counterfactual_status": state,
                         "counterfactual_net_r": outcome["net_r"] if outcome and is_closed(outcome) else None})
    return {
        "states_by_original_reason": {reason: dict(Counter(x["counterfactual_status"] for x in controls
                                                          if x["reason"] == reason))
                                      for reason in sorted({x["reason"] for x in controls})},
        "ledger": controls,
    }


def first_unique(events, eligible, anchor, lookback=0):
    """Match each observation once per stage; own UTC hour has precedence."""
    available = sorted(events, key=lambda row: (row[0], row[1], row[2]))
    used = set()
    matched = {}
    for episode in eligible:
        start, side = episode["start"], episode["side"]
        within = [row for row in available if row[1] == side and start <= row[0] < start + HOUR and row[2] not in used]
        before = [row for row in available if row[1] == side and start - lookback <= row[0] < start and row[2] not in used
                  and (row[0] // HOUR * HOUR, side) not in anchor]
        for stage, records in (("in_hour", within), ("pre_hour", before)):
            if records:
                winner = min(records)
                used.add(winner[2])
                matched.setdefault(start, {})[stage] = winner
    return matched


def coverage(independent, categories, observations, early, outcomes, shadow=()):
    anchor = {(row["start"], row["side"]) for row in independent}
    kinds = {
        "watch": [(row["known_ms"], row["side"], row["id"]) for row in observations if row["event"] == "EARLY_SETUP"],
        "micro_break": [(row["known_ms"], row["side"], row["id"]) for row in observations
                        if row["event"] in {"MICRO_BREAK", "SECOND_CHANCE"}],
        "candidate_known": [(row["known_ms"], row["side"], row["id"]) for row in observations if row["event"] == "ENTRY_CANDIDATE"],
        "fill_at_open": [(row["entry_ms"], row["side"], row["id"]) for row in outcomes],
        "rc2_early": [(row["known_ms"], row["side"], f"{row['known_ms']}|{row['side']}") for row in early],
    }
    if shadow:
        kinds["native_shadow_watch"] = [(row["time_ms"], row["side"], f"{row['model']}|{row['time_ms']}|{row['side']}")
                                       for row in shadow if row["state"] in {"WATCH", "RETEST WATCH"}]
        kinds["native_shadow_confirm"] = [(row["time_ms"], row["side"], f"{row['model']}|{row['time_ms']}|{row['side']}")
                                         for row in shadow if row["state"] == "BREAKOUT CONFIRMED"]
    lookback = 15 * 60_000
    matching = {key: first_unique(events, independent, anchor, lookback if "watch" in key or key == "rc2_early" else 0)
                for key, events in kinds.items()}
    rows = []
    for episode in independent:
        at, side = episode["start"], episode["side"]
        lead = {}
        for stage, store in matching.items():
            for when, entry in store.get(at, {}).items():
                lead[f"{stage}_{when}"] = (entry[0] - at) / 60_000
        study = matching["watch"].get(at, {})
        legacy = matching["rc2_early"].get(at, {})
        paired = lambda first, second: ((min(v[0] for v in second.values()) - min(v[0] for v in first.values())) / 60_000
                                        if first and second else None)
        both_lead = paired(study, legacy)
        rows.append({"start_utc": utc(at), "end_utc": utc(episode["end"]), "side": SIDE[side],
                     "watch_earlier_than_rc2_minutes": both_lead,
                     "watch_earlier_than_native_shadow_minutes": paired(study, matching.get("native_shadow_watch", {}).get(at, {})),
                     "micro_earlier_than_native_shadow_confirm_minutes": paired(
                         matching["micro_break"].get(at, {}), matching.get("native_shadow_confirm", {}).get(at, {})),
                     **lead})
    summary = {}
    for label in ("all", "BUY", "SELL"):
        cohort = [row for row in rows if label == "all" or row["side"] == label]
        leads = [row["watch_earlier_than_rc2_minutes"] for row in cohort if row["watch_earlier_than_rc2_minutes"] is not None]
        summary[label] = {
            "independent_hours": len(cohort),
            "watch_strictly_before_hour": sum("watch_pre_hour" in row for row in cohort),
            "watch_in_hour": sum("watch_in_hour" in row for row in cohort),
            "missed_any_watch": sum("watch_in_hour" not in row and "watch_pre_hour" not in row for row in cohort),
            "micro_break_in_hour": sum("micro_break_in_hour" in row for row in cohort),
            "risk_candidate_known_in_hour": sum("candidate_known_in_hour" in row for row in cohort),
            "executed_in_hour": sum("fill_at_open_in_hour" in row for row in cohort),
            "missed_executable_hours": sum("fill_at_open_in_hour" not in row for row in cohort),
            "legacy_rc2_early_in_hour": sum("rc2_early_in_hour" in row for row in cohort),
            "legacy_rc2_early_strictly_before_hour": sum("rc2_early_pre_hour" in row for row in cohort),
            "legacy_rc2_early_missed_any": sum("rc2_early_in_hour" not in row and "rc2_early_pre_hour" not in row
                                             for row in cohort),
            "native_shadow_confirm_in_hour": sum("native_shadow_confirm_in_hour" in row for row in cohort) if shadow else None,
            "median_paired_watch_lead_vs_legacy_min": median(leads) if leads else None,
            "paired_lead_hours": len(leads),
            "paired_watch_earlier_equal_later_counts": {
                "earlier": sum(x > 0 for x in leads), "equal": sum(x == 0 for x in leads), "later": sum(x < 0 for x in leads)},
            "median_latency_minutes_by_stage": {
                stage: median([row[stage] for row in cohort if stage in row])
                if any(stage in row for row in cohort) else None
                for stage in sorted({key for row in rows for key in row if key.endswith(("_in_hour", "_pre_hour"))})
            },
        }
    extras = {}
    for stage, events in kinds.items():
        used = {row[2] for value in matching[stage].values() for row in value.values()}
        extras[stage] = {
            "decisions": len(events), "matched_unique_events": len(used),
            "outside_any_same_side_event_window": dict(Counter(categories.get(at // HOUR * HOUR, "INELIGIBLE_OR_PARTIAL")
                                                               for at, side, identity in events if identity not in used
                                                               and (at // HOUR * HOUR, side) not in anchor)),
        }
    return {"summary": summary, "episodes": rows, "outside_event_decisions": extras}


def false_breakout(observations, bars, bars_by_close):
    labels = []
    for event in observations:
        if event["event"] != "MICRO_BREAK":
            continue
        index = bars_by_close[event["known_ms"]]
        future = bars[index + 1:index + 4]
        complete = len(future) == 3 and all(row["time"] == bars[index + i]["time_close"] for i, row in enumerate(future))
        reclaim = next((row["time_close"] for row in future if event["side"] * (row["close"] - event["level"]) < 0), None)
        labels.append({"id": event["id"], "side": event["side"], "known_ms": event["known_ms"],
                       "valid_horizon": complete, "first_reclaim_utc": utc(reclaim) if complete and reclaim else None})
    return {
        side: {
            "observed_breaks": len(cohort), "eligible_3_contiguous": (n := sum(row["valid_horizon"] for row in cohort)),
            "strict_reclaims": (bad := sum(row["first_reclaim_utc"] is not None for row in cohort)),
            "reclaim_rate": bad / n if n else None, "wilson_95": wilson(bad, n),
        } for side in ("all", "BUY", "SELL")
        for cohort in [[row for row in labels if side == "all" or SIDE[row["side"]] == side]]
    }


def examples(independent, categories, observations, bars, window):
    begin, end = window
    events = {row["start"]: row for row in independent}
    hot = {row["known_ms"] // HOUR * HOUR for row in observations if row["event"] in {"EARLY_SETUP", "MICRO_BREAK"}}
    samples = {}
    for kind in ("BUY", "SELL", "NON_SIGNIFICANT"):
        selected = [hour for hour in sorted(categories) if begin <= hour and hour + HOUR <= end and (
            hour in hot and hour not in events if kind == "NON_SIGNIFICANT"
            else hour in events and SIDE[events[hour]["side"]] == kind)][:4]
        samples[kind] = []
        for hour in selected:
            bucket = [bar for bar in bars if hour <= bar["time"] < hour + HOUR]
            samples[kind].append({
                "start_utc": utc(hour), "classification": categories[hour], "open": bucket[0]["open"],
                "close": bucket[-1]["close"], "events": [row for row in observations if hour <= row["known_ms"] < hour + HOUR],
            })
    return samples


def context_conditioned(events, bars):
    by_close = {row["time_close"]: row for row in bars}
    result = defaultdict(Counter)
    for event in events:
        if event["event"] not in {"EARLY_SETUP", "MICRO_BREAK", "ENTRY_CANDIDATE", "REJECTED"}:
            continue
        trend = by_close[event["known_ms"]]["m15"]
        status = "MISSING" if trend is None else "ALIGNED" if trend == event["side"] else "COUNTERTREND" if trend else "MIXED"
        result[status][event["event"]] += 1
    return {key: dict(counts) for key, counts in result.items()}


def baseline_directional_availability(controls, outcomes, bars, independent):
    by_hour = {(row["start"], row["side"]) for row in independent}
    admitted = {(bars[plan.bar]["time_close"] // HOUR * HOUR, plan.side) for plan in controls}
    filled = {(bars[row.entry_bar]["time"] // HOUR * HOUR, row.plan.side) for row in outcomes if row.entry_bar is not None}
    return {
        side: {"directional_hours": len(hours), "admitted_same_side": len(hours & admitted),
               "filled_same_side": len(hours & filled), "missed": len(hours - filled)}
        for side in ("all", "BUY", "SELL")
        for hours in [{row for row in by_hour if side == "all" or SIDE[row[1]] == side}]
    }


def run_window(bars, early, slots, bounds, event_rules, controls=(), shadow=()):
    begin, end = bounds
    index = {bar["time_close"]: i for i, bar in enumerate(bars)}
    directional, categories, audit = independent_hours(bars, 5, event_rules)
    events = [episode for episode in directional if begin <= episode["start"] and episode["end"] <= end]
    early = [row for row in early if begin - 900_000 <= row["known_ms"] <= end]
    settings = {}
    split = bars[WARMUP + (len(bars) - WARMUP) * 3 // 5]["time"]
    windows = {"full": bounds}
    if begin < split < end:
        windows.update(early_60=(begin, split), later_40=(split, end))
    for name, costs in COSTS.items():
        transitions = replay(bars, costs=costs)
        for size in (len(bars) // 2, 3 * len(bars) // 4, len(bars) - 1):
            prefix = replay(bars[:size], costs=costs)
            cutoff = bars[size - 1]["time_close"]
            require(prefix == [row for row in transitions if row["known_ms"] <= cutoff], f"M5 lookahead at {size}")
        observed = [row for row in transitions if begin <= row["known_ms"] <= end]
        audit_coverage = [row for row in transitions if begin - 900_000 <= row["known_ms"] <= end]
        paths = {}
        for case, control_slots in (("standalone", ()), ("approved_priority", slots)):
            filled, blocked = run_portfolio(observed, bars, costs, bounds, control_slots)
            paths[case] = {
                "metrics": {key: {label: statistics(filled, a, b, side, key == "full" and side == 0)
                                  for side, label in ((0, "all"), *SIDE.items())} for key, (a, b) in windows.items()},
                "diagnostic_coverage": coverage(events, categories, audit_coverage, early, filled, shadow),
                "position_exclusions": blocked,
                "source_risk_rejection_and_occupancy_counterfactuals": classify_counterfactuals(observed, bars, costs, blocked),
                "journal": filled,
            }
        settings[name] = {
            "event_states": dict(Counter(row["event"] for row in observed)),
            "events_by_side": {label: dict(Counter(row["event"] for row in observed if row["side"] == side))
                               for side, label in SIDE.items()},
            "source_rejections": dict(Counter(row["reason"] for row in observed if row["event"] == "REJECTED")),
            "breakout_reclaim_after_3": false_breakout(observed, bars, index),
            "m15_context": context_conditioned(observed, bars),
            "portfolio": paths,
            "all_confirmed_events": observed,
        }
        if controls:
            warm = [plan for plan in controls if plan.bar >= WARMUP]
            control = portfolio(warm, bars, costs, use_s5_lock=True)
            settings[name]["unchanged_s5_admitted_offer_replay"] = {
                "metrics": cohort_statistics(control, bars, windows), "journal": [journal(row, bars) for row in control],
                "same_side_directional_availability": baseline_directional_availability(warm, control, bars, events),
            }
    return {
        "scored_window_utc": [utc(begin), utc(end)], "loaded_bars": len(bars),
        "independent_event_catalog": audit, "scored_directional_hours": dict(Counter(SIDE[row["side"]] for row in events)),
        "descriptive_windows_utc": {key: [utc(a), utc(b)] for key, (a, b) in windows.items()},
        "results": settings,
        "counterexamples": examples(events, categories, settings["normal"]["all_confirmed_events"], bars, bounds),
    }


def read_m15_m30(root):
    path15, path30 = root / "ui_dataset/bars.csv", root / "M30_dataset/bars.csv"
    checked(path15, M15_SHA)
    checked(path30, M30_SHA)
    return load_bars(path15), load_bars(path30)


def frozen_geometry_pricing(prices, protocol):
    require(protocol["tick"] == BASE.tick == STRESS.tick, "Frozen tick changed")
    for label, costs in prices.items():
        frozen = protocol["costs"][label]
        require((costs.spread, costs.ticks, costs.commission) == (
            frozen["spread"], frozen["slippage_ticks_per_market_or_stop"], frozen["commission_price_per_fill"]),
            f"Frozen {label} costs differ")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True, help="Registered M15 and M30 archive roots")
    parser.add_argument("--output", type=Path, default=HERE / "evidence" / "evaluation.json.gz")
    args = parser.parse_args()
    protocol = json.loads(checked(HERE / "FROZEN_PROTOCOL.json", PROTOCOL_SHA))
    frozen_geometry_pricing(COSTS, protocol)
    event_rules = json.loads(pinned(EVENT_CONFIG))["event"]
    m15, m30 = read_m15_m30(args.data_root)
    with TemporaryDirectory() as scratch, ZipFile(BytesIO(pinned(WSB_ARCHIVE))) as source:
        folder = Path(scratch)
        market = extract(source, "archival_m5_bars.csv", folder)
        pinned(market.name, market.read_bytes())
        broad = load_bars(market)
        s5_plans, _, s5_slots, s5_provenance = old_s5(broad, source, folder)
    broad_overlap = exact_aggregation(broad, m15)
    case, case_tags = case_archive()
    case_overlap = exact_aggregation(case[5].bars, case[15].bars)
    seal = json.loads(pinned(FORWARD_SEAL))["forward_start_ms"]
    require(max(data[-1]["time_close"] for data in (broad, m15, m30, case[5].bars, case[15].bars)) < seal,
            "Sealed market data cannot be accessed")
    broad_joined = add_context(broad, m15, m30)
    case_bars = [bar for bar in case[5].bars if bar["time_close"] <= CASE_END]
    case_joined = add_context(case_bars, case[15].bars, m30)
    context_proof = {
        "archival_prefixes": require_context_prefix(broad, m15, m30, broad_joined),
        "case_prefixes": require_context_prefix(case_bars, case[15].bars, m30, case_joined),
        "archival_exact_m5_to_m15_aggregates": broad_overlap, "native_case_exact_m5_to_m15_aggregates": case_overlap,
        "scope": "M15 formula reconstructed on named confirmed archive, matching bar-open join semantics. H1/H4 use prior verified finite GOLD PRO context as an advisory reconstruction. Neither implies native legacy bias parity.",
    }
    historical = run_window(broad_joined, [row for row in early_observations(broad)
                                          if row["known_ms"] >= broad[WARMUP]["time"]],
                            timestamps_slots(s5_slots, broad),
                            (broad[WARMUP]["time"], broad[-1]["time_close"]), event_rules, s5_plans)
    case_result = run_window(case_joined, baseline_case_observations(case_tags[5]),
                             native_case_slots(case_tags[5], case_bars), (CASE_START, CASE_END), event_rules,
                             shadow=[row for row in case[5].events if CASE_START <= row["time_ms"] <= CASE_END])
    case_result["around_0300_utc"] = {
        "m5_market": [row for row in case_joined if FIXTURE_START <= row["time_close"] <= FIXTURE_END],
        "m15_market": [row for row in case[15].bars if FIXTURE_START <= row["time_close"] <= FIXTURE_END],
        "m5_original_early": [row for row in baseline_case_observations(case_tags[5])
                              if FIXTURE_START <= row["known_ms"] <= FIXTURE_END],
        "original_direction_and_permission": {
            f"M{frame}": [{key: row[key] for key in ("known_ms", "direction", "permission", "paper_phase")}
                          for row in map(json.loads, case_tags[frame]["DTV_STATE"])
                          if FIXTURE_START <= row["known_ms"] <= FIXTURE_END] for frame in (5, 15)},
        "original_shadow": {
            f"M{frame}": [row for row in map(json.loads, case_tags[frame]["DT_SHADOW"])
                          if FIXTURE_START <= row["time_ms"] <= FIXTURE_END] for frame in (5, 15)},
        "new_transitions_normal": [row for row in case_result["results"]["normal"]["all_confirmed_events"]
                                   if FIXTURE_START <= row["known_ms"] <= FIXTURE_END],
        "registered_0300_hour_significant": any(row["start_utc"] == "2026-11-11T03:00:00Z" for row in
            case_result["results"]["normal"]["portfolio"]["standalone"]["diagnostic_coverage"]["episodes"]),
    }
    report = {
        "schema": "qirvanta.m5-lead.historical-research.v1", "status": "INCONCLUSIVE_RESEARCH_ONLY",
        "protocol_sha256": PROTOCOL_SHA, "replay_sha256": sha256((HERE / "replay.py").read_bytes()).hexdigest(),
        "untouched_holdout": "NOT_AVAILABLE_NOT_ACCESSED",
        "inputs": {"registered_m5_sha256": PINS["archival_m5_bars.csv"], "registered_m15_sha256": M15_SHA,
                   "prior_m30_sha256": M30_SHA, "case_native_pairs_sha256": VISUAL_SHA,
                   "registered_s5_control": s5_provenance},
        "context": context_proof, "archival_m5": historical, "visual_v2_native_market_case": case_result,
        "assumptions": {
            "basis": "This output simulates a conditional instruction fixed on the B close. C is confirmed only after the modeled next-open time; a C notification cannot achieve that fill.",
            "execution": "One unit/reference midpoint R in the research arm. S5 source volume and native original events remain fixed; economic reference separately replays registered admissions with S5 three-part geometry and one slot.",
            "drawdown": "Research reports both realized campaign-close R and quote-side marked chart-close R and USD per unit. Intrabar equity is unknown. S5 retains its original 3-unit cash basis and an OHLC marked-close USD estimate. These bases do not establish lower risk.",
            "quotes": "Quote sides model midpoint +/- half spread, no duplicated spread charges. Real OANDA bid/ask, queue, financing and intrabar chronology are unavailable.",
            "analysis": "Observed-window only. Unique event-stage identity matched once per study, with same-hour direction preferred. Outside same-side event observations and three-close strict reclaims are distinct. All time labels use confirmed UTC or explicitly separate effective entry opens.",
        },
    }
    payload = (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(compress(payload, mtime=0) if args.output.suffix == ".gz" else payload)
    print(f"Evaluated {len(broad)} registered M5 bars and {len(case_bars)} bounded native case bars.")


if __name__ == "__main__":
    main()

