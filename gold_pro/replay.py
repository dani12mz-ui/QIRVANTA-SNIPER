"""Conservative OHLC replay for frozen research proposals."""

from collections import defaultdict
from dataclasses import dataclass, replace
from math import ceil, floor, isfinite


@dataclass(frozen=True)
class Plan:
    id: str
    model: str
    side: int
    bar: int
    reference: float
    stop: float
    targets: tuple[float, float, float]
    atr: float
    max_hold: int
    units: float = 3
    regime: str | None = None


@dataclass(frozen=True)
class Part:
    bar: int
    price: float
    units: float
    reason: str
    commission: float
    net: float


@dataclass(frozen=True)
class Outcome:
    plan: Plan
    state: str
    release: int
    entry_bar: int | None = None
    fill: float | None = None
    exits: tuple[Part, ...] = ()

    @property
    def net(self):
        return sum(part.net for part in self.exits)


@dataclass(frozen=True)
class Costs:
    tick: float = .001
    pointvalue: float = 1.
    commission: float = .175
    ticks: int = 2
    spread: float = .35
    extra_spread: float = .30
    extra_slip: float = .10
    extra_commission: float = .50

    @property
    def slip(self):
        return self.ticks * self.tick


def round_away(price, tick, side):
    scale = price / tick
    return round((ceil(scale - 1e-8) if side == 1 else floor(scale + 1e-8)) * tick, 9)


def comparable_units(units, old_risk, new_risk):
    """Round down to three equal 0.01-unit legs with no leverage increase."""
    return min(units, floor((units * old_risk / new_risk + 1e-10) / .03) * .03)


def buffered(plan, extra_atr, costs):
    risk = plan.side * (plan.reference - plan.stop)
    stop = round_away(plan.stop - plan.side * extra_atr * plan.atr, costs.tick, -plan.side)
    new_risk = plan.side * (plan.reference - stop)
    targets = tuple(round_away(plan.reference + (i + 1) * plan.side * new_risk, costs.tick, plan.side)
                    for i in range(3))
    return replace(plan, stop=stop, targets=targets, units=comparable_units(plan.units, risk, new_risk))


def _fill(plan, bars, cost, minutes, method, amount):
    side = plan.side
    risk = side * (plan.reference - plan.stop)
    first = plan.bar + 1
    if first >= len(bars):
        return None, None, len(bars), False, "CENSORED_PENDING"
    if method == "market":
        return first, bars[first]["open"] + side * cost.slip, first, True, "FILLED"
    sign = 1 if method == "stop" else -1
    trigger = round_away(plan.reference + sign * side * amount * risk, cost.tick, sign * side)
    expires = bars[plan.bar]["time_close"] + 3 * minutes * 60_000
    for j in range(first, len(bars)):
        bar = bars[j]
        if bar["time"] >= expires:
            return None, None, j, False, "ENTRY_EXPIRED"
        opening = bar["open"]
        if sign * side * (opening - trigger) >= 0:
            fill = opening + side * cost.slip if method == "stop" else trigger
            return j, fill, j, True, "FILLED"
        extreme = bar["high"] if sign * side == 1 else bar["low"]
        penetration = 0 if method == "stop" else cost.tick
        if sign * side * (extreme - trigger) >= penetration - 1e-8:
            fill = trigger + side * cost.slip if method == "stop" else trigger
            return j, fill, j, False, "FILLED"
        if bar["time_close"] >= expires:
            return None, None, j, False, "ENTRY_EXPIRED"
    return None, None, len(bars), False, "CENSORED_PENDING"


def simulate(plan, bars, cost, minutes, *, method="market", entry_amount=0., exit_policy="fixed"):
    risk = plan.side * (plan.reference - plan.stop)
    if (plan.side not in (-1, 1) or not isfinite(risk) or risk < cost.tick or plan.units < .03
            or len(plan.targets) != 3 or any(plan.side * (t - plan.reference) <= 0 for t in plan.targets)):
        return Outcome(plan, "RISK_INVALID", plan.bar)
    i, fill, release, at_open, state = _fill(plan, bars, cost, minutes, method, entry_amount)
    if i is None:
        return Outcome(plan, state, release)
    side, stop = plan.side, plan.stop
    units = plan.units / 3
    charge = 2 * cost.commission * units
    spread = cost.spread * cost.pointvalue * units
    legs, exits = list(enumerate(plan.targets, 1)), []
    actual_risk = side * (fill - stop)
    hit_first = False
    pending_exit = None
    maximum_deviation = .6 * plan.atr
    invalid_fill = actual_risk < cost.tick or actual_risk > 3 * plan.atr or abs(fill - plan.reference) > maximum_deviation
    invalid_fill |= side * (plan.targets[0] - fill) <= 0
    for j in range(i, len(bars)):
        row = bars[j]
        opening, close = row["open"], row["close"]
        adverse = row["low"] if side > 0 else row["high"]
        favorable = row["high"] if side > 0 else row["low"]
        safe_open = j > i or at_open
        allow_targets = safe_open and (j > i or not invalid_fill)

        def settle(leg, price, reason):
            net = side * (price - fill) * units * cost.pointvalue - charge - spread
            exits.append(Part(j, price, units, reason, charge, net))
            legs.remove(leg)

        if pending_exit or (safe_open and side * (opening - stop) <= 0):
            for leg in legs[:]:
                settle(leg, opening - side * cost.slip, pending_exit or "STOP_GAP")
        else:
            if allow_targets:
                for leg in legs[:]:
                    if side * (opening - leg[1]) >= 0:
                        settle(leg, leg[1], f"TP{leg[0]}_AT_OPEN")
                        hit_first |= leg[0] == 1
            if side * (adverse - stop) <= 0:
                for leg in legs[:]:
                    settle(leg, stop - side * cost.slip, "STOP")
            elif allow_targets:
                for leg in legs[:]:
                    if side * (favorable - leg[1]) >= 0:
                        settle(leg, leg[1], f"TP{leg[0]}")
                        hit_first |= leg[0] == 1
        if not legs:
            return Outcome(plan, "CLOSED", j, i, fill, tuple(exits))
        # An intrabar fill cannot earn a target from an earlier, unordered extreme.
        if j == i and invalid_fill:
            pending_exit = "GAP_OR_FILL_INVALID"
        elif j - i + 1 >= plan.max_hold or (exit_policy == "stall" and not hit_first and j - i + 1 >= 24):
            pending_exit = "TIME_EXIT"
        elif hit_first and exit_policy in {"be", "lock"} and actual_risk >= cost.tick:
            buffer = cost.spread + 2 * cost.commission / cost.pointvalue + cost.slip
            if exit_policy == "lock":
                buffer += .05 * actual_risk
            proposed = round_away(fill + side * buffer, cost.tick, side)
            if side * (proposed - stop) > 0 and side * (close - proposed) >= cost.tick:
                stop = proposed
    return Outcome(plan, "CENSORED_OPEN", len(bars), i, fill, tuple(exits))


def portfolio(plans, bars, costs, minutes, equity, *, frozen=None, **execution):
    """Recompute chronological occupancy, including reserved unfilled orders."""
    results, rejections = [], []
    occupied_until = -1
    balance = equity
    frozen = frozen or {}
    by_bar = defaultdict(list)
    for plan in plans:
        by_bar[plan.bar].append(plan)
    for bar, candidates in sorted(by_bar.items()):
        conflict = len({p.side for p in candidates}) > 1
        used_this_close = False
        for plan in sorted(candidates, key=lambda p: (p.model != "S5", p.model, p.id)):
            risk = plan.side * (plan.reference - plan.stop)
            risk_cost = (risk + costs.spread + 2 * costs.slip) * costs.pointvalue + 2 * costs.commission
            if bar < occupied_until or used_this_close:
                rejections.append((plan, "POSITION_OCCUPIED"))
            elif conflict and plan.model != "S5":
                rejections.append((plan, "OPPOSING_SIGNAL"))
            elif plan.units * risk_cost > equity * .0025:
                rejections.append((plan, "RISK_CAP"))
            elif plan.units * plan.reference * costs.pointvalue > balance:
                rejections.append((plan, "CAPITAL"))
            else:
                result = frozen[plan.id] if plan.id in frozen else simulate(plan, bars, costs, minutes, **execution)
                if result.fill is not None and result.fill * plan.units * costs.pointvalue > balance:
                    result = Outcome(plan, "CAPITAL_AT_FILL", result.entry_bar)
                if result.state in {"RISK_INVALID", "CAPITAL_AT_FILL"}:
                    rejections.append((plan, result.state))
                results.append(result)
                if result.state != "RISK_INVALID":
                    occupied_until = result.release
                    used_this_close = True
                    balance += result.net
    return results, rejections
