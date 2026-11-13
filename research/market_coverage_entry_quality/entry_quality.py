"""Frozen next-open quality gates over original Direct Trend Shadow proposals."""

from collections import Counter, defaultdict
from dataclasses import replace
from math import isclose

from gold_pro.direct_breakout_research import (
    BASE, CAPITAL, MAX_CASH_RISK, MAX_OPEN_MOVE_ATR, STRESS, Execution, drawdown, journal, portfolio, simulate,
    utc,
)
from gold_pro.direct_trend_breakout import (
    CASH_RISK_LIMIT, MAX_EXTENSION_ATR, MAX_FRICTION_R, MAX_HOLD, MAX_STOP_ATR, MIN_NET_ROOM_R,
    MIN_STOP_ATR, UNITS,
)
from gold_pro.replay import Plan
from research.professional_signal_selector.evaluate import cohort_statistics


COSTS = {"normal": BASE, "stress": STRESS}
LANES = {
    "all_standalone": (False, False),
    "continuation_standalone": (True, False),
    "all_protected_priority": (False, True),
    "continuation_protected_priority": (True, True),
}
EPSILON = 1e-9


def source_key(row):
    return row["time_ms"], row["side"], row["model"]


def reject(reasons, condition, label):
    if condition:
        reasons.append(label)


def verify_source(plan, row, bars):
    if (plan.bar, plan.side, plan.model) != (row["bar"], row["side"], row["model"]):
        raise ValueError(f"Source identity changed for {plan.id}")
    if not (row["level_known_ms"] <= row["time_ms"] == bars[plan.bar]["time_close"]):
        raise ValueError(f"Source geometry is not known at confirmation for {plan.id}")
    if (plan.reference, plan.stop, plan.targets, plan.atr) != (
            row["reference"], row["stop"], tuple(row["targets"]), row["prior_atr"]):
        raise ValueError(f"Source geometry changed for {plan.id}")


def next_open_gate(plan, row, bars, costs, continuation):
    verify_source(plan, row, bars)
    first = plan.bar + 1
    decision = row["time_ms"]
    out = {
        "plan_id": plan.id, "time_ms": decision, "side": plan.side, "model": plan.model,
        "signal_close_utc": utc(decision), "continuation": continuation,
        "level": row["level"], "level_known_ms": row["level_known_ms"], "obstacle": row["obstacle"],
        "reference": plan.reference, "stop": plan.stop, "targets": plan.targets, "prior_atr": plan.atr,
        "source_net_room_r": row["net_room_r"], "source_friction_r": row["friction_r"],
        "next_open_ms": None, "next_open_utc": None, "known_ms": decision,
        "midpoint": None, "entry_quote_with_slippage": None, "opening_exit_quote": None,
        "midpoint_stop_atr": None, "absolute_repricing_atr": None, "extension_atr": None,
        "net_room_r": None, "friction_r": None, "all_in_risk_usd": None, "notional_usd": None,
        "est_round_trip_friction_price": None,
    }
    reasons = []
    if first >= len(bars):
        reasons.append("CENSORED_PENDING")
    elif bars[first]["time"] != decision:
        reasons.append("ENTRY_SESSION_GAP")
        out.update(next_open_ms=bars[first]["time"], next_open_utc=utc(bars[first]["time"]),
                   known_ms=bars[first]["time"])
    else:
        opening = bars[first]["open"]
        half_spread = costs.spread / 2
        fill = opening + plan.side * (half_spread + costs.slip)
        exit_quote = opening - plan.side * half_spread
        midpoint_risk = plan.side * (opening - plan.stop)
        fill_risk = plan.side * (fill - plan.stop)
        friction = costs.spread + 2 * costs.slip + 2 * costs.commission / costs.pointvalue
        net_room = None
        if row["obstacle"] is not None and midpoint_risk > 0:
            gross_room = min(plan.side * (plan.targets[0] - opening), plan.side * (row["obstacle"] - opening))
            net_room = (gross_room - friction) / midpoint_risk
        risk = plan.units * ((fill_risk + costs.slip) * costs.pointvalue + 2 * costs.commission)
        notional = plan.units * fill * costs.pointvalue
        out.update(
            next_open_ms=bars[first]["time"], next_open_utc=utc(bars[first]["time"]), known_ms=bars[first]["time"],
            midpoint=opening, entry_quote_with_slippage=fill, opening_exit_quote=exit_quote,
            midpoint_stop_atr=midpoint_risk / plan.atr, absolute_repricing_atr=abs(opening - plan.reference) / plan.atr,
            extension_atr=max(0., plan.side * (opening - row["level"]) / plan.atr),
            net_room_r=net_room, friction_r=friction / midpoint_risk if midpoint_risk > 0 else None,
            all_in_risk_usd=risk, notional_usd=notional, est_round_trip_friction_price=friction,
        )
        reject(reasons, fill_risk < costs.tick or plan.side * (exit_quote - plan.stop) <= 0, "OPEN_THROUGH_STOP")
        reject(reasons, plan.side * (plan.targets[0] - fill) <= 0, "TP1_ALREADY_PASSED")
        reject(reasons, abs(opening - plan.reference) > MAX_OPEN_MOVE_ATR * plan.atr + costs.tick * 1e-6,
               "OPEN_MOVE_GT_0_20_ATR")
        reject(reasons, midpoint_risk < max(costs.tick, MIN_STOP_ATR * plan.atr) - EPSILON, "STOP_TOO_SMALL_AT_OPEN")
        reject(reasons, midpoint_risk > MAX_STOP_ATR * plan.atr + EPSILON, "STOP_TOO_WIDE_AT_OPEN")
        reject(reasons, out["extension_atr"] > MAX_EXTENSION_ATR + EPSILON, "LATE_EXTENSION_AT_OPEN")
        reject(reasons, row["obstacle"] is None, "OBSTACLE_UNKNOWN")
        reject(reasons, row["obstacle"] is not None and plan.side * (row["obstacle"] - fill) <= 0, "OBSTACLE_CROSSED_AT_OPEN")
        reject(reasons, out["friction_r"] is not None and out["friction_r"] > MAX_FRICTION_R + EPSILON, "COST_TOO_HIGH_AT_OPEN")
        reject(reasons, net_room is not None and net_room < MIN_NET_ROOM_R - EPSILON, "INSUFFICIENT_NET_ROOM_AT_OPEN")
        reject(reasons, risk > min(MAX_CASH_RISK, CASH_RISK_LIMIT) or notional > CAPITAL, "RISK_CAP_AT_OPEN")
    return {**out, "passed": not reasons, "reason": reasons[0] if reasons else "GEOMETRY_VALID", "reasons": reasons}


def priority_exit(outcome, bars, costs, protected_start):
    """Preserve the shared path up to the first opening reserved for S5."""
    exit_at = protected_start + 1
    if outcome.entry_bar is None or outcome.release < exit_at or exit_at >= len(bars):
        return outcome
    if exit_at <= outcome.entry_bar:
        raise ValueError("Protected overlap should have blocked this research entry")
    plan, bar = outcome.plan, bars[exit_at]
    side, units = plan.side, plan.units / len(plan.targets)
    opening = bar["open"] - side * costs.spread / 2
    price = opening - side * costs.slip
    reason = "SL_GAP" if side * (opening - plan.stop) <= 0 else "S5_PRIORITY"
    exited = [part for part in outcome.exits if part["bar"] < exit_at]
    sold = {part["leg"] for part in exited}
    for leg in range(1, len(plan.targets) + 1):
        if leg in sold:
            continue
        exited.append({
            "bar": exit_at, "bar_open_utc": utc(bar["time"]), "known_at_utc": utc(bar["time_close"]),
            "leg": leg, "price": price, "reason": reason,
            "net_usd": side * (price - outcome.entry) * costs.pointvalue * units - 2 * costs.commission * units,
        })
    closed = sum(part["net_usd"] for part in exited)
    marks = tuple(point for point in outcome.marked if point[0] < exit_at) + ((exit_at, closed),)
    return replace(outcome, state="CLOSED", release=exit_at, exits=tuple(exited), marked=marks)


def next_protected(index, slots):
    return next((row for row in slots if row[0] > index), None)


def blocked_slot(index, slots):
    return next((row for row in slots if row[0] <= index < row[1]), None)


def control_slots(original, replayed, last_index):
    captured = []
    for start, release, pid in original:
        if not 0 <= start < last_index or not start < release <= last_index:
            raise ValueError(f"Invalid original protected slot: {pid}")
        captured.append((start, release, str(pid)))
    rerun = [(row.plan.bar, row.release, row.plan.id) for row in replayed
             if row.state != "POSITION_OCCUPIED" and row.release > row.plan.bar]
    return sorted(set(captured + rerun))


def economics(outcome, bars, costs):
    if outcome.entry_bar is None:
        return {"gross_midpoint_usd": None, "modeled_total_cost_usd": None, "modeled_commission_usd": None}
    if not outcome.exits:
        return {"gross_midpoint_usd": 0., "modeled_total_cost_usd": 0., "modeled_commission_usd": 0.}
    units = outcome.plan.units / len(outcome.plan.targets)
    side = outcome.plan.side
    opening = bars[outcome.entry_bar]["open"]
    gross = sum(side * (part["price"] + side * (costs.spread / 2 + (0. if part["reason"].startswith("TP") else costs.slip))
                        - opening) * costs.pointvalue * units for part in outcome.exits)
    fees = 2 * costs.commission * units * len(outcome.exits)
    return {"gross_midpoint_usd": gross, "modeled_total_cost_usd": gross - outcome.net, "modeled_commission_usd": fees}


def trade_journal(outcome, bars, costs):
    first = outcome.entry_bar
    closed = outcome.state == "CLOSED"
    return {
        **journal(outcome, bars), **economics(outcome, bars, costs),
        "decision_ms": bars[outcome.plan.bar]["time_close"], "time_ms": bars[outcome.plan.bar]["time_close"],
        "entry_ms": bars[first]["time"] if first is not None else None,
        "release_known_ms": bars[outcome.release]["time_close"] if outcome.release < len(bars) else None,
        "exit_reason": outcome.exits[-1]["reason"] if closed and outcome.exits else None,
        "source_candidate_class": "S5_CONTROL" if outcome.plan.model == "S5_RAW" else "SHADOW_RESEARCH",
        "closed": closed,
    }


def isolated_exclusion(outcome, bars, costs, reason, blocked_by=None):
    state = ("WIN" if outcome.net > 0 else "LOSS" if outcome.net < 0 else "FLAT") if outcome.state == "CLOSED" else outcome.state
    return {
        "plan_id": outcome.plan.id, "side": outcome.plan.side, "model": outcome.plan.model,
        "time_ms": bars[outcome.plan.bar]["time_close"], "reason": reason, "blocked_by": blocked_by,
        "independent_counterfactual_only": True,
        "would_be_entry_open_ms": bars[outcome.plan.bar + 1]["time"] if outcome.plan.bar + 1 < len(bars) else None,
        "counterfactual_outcome": state, "counterfactual": trade_journal(outcome, bars, costs),
    }


def research_portfolio(offers, isolated, bars, costs, slots):
    results, blocked, handoffs = [], [], []
    release = -1
    reserved_at = -1
    occupant = None
    for plan in sorted(offers, key=lambda p: (p.bar, p.id)):
        result = isolated[plan.id]
        if result.entry_bar is None:
            raise ValueError(f"Independent simulator disagrees with passed next-open gate: {plan.id}")
        protected = blocked_slot(plan.bar, slots)
        reason = ("PROTECTED_POSITION_OCCUPIED" if protected else "RESEARCH_SAME_BAR_COLLISION" if plan.bar == reserved_at
                  else "RESEARCH_POSITION_OCCUPIED" if plan.bar < release else None)
        if reason:
            occupied = protected[2] if protected else occupant
            results.append(Execution(plan, reason, plan.bar, blocked_by=occupied))
            record = isolated_exclusion(result, bars, costs, reason, occupied)
            if protected:
                record.update(occupant_started_at_ms=bars[protected[0]]["time_close"],
                              occupant_released_at_ms=bars[protected[1]]["time_close"]
                              if protected[1] < len(bars) else None)
            elif release >= 0:
                record.update(occupant_started_at_ms=bars[reserved_at]["time_close"],
                              occupant_released_at_ms=bars[release]["time_close"] if release < len(bars) else None)
            blocked.append(record)
            continue
        priority = next_protected(plan.bar, slots)
        if priority:
            shortened = priority_exit(result, bars, costs, priority[0])
            if shortened != result:
                handoffs.append({
                    "research_plan_id": plan.id, "protected_plan_id": priority[2],
                    "protected_reservation_close_ms": bars[priority[0]]["time_close"],
                    "effective_exit_open_ms": bars[shortened.release]["time"],
                    "exit_known_close_ms": bars[shortened.release]["time_close"],
                    "reason": shortened.exits[-1]["reason"],
                    "counterfactual_closed": result.state == "CLOSED",
                    "counterfactual_net_usd": result.net if result.state == "CLOSED" else None,
                    "actual_modeled_net_usd": shortened.net,
                })
            result = shortened
        release, reserved_at, occupant = result.release, plan.bar, plan.id
        results.append(result)
    return results, blocked, handoffs


def ledger_closes(outcomes, bars, begin, end, side=0):
    return [row for row in outcomes if row.state == "CLOSED" and (side == 0 or row.plan.side == side)
            and begin <= bars[row.entry_bar]["time"] < end and bars[row.release]["time_close"] <= end]


def mark_to_market(outcomes, bars, begin, end):
    """Combine bars by chart close so an opening handoff does not double-mark cash."""
    changes = defaultdict(float)
    for outcome in outcomes:
        if outcome.entry_bar is None or not begin <= bars[outcome.entry_bar]["time"] < end:
            continue
        prior = 0.
        for index, value in outcome.marked:
            if bars[index]["time_close"] > end:
                break
            changes[index] += value - prior
            prior = value
    balance, marks = 0., []
    for index in sorted(changes):
        balance += changes[index]
        marks.append(balance)
    return drawdown(marks)


def metrics(outcomes, bars, costs, bounds):
    report = cohort_statistics(outcomes, bars, bounds)
    for window, (start, end) in bounds.items():
        for label, side in (("all", 0), ("BUY", 1), ("SELL", -1)):
            closed = ledger_closes(outcomes, bars, start, end, side)
            charges = [economics(row, bars, costs) for row in closed]
            group = [row for row in outcomes if side == 0 or row.plan.side == side]
            report[window][label].update(
                marked_bar_close_drawdown_usd=mark_to_market(group, bars, start, end),
                **{key: sum(item[key] for item in charges) for key in (
                    "gross_midpoint_usd", "modeled_total_cost_usd", "modeled_commission_usd")},
            )
    return report


def summary_exclusions(blocked):
    counts = defaultdict(Counter)
    for row in blocked:
        counts[row["reason"]][row["counterfactual_outcome"]] += 1
    return {key: dict(value) for key, value in sorted(counts.items())}


def portfolio_report(outcomes, exclusions, bars, costs, bounds, handoffs=()):
    return {
        "metrics": metrics(outcomes, bars, costs, bounds),
        "state_counts": dict(Counter(row.state for row in outcomes)),
        "closure_reasons_by_leg": dict(Counter(part["reason"] for row in outcomes for part in row.exits)),
        "priority_exit_campaigns": sum(any(part["reason"] == "S5_PRIORITY" for part in row.exits)
                                       for row in outcomes),
        "protected_priority_handoffs": list(handoffs),
        "journal": [trade_journal(row, bars, costs) for row in outcomes],
        "exclusions": exclusions, "excluded_counterfactuals_by_reason": summary_exclusions(exclusions),
    }


def combined_comparison(approved, research, control_report, bars, costs, bounds):
    mixed = metrics([*approved, *research], bars, costs, bounds)
    keys = ("filled", "closed", "net_usd", "net_r", "expectancy_r", "profit_factor",
            "marked_bar_close_drawdown_usd", "worst_r", "mean_worst_five_percent_r", "modeled_total_cost_usd")
    delta = {}
    for window, cells in mixed.items():
        delta[window] = {}
        for side, cell in cells.items():
            original = control_report[window][side]
            delta[window][side] = {key: cell[key] - original[key] if None not in (cell[key], original[key]) else None for key in keys}
    return {"combined_metrics": mixed, "delta_vs_unchanged_s5": delta, "protected_campaigns_displaced": 0}


def overlap_scenarios(source, continuations, bars, costs, begin, end):
    results = []
    for row in source:
        if row["state"] != "REJECTED" or "OVERLAP_SAME_BAR" not in row.get("reasons", ()):
            continue
        if not begin <= row["time_ms"] < end or not row.get("targets"):
            continue
        plan = Plan(row["plan_id"], row["model"], row["side"], row["bar"], row["reference"], row["stop"],
                    tuple(row["targets"]), row["prior_atr"], MAX_HOLD, UNITS)
        gate = next_open_gate(plan, row, bars, costs, source_key(row) in continuations)
        result = simulate(plan, bars, costs)
        results.append({**isolated_exclusion(result, bars, costs, "ORIGINAL_SHADOW_SAME_BAR_COLLISION"),
                        "would_pass_new_gate_without_original_collision": gate["passed"],
                        "next_open_gate_reasons": gate["reasons"]})
    return results


def evaluate_entries(bars, source_events, source_plans, continuations, controls, native_slots, bounds):
    """Evaluate one frozen next-open policy; original source and protected ledgers are immutable."""
    if not isinstance(bounds, dict):
        bounds = {"full": bounds}
    begin, end = bounds["full"]
    candidates = {row["plan_id"]: row for row in source_events if row["state"] == "ENTRY CANDIDATE"}
    if len(candidates) != sum(row["state"] == "ENTRY CANDIDATE" for row in source_events):
        raise ValueError("Duplicate original Shadow candidate ID")
    original = [plan for plan in source_plans if begin <= bars[plan.bar]["time_close"] < end]
    source_rejects = [row for row in source_events if row["state"] == "REJECTED" and begin <= row["time_ms"] < end]
    source = {
        "original_candidate_count": len(original),
        "original_continuation_candidate_count": sum(source_key(candidates[plan.id]) in continuations for plan in original),
        "original_rejection_count": len(source_rejects),
        "original_reasons": dict(Counter(reason for row in source_rejects for reason in row.get("reasons", (row["reason"],)))),
        "original_rejections": source_rejects,
    }
    evaluated = {}
    for name, costs in COSTS.items():
        control_plans = [plan for plan in controls if begin <= bars[plan.bar]["time_close"] < end]
        approved = portfolio(control_plans, bars, costs, use_s5_lock=True)
        slots = control_slots(native_slots, approved, len(bars))
        gate_rows = [next_open_gate(plan, candidates[plan.id], bars, costs, source_key(candidates[plan.id]) in continuations)
                     for plan in original]
        passed = {row["plan_id"] for row in gate_rows if row["passed"]}
        next_open_rows = {row["plan_id"]: row for row in gate_rows}
        isolated = {plan.id: simulate(plan, bars, costs) for plan in original}
        for row in gate_rows:
            if row["passed"] and not isclose(isolated[row["plan_id"]].risk_usd or -1., row["all_in_risk_usd"], abs_tol=1e-8):
                raise ValueError(f"Quality gate risk disagrees with shared execution: {row['plan_id']}")
        portfolios = {"s5_control": portfolio_report(approved, [], bars, costs, bounds)}
        increments = {}
        for key, (continuation, protected) in LANES.items():
            offers = [plan for plan in original if plan.id in passed
                      and (not continuation or next_open_rows[plan.id]["continuation"])]
            outcomes, blocked, handoffs = research_portfolio(offers, isolated, bars, costs, slots if protected else ())
            portfolios[key] = portfolio_report(outcomes, blocked, bars, costs, bounds, handoffs)
            if protected:
                increments["continuation" if continuation else "all"] = combined_comparison(
                    approved, outcomes, portfolios["s5_control"]["metrics"], bars, costs, bounds)
        rejected = [
            isolated_exclusion(isolated[row["plan_id"]], bars, costs, row["reason"])
            for row in gate_rows if not row["passed"] and row["obstacle"] is not None
            and not set(row["reasons"]) & {"OBSTACLE_CROSSED_AT_OPEN", "OPEN_THROUGH_STOP", "TP1_ALREADY_PASSED"}
        ]
        evaluated[name] = {
            "gate_rows": gate_rows,
            "counters": {
                "eligible_geometry": len(original), "passed": len(passed),
                "continuation_passed": sum(row["passed"] and row["continuation"] for row in gate_rows),
                "next_open_primary_reasons": dict(Counter(row["reason"] for row in gate_rows)),
                "next_open_all_blockers": dict(Counter(reason for row in gate_rows for reason in row["reasons"])),
            },
            "portfolios": portfolios, "protected_increment": increments,
            "original_same_bar_collision_counterfactuals": overlap_scenarios(
                source_rejects, continuations, bars, costs, begin, end),
            "new_gate_rejection_counterfactuals": rejected,
            "new_gate_rejection_counterfactual_counts": summary_exclusions(rejected),
            "protected_slot_count_original_or_replay": len(slots),
        }
    return {
        "scope": {
            "source": "Original normal-cost Shadow candidates only, frozen decision geometry. Stress applies to the same original source universe and cannot introduce a new original signal.",
            "geometry": "At the next contiguous open. Spread applies in bid/ask quotes once. Net room and friction gate use midpoint risk; R economics retain simulator all-in risk.",
            "priority": "One research slot, both original native S5 and same-scenario admitted-plan replay reserve it. Reservations release at confirmed terminal bar closes; a new proposal at that close can use the following open. If the approved reserve starts later, residual research closes at the next observed opening exit quote. S5 is unchanged.",
            "costs": "Gross is a midpoint-equivalent benchmark from modeled order events; difference from net contains spread, slippage and both commissions. Open partials remain in the journal and outside closed P&L.",
            "limits": "Blocked or rejected isolated outcomes are separate ex-post counterfactuals, not admissions or profitable executions. Historical only; no untouched holdout or known broker fills.",
        },
        "source": source, "costs": evaluated,
    }

