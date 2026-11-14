"""Fixed-budget chronological research replay over chart OHLC and admitted-plan controls."""

import argparse
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from gzip import compress
from hashlib import sha256
import json
import math
from pathlib import Path
from statistics import median

from gold_pro.direct_breakout_research import (
    BASE, EVENT_CONFIG, HOUR, MAX_OPEN_MOVE_ATR, STRESS, WARMUP, Execution, independent_hours, journal, pinned, simulate,
)
from gold_pro.replay import Plan, round_away
from research.gold_end_to_end.controls import read_inputs
from research.gold_end_to_end.regimes import regime_tape
from research.gold_end_to_end.signals import scan
from research.net_campaign_evidence.metrics import campaign_metrics, marked_drawdown
from research.net_campaign_evidence.replay_archive import exposure_at_cutoff, mark_series


ROOT = Path(__file__).resolve().parent
REGISTRATION = ROOT / "REGISTRATION.json"
ARMS = ("S5_ARCHIVE", "G_BASE", "G_HOLD", "M_CROSS", "M_HOLD")
SCENARIOS = {"normal": BASE, "stress": STRESS}
CAPITAL = 100_000.
BUDGET = 250.
LOT = .001


def stamp(text):
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


def offer_plan(offer):
    ref, stop, side = offer["reference"], offer["stop"], offer["side"]
    distance = (ref - stop) * side
    targets = tuple(round_away(ref + side * distance * level, BASE.tick, side) for level in (1, 2, 3))
    return Plan(offer["id"], offer["model"], side, offer["index"], ref, stop, targets, offer["atr"], 48)


def open_gate(plan, bars, cost, equity):
    """Use the oldest possible execution price, independently in each cost lane."""
    if plan.bar < WARMUP:
        return "WARMUP", None
    direction = plan.side
    price_risk = (plan.reference - plan.stop) * direction
    initial_min, initial_max = .8 * plan.atr, 3.5 * plan.atr
    friction = cost.spread + 2 * cost.slip + 2 * cost.commission / cost.pointvalue
    if plan.model != "S5_RAW":
        if price_risk + 1e-9 < max(cost.tick, initial_min) or price_risk > initial_max + 1e-9:
            return "SIGNAL_STOP_GEOMETRY", None
        if friction / price_risk > .18:
            return "SIGNAL_COST_GATE", None
    first = plan.bar + 1
    if first == len(bars):
        return "CENSORED_PENDING", None
    if bars[first]["time"] != bars[plan.bar]["time_close"]:
        return "ENTRY_SESSION_GAP", None
    midpoint = bars[first]["open"]
    midpoint_risk = (midpoint - plan.stop) * direction
    fill_quote = midpoint + direction * (cost.spread / 2 + cost.slip)
    liquidation_quote = midpoint - direction * cost.spread / 2
    if (liquidation_quote - plan.stop) * direction <= 0 or (plan.targets[0] - fill_quote) * direction <= 0:
        return "OPEN_OUTSIDE_SL_TP1", None
    if abs(midpoint - plan.reference) > MAX_OPEN_MOVE_ATR * plan.atr + 1e-9:
        return "OPEN_DEVIATION", None
    if plan.model != "S5_RAW" and not initial_min <= midpoint_risk <= initial_max:
        return "OPEN_STOP_GEOMETRY", None
    if friction / midpoint_risk > .18:
        return "OPEN_COST_GATE", None
    unit_risk = ((fill_quote - plan.stop) * direction + cost.slip) * cost.pointvalue + 2 * cost.commission
    # All three equal brackets must each fit the simulated quantity grid.
    lot_group = 3 * LOT
    lots = math.floor(BUDGET / unit_risk / lot_group)
    units = lots * lot_group
    if units * unit_risk > BUDGET:
        units = (lots - 1) * lot_group
    if units <= 0 or units * fill_quote * cost.pointvalue > min(equity, CAPITAL):
        return "CAPITAL_OR_LOT", None
    return None, replace(plan, units=units)


def execute(plans, bars, cost):
    """Reserve one slot, settle its realized cash before later decisions, and preserve rejections."""
    outcomes = []
    occupied = None
    equity = CAPITAL
    used_bar = -1
    for plan in sorted(plans, key=lambda row: (row.bar, row.id)):
        if occupied is not None and occupied.release <= plan.bar:
            equity += occupied.net
            occupied = None
        if occupied is not None or used_bar == plan.bar:
            outcome = Execution(plan, "POSITION_OCCUPIED", plan.bar,
                                blocked_by=occupied.plan.id if occupied else outcomes[-1].plan.id)
        else:
            used_bar = plan.bar
            reason, sized = open_gate(plan, bars, cost, equity)
            if reason:
                release = plan.bar + (reason not in {"WARMUP", "SIGNAL_STOP_GEOMETRY", "SIGNAL_COST_GATE"})
                outcome = Execution(plan, reason, release)
            else:
                outcome = simulate(sized, bars, cost, lock_r=.05)
                if outcome.entry_bar is None:
                    raise ValueError(f"Open validation disagrees with the frozen execution model for {plan.id}")
                occupied = outcome
        outcomes.append(outcome)
    return outcomes


def utc_day(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).date()


def classify(axis, key, side, context):
    if axis == "side":
        return key == ("BUY" if side == 1 else "SELL")
    if axis == "all":
        return True
    return context[axis] == key


GROUPS = {
    "all": ("ALL",),
    "trend": ("UP", "DOWN", "SIDEWAYS", "UNKNOWN"),
    "volatility": ("HIGH", "NORMAL", "LOW", "UNKNOWN"),
    "session": ("ASIA", "EUROPE", "US", "ROLLOVER"),
    "side": ("BUY", "SELL"),
}


def coverage(events, complete_hours, observations, outcomes, bars, context, start, end):
    signal_times, candidates, fills = defaultdict(list), defaultdict(list), defaultdict(list)
    for x in observations:
        clock = x["known_ms"]
        signal_times[clock // HOUR * HOUR, x["side"]].append(clock)
    for out in outcomes:
        decision = bars[out.plan.bar]["time_close"]
        candidates[decision // HOUR * HOUR, out.plan.side].append((decision, out.plan.id, out.state))
        if out.entry_bar is not None:
            entry = bars[out.entry_bar]["time"]
            fills[entry // HOUR * HOUR, out.plan.side].append((entry, out))
    eligible = {e["start"]: e["side"] for e in events if start <= e["start"] and e["end"] <= end}
    rows = []
    for event in events:
        begin, side = event["start"], event["side"]
        if begin not in eligible:
            continue
        entry = min(fills.get((begin, side), ()), key=lambda pair: pair[0], default=None)
        seen = min(signal_times.get((begin, side), ()), default=None)
        offers = sorted(candidates.get((begin, side), ()))
        percent = None
        if entry:
            # Completed move is used only for ex-post scoring, never entry selection.
            fill_mid = bars[entry[1].entry_bar]["open"]
            percent = side * (fill_mid - event["open"]) / abs(event["close"] - event["open"])
        rows.append({
            "hour": begin, "side": side,
            "regime": context.get(begin, {"trend": "UNKNOWN", "volatility": "UNKNOWN", "session": session(begin)}),
            "first_observed_ms": seen, "first_candidate_ms": offers[0][0] if offers else None,
            "first_executable_decision_ms": next((t for t, _, state in offers
                                                   if state in {"CLOSED", "CENSORED_OPEN"}), None),
            "first_fill_ms": entry[0] if entry else None, "first_fill_plan_id": entry[1].plan.id if entry else None,
            "first_fill_known_ms": bars[entry[1].entry_bar]["time_close"] if entry else None,
            "fill_delay_minutes": (entry[0] - begin) / 60_000 if entry else None,
            "timely": bool(entry and entry[0] - begin <= 30 * 60_000),
            "fraction_move_elapsed": percent, "candidates": offers,
        })
    signals_in_full_hours = [(hour, side) for (hour, side) in signal_times
                             if start <= hour and hour + HOUR <= end and hour in complete_hours]
    outside = sum(eligible.get(hour) != side for hour, side in signals_in_full_hours)
    return rows, outside, len(signals_in_full_hours)


def session(clock):
    hour = clock // HOUR % 24
    return "ASIA" if hour < 7 else "EUROPE" if hour < 13 else "US" if hour < 21 else "ROLLOVER"


def summarize(outcomes, observations, bars, contexts, events, complete_hours, start, end, frame, arm, cost_name):
    entries = [x for x in outcomes if x.entry_bar is not None and start <= bars[x.entry_bar]["time"] < end]
    closed = sorted((x for x in entries if x.state == "CLOSED" and bars[x.release]["time_close"] <= end),
                    key=lambda x: (x.release, x.plan.id))
    days = sorted({utc_day(bar["time_close"]) for bar in bars if start < bar["time_close"] <= end})
    day_events, outside, denominator = coverage(events, complete_hours, observations, outcomes, bars, contexts, start, end)
    regime_at = lambda x: contexts[bars[x.plan.bar]["time_close"]]
    rows = []
    for axis, keys in GROUPS.items():
        for key in keys:
            group = [x for x in closed if classify(axis, key, x.plan.side, regime_at(x))]
            campaign_rows = [{"id": x.plan.id, "close_ms": bars[x.release]["time_close"], "net_usd": x.net,
                              "net_r": x.net / x.risk_usd} for x in group]
            numbers = campaign_metrics(campaign_rows, days)
            included = [row for row in day_events if classify(axis, key, row["side"], row["regime"])]
            delays = [row["fill_delay_minutes"] for row in included if row["first_fill_ms"] is not None]
            cost = SCENARIOS[cost_name]
            gross_model_cost = sum(x.plan.units / 3 * (
                2 * cost.commission + (cost.spread + cost.slip * (1 if part["reason"].startswith("TP") else 2))
                * cost.pointvalue
            ) for x in group for part in x.exits)
            rows.append({
                "frame": frame, "arm": arm, "cost": cost_name, "start_ms": start, "end_ms": end,
                "axis": axis, "regime": key, **{k: numbers[k] for k in (
                    "closed", "wins", "losses", "flats", "win_rate", "net_usd", "net_r", "expectancy_r",
                    "profit_factor_usd", "average_win_usd", "average_loss_usd", "worst_loss_usd",
                    "realized_campaign_close_drawdown_usd",
                )}, "win_ci_low": numbers["day_block_bootstrap"]["win_rate_ci95"][0],
                "win_ci_high": numbers["day_block_bootstrap"]["win_rate_ci95"][1],
                "expected_r_ci_low": numbers["day_block_bootstrap"]["expectancy_r_ci95"][0],
                "expected_r_ci_high": numbers["day_block_bootstrap"]["expectancy_r_ci95"][1],
                "realized_modeled_cost_usd": gross_model_cost, "labeled_move_hours": len(included),
                "observed_hours": sum(row["first_observed_ms"] is not None for row in included) if arm != "S5_ARCHIVE" else None,
                "fill_hours": len(delays), "timely_hours": sum(row["timely"] for row in included),
                "median_fill_delay_minutes": median(delays) if delays else None,
                "marked_drawdown_usd": None,
            })
    marks = mark_series(outcomes, bars, start, end)
    rows[0]["marked_drawdown_usd"] = marked_drawdown(marks, initial_equity=marks[0][1])["max_drawdown"]
    return {
        "metrics": rows, "market_events": day_events, "marks_usd": marks,
        "unmatched_observational_hour_sides": outside if arm != "S5_ARCHIVE" else None,
        "observable_hour_sides": denominator if arm != "S5_ARCHIVE" else None,
        "open_at_end": [x for out in outcomes if (x := exposure_at_cutoff(out, bars, end)) is not None],
        "gate_states": dict(Counter(out.state for out in outcomes if start <= bars[out.plan.bar]["time_close"] < end)),
    }


def evaluate(inputs):
    config = json.loads(REGISTRATION.read_bytes())
    dates = config["windows"]
    start, pivot, end = (stamp(dates[key]) for key in (
        "development_start_utc", "archive_chronological_validation_start_utc", "archive_end_utc"))
    windows = {"full_seen": (start, end), "development_seen": (start, pivot), "later_seen": (pivot, end)}
    data = inputs["frames"]
    rules = json.loads(pinned(EVENT_CONFIG))["event"]
    major, complete_hours, _ = independent_hours(data["M5"]["bars"], 5, rules)
    result = {}
    for frame in ("M5", "M15"):
        minutes = int(frame[1:])
        bars = data[frame]["bars"]
        contexts = regime_tape(bars, minutes)
        signals = scan(bars, minutes)
        plans = {"S5_ARCHIVE": [Plan(**row) for row in data[frame]["protected_plans"]]}
        plans.update({name: [offer_plan(x) for x in signals[name]["offers"]] for name in ARMS[1:]})
        replays = {}
        for name in ARMS:
            events = signals[name]["observations"] if name != "S5_ARCHIVE" else []
            replays[name] = {}
            for cost_name, cost in SCENARIOS.items():
                outcomes = execute(plans[name], bars, cost)
                replays[name][cost_name] = {
                    "windows": {key: summarize(outcomes, events, bars, contexts, major, complete_hours,
                                               *limits, frame, name, cost_name)
                                for key, limits in windows.items()},
                    "observations": events, "all_plans": [
                        {**journal(x, bars), "decision_index": x.plan.bar, "units": x.plan.units if x.entry_bar is not None else None,
                         "risk": x.risk_usd, "entry_index": x.entry_bar, "release_index": x.release,
                         "open_equity_marks": x.marked, "regime": contexts[bars[x.plan.bar]["time_close"]]}
                        for x in outcomes],
                }
        result[frame] = replays
    return {"status": "SEEN_ARCHIVE_ONLY", "registration_sha256": sha256(REGISTRATION.read_bytes()).hexdigest(),
            "frames": result, "unavailable": {"H1": "No authenticated native complete A/B tapes",
                                              "holdout": "No independent eligible acquisition"}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate(read_inputs(args.input))
    data = json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(compress(data, mtime=0))


if __name__ == "__main__":
    main()
