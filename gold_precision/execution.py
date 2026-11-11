"""Causal policy replays over frozen FAST proposals."""

from collections import defaultdict
from dataclasses import replace

from gold_pro.replay import Outcome, Part, comparable_units, round_away, simulate


ALLOCATION = {
    "ALLOC_503020": (0.5, 0.3, 0.2),
    "PARTIAL_RUNNER": (0.5, 0, 0.5),
}


def friction_price(cost):
    return cost.spread + 2 * cost.slip + 2 * cost.commission / cost.pointvalue


def capped_plan(plan, cost, budget=20):
    risk = (plan.side * (plan.reference - plan.stop) + friction_price(cost)) * cost.pointvalue
    return replace(plan, units=comparable_units(plan.units, budget / plan.units, risk))


def exit_trigger(policy, plan, bars, features, j, i, fill, tp1, cost):
    side = plan.side
    row, feature = bars[j], features[j]
    close = row["close"]
    behind_fill = side * (close - fill)
    held = j - i + 1
    if policy == "OPPOSITE_MSS":
        return feature.mss and feature.bos == -side
    if policy == "MOMENTUM_FAILURE":
        return (j >= 3 and feature.atr is not None and
                all(side * (bars[k]["close"] - bars[k - 1]["close"]) < 0 for k in (j, j - 1)) and
                side * (close - bars[j - 3]["close"]) <= -.35 * feature.atr)
    if policy == "STRUCTURE_FAILURE":
        anchor = plan.stop + side * .10 * plan.atr
        return side * (close - anchor) <= -cost.tick
    if policy == "ADVERSE_SHOCK":
        return feature.spike and side * (close - row["open"]) < 0 and behind_fill < 0
    if policy == "TIME_FAILURE_12":
        return held >= 12 and not tp1 and behind_fill <= friction_price(cost)
    if policy == "ACTUAL_RISK_20":
        actual = (side * (fill - plan.stop) + friction_price(cost)) * plan.units * cost.pointvalue
        return actual > 20
    if policy == "TIME_24":
        return held >= 24
    if policy == "STRUCTURE_CONTINUATION":
        return tp1 and feature.bos == -side
    return False


def protective_price(policy, plan, cost, feature, close, fill, i, j):
    side = plan.side
    if policy in ("COST_BE", "COST_LOCK_005"):
        initial_r = side * (fill - plan.stop)
        cushion = cost.spread + 2 * cost.commission / cost.pointvalue + cost.slip
        if policy == "COST_LOCK_005":
            cushion += .05 * initial_r
        return round_away(fill + side * cushion, cost.tick, side)
    if policy == "ATR_TRAIL_150" and feature.atr:
        return round_away(close - side * 1.5 * feature.atr, cost.tick, -side)
    if policy == "SWING_TRAIL" and feature.atr:
        newest = feature.newest_low if side == 1 else feature.newest_high
        swings = feature.lows if side == 1 else feature.highs
        if newest is not None and j - 2 >= i:
            return round_away(newest - side * .10 * feature.atr, cost.tick, -side)
        if swings and swings[-1][0] >= i:
            return round_away(swings[-1][1] - side * .10 * feature.atr, cost.tick, -side)
    return None


def policy_simulate(plan, bars, features, cost, minutes, policy):
    """Settle eligible orders before using the current confirmed close."""
    if policy == "FIXED":
        return simulate(plan, bars, cost, minutes)

    # Share the immutable fill model and leave its invalid gap settlement unchanged.
    original = simulate(plan, bars, cost, minutes)
    i, fill = original.entry_bar, original.fill
    if i is None:
        return original
    side = plan.side
    risk = side * (fill - plan.stop)
    invalid_fill = (risk < cost.tick or risk > 3 * plan.atr or
                    abs(fill - plan.reference) > .60 * plan.atr or side * (plan.targets[0] - fill) <= 0)
    if invalid_fill:
        return original

    proportions = ALLOCATION.get(policy, (1 / 3, 1 / 3, 1 / 3))
    legs = [(n, price, plan.units * portion)
            for n, (price, portion) in enumerate(zip(plan.targets, proportions), 1) if portion > 0]
    exits = []
    stop, pending_exit = plan.stop, None
    tp1 = False
    for j in range(i, len(bars)):
        row = bars[j]
        opening = row["open"]
        adverse = row["low"] if side == 1 else row["high"]
        favorable = row["high"] if side == 1 else row["low"]

        def settle(leg, price, reason):
            units = leg[2]
            commission = 2 * cost.commission * units
            net = side * (price - fill) * units * cost.pointvalue
            net -= commission + cost.spread * cost.pointvalue * units
            exits.append(Part(j, price, units, reason, commission, net))
            legs.remove(leg)

        if pending_exit or side * (opening - stop) <= 0:
            for leg in legs[:]:
                settle(leg, opening - side * cost.slip, pending_exit or "STOP_GAP")
        else:
            for leg in legs[:]:
                if side * (opening - leg[1]) >= 0:
                    settle(leg, leg[1], f"TP{leg[0]}_AT_OPEN")
                    tp1 |= leg[0] == 1
            if side * (adverse - stop) <= 0:
                for leg in legs[:]:
                    settle(leg, stop - side * cost.slip, "STOP")
            else:
                for leg in legs[:]:
                    if side * (favorable - leg[1]) >= 0:
                        settle(leg, leg[1], f"TP{leg[0]}")
                        tp1 |= leg[0] == 1
        if not legs:
            return Outcome(plan, "CLOSED", j, i, fill, tuple(exits))

        # No closing-bar decision can affect the fills settled above.
        if j - i + 1 >= plan.max_hold:
            pending_exit = "TIME_EXIT"
        elif exit_trigger(policy, plan, bars, features, j, i, fill, tp1, cost):
            pending_exit = policy
        elif tp1:
            new_stop = protective_price(policy, plan, cost, features[j], row["close"], fill, i, j)
            if (new_stop is not None and side * (new_stop - stop) > 0 and
                    side * (row["close"] - new_stop) >= cost.tick):
                stop = new_stop
    return Outcome(plan, "CENSORED_OPEN", len(bars), i, fill, tuple(exits))


def book(plans, cost, equity, *, outcomes, recovery_first=()):
    """Reserve capacity chronologically and settle realized cash at release."""
    by_bar = defaultdict(list)
    for plan in plans:
        by_bar[plan.bar].append(plan)
    preferred = set(recovery_first)
    accepted, rejected = [], []
    balance = equity
    occupied = None
    for bar, candidates in sorted(by_bar.items()):
        if occupied is not None and bar >= occupied.release:
            balance += occupied.net
            occupied = None
        conflict = len({plan.side for plan in candidates}) > 1
        ordering = lambda p: (p.model != "S5", p.id not in preferred, p.model, p.id)
        used_close = False
        for plan in sorted(candidates, key=ordering):
            price_risk = (plan.side * (plan.reference - plan.stop) + friction_price(cost)) * cost.pointvalue
            if occupied is not None or used_close:
                rejected.append((plan, "POSITION_OCCUPIED"))
            elif conflict and plan.model != "S5":
                rejected.append((plan, "OPPOSING_SIGNAL"))
            elif plan.units * price_risk > equity * .0025:
                rejected.append((plan, "RISK_CAP"))
            elif plan.units * plan.reference * cost.pointvalue > balance:
                rejected.append((plan, "CAPITAL"))
            else:
                result = outcomes[plan.id]
                if result.fill is not None and result.fill * plan.units * cost.pointvalue > balance:
                    result = Outcome(plan, "CAPITAL_AT_FILL", result.entry_bar)
                if result.state in {"RISK_INVALID", "CAPITAL_AT_FILL"}:
                    rejected.append((plan, result.state))
                accepted.append(result)
                if result.state != "RISK_INVALID":
                    occupied = result
                    used_close = True
    return accepted, rejected
