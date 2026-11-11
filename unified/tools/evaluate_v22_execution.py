"""One-slot V2.2 campaign model with frozen native S5 proposals."""

from collections import defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal
from math import floor

from unified.tools.evaluate_v21_impulse import Exit, observed_offers, round_tick

POLICIES = {
    "FIXED": "Original 1R / 2R / 3R",
    "COST_BE": "Cost-adjusted break-even after TP1",
    "LOCK": "0.05 actual R lock after TP1",
    "ATR_TRAIL": "2 ATR trailing stop",
    "SWING_TRAIL": "Confirmed swing trailing stop",
    "TIME_24": "24-bar exit",
    "MOMENTUM_EXIT": "Adverse closes and RSI deterioration",
}
ZERO = Decimal(0)


@dataclass(frozen=True)
class Plan:
    id: str
    model: str
    side: int
    bar: int
    time: int
    watch_time: int
    reference: float
    stop: float
    targets: tuple[float, ...]
    prior_atr: float
    risk_price: float
    cost_to_risk: float | None
    net_room_r: float | None


@dataclass(frozen=True)
class Campaign:
    id: str
    engine: str
    side: int
    decision_bar: int
    decision_time: int
    entry_bar: int
    entry_time: int | None
    entry_price: float | None
    units: Decimal
    initial_risk: Decimal | None
    initial_stop: float
    current_stop: float
    targets: tuple[float, ...]
    exits: tuple[Exit, ...]
    complete: bool
    gap_invalid: bool
    stop_changes: tuple[dict, ...]
    cancelled_at_entry: bool = False

    @property
    def release(self):
        if self.complete:
            return self.exits[-1].bar
        return self.entry_bar if self.cancelled_at_entry else float("inf")

    @property
    def cash(self):
        return sum((part.net for part in self.exits), ZERO)


def make_plan(c, execution, tick, pointvalue):
    risk_settings = execution["risk_gates"]
    side = c.side
    reference = round(floor(c.reference / tick + .5) * tick, 9)
    stop = round_tick(c.stop_base - side * risk_settings["stop_buffer_atr"] * c.atr, tick, -side)
    risk = round(side * (reference - stop), 9)
    targets = tuple(round_tick(reference + side * r * risk, tick, side) for r in execution["fixed_reward_r"])
    fee = execution["native_commission_usd_per_unit_per_fill"]
    slip = execution["market_and_stop_adverse_slippage_ticks"] * tick
    budget = risk_settings["planned_spread_price"] + 2 * fee / float(pointvalue) + 2 * slip
    forward = side * (c.barrier - reference) if c.barrier is not None else None
    room = min(risk, forward) if forward is not None and forward > 0 else risk
    cost_ratio = budget / risk if risk > 0 else None
    net_room_r = (room - budget) / risk if risk > 0 else None
    gates = (
        (risk < tick, "STRUCTURE_INVALID"),
        (c.spike, "VOLATILITY_SPIKE"),
        (c.htf_conflict, "HTF_CONFLICT"),
        (c.extension > risk_settings["maximum_extension_atr"], "EXCESSIVE_EXTENSION"),
        (risk > risk_settings["maximum_stop_atr"] * c.atr, "OVERSIZED_STOP"),
        (cost_ratio is not None and cost_ratio > risk_settings["maximum_cost_to_initial_risk"], "COST_TOO_HIGH"),
        (net_room_r is not None and net_room_r < risk_settings["minimum_net_barrier_room_r"], "INSUFFICIENT_ROOM"),
    )
    reasons = tuple(reason for failed, reason in gates if failed)
    plan_id = f"{c.model}-{'BUY' if side == 1 else 'SELL'}-{c.watch_time}|{c.time}"
    plan = Plan(plan_id, c.model, side, c.index, c.time,
                c.watch_time, reference, stop, targets, c.atr, risk, cost_ratio, net_room_r)
    return plan, reasons


def scheduled_protection(policy, plan, bars, features, i, fill_time, fill, actual_risk, tp1_filled,
                         close, execution, tick, pointvalue):
    f, side = features[i], plan.side
    if policy == "COST_BE" and tp1_filled:
        fee = execution["native_commission_usd_per_unit_per_fill"]
        slip = execution["market_and_stop_adverse_slippage_ticks"] * tick
        spread = execution["risk_gates"]["planned_spread_price"]
        return round_tick(fill + side * (spread + 2 * fee / float(pointvalue) + slip), tick, side)
    if policy == "LOCK" and tp1_filled:
        return round_tick(fill + side * .05 * actual_risk, tick, -side)
    if policy == "ATR_TRAIL" and f.atr:
        return round_tick(close - side * 2 * f.atr, tick, -side)
    if policy == "SWING_TRAIL" and f.atr:
        pivots = f.lows if side == 1 else f.highs
        just_confirmed = f.newest_low if side == 1 else f.newest_high
        latest = just_confirmed if just_confirmed is not None else pivots[-1][1] if pivots else None
        disclosed_at = (bars[i]["time_close"] if just_confirmed is not None else
                        bars[pivots[-1][0] + 2]["time_close"] if pivots else None)
        if latest is not None and disclosed_at >= fill_time:
            buffer = execution["risk_gates"]["stop_buffer_atr"] * f.atr
            return round_tick(latest - side * buffer, tick, -side)
    return None


def simulate(plan, bars, features, execution, tick, pointvalue, minutes, policy):
    """Three equal legs. Apply all close decisions only on a later available tick."""
    side = plan.side
    i = plan.bar + 1
    units = Decimal(str(execution["position_units"]))
    leg_units = units / Decimal(len(plan.targets))
    slip = execution["market_and_stop_adverse_slippage_ticks"] * tick
    commission = 2 * leg_units * Decimal(str(execution["native_commission_usd_per_unit_per_fill"]))
    entry_time = bars[i]["time"] if i < len(bars) else None
    fill = round(bars[i]["open"] + side * slip, 9) if entry_time is not None else None
    actual_risk = round(side * (fill - plan.stop), 9) if fill is not None else None
    initial_risk = (Decimal(str(actual_risk)) * units * pointvalue
                    if actual_risk is not None and actual_risk >= tick else None)
    settings = execution["risk_gates"]
    gap_invalid = bool(fill is not None and (actual_risk < tick or
        actual_risk > settings["maximum_stop_atr"] * plan.prior_atr or
        abs(fill - plan.reference) > settings["maximum_fill_deviation_atr"] * plan.prior_atr or
        side * (plan.targets[0] - fill) <= 0))
    base_hold = 60 if minutes == 30 else 48
    live = list(enumerate(plan.targets, start=1))
    exits, stop_changes = [], []
    stop, queued_market_exit, tp1_filled = plan.stop, None, False

    for j in range(i, len(bars)):
        b = bars[j]
        op, high, low, close = (b[k] for k in ("open", "high", "low", "close"))
        adverse, favorable = (low, high) if side == 1 else (high, low)

        def close_leg(number, price, reason):
            gross = Decimal(str(round(side * (price - fill), 9))) * leg_units * pointvalue
            exits.append(Exit(j, b["time"], round(price, 9), leg_units, commission, gross - commission, reason))
            nonlocal tp1_filled
            tp1_filled |= number == 1 and reason.startswith("TP")

        if queued_market_exit:
            for number, _ in live:
                close_leg(number, op - side * slip, queued_market_exit)
            live.clear()
        elif side * (op - stop) <= 0:
            for number, _ in live:
                close_leg(number, op - side * slip, "STOP_GAP")
            live.clear()
        else:
            # A favorable limit gap is established at the open before ambiguous extremes.
            gap_parts = [(number, target) for number, target in live if side * (op - target) >= 0]
            for number, target in gap_parts:
                close_leg(number, op, f"TP{number}_GAP")
                live.remove((number, target))
            if side * (adverse - stop) <= 0:
                for number, _ in live:
                    close_leg(number, stop - side * slip, "STOP")
                live.clear()
            else:
                touched = [(number, target) for number, target in live if side * (favorable - target) >= 0]
                for number, target in touched:
                    close_leg(number, target, f"TP{number}")
                    live.remove((number, target))
        if not live:
            break
        if j == i and gap_invalid:
            queued_market_exit = "GAP_INVALID"
        elif j - i >= (24 if policy == "TIME_24" and not tp1_filled else base_hold):
            queued_market_exit = "TIME_EXIT"
        elif policy == "MOMENTUM_EXIT" and j > i and b["time"] == bars[j - 1]["time_close"]:
            rsi = features[j].rsi
            adverse_pair = max(side * (close - fill), side * (bars[j - 1]["close"] - fill)) < 0
            if rsi is not None and side * (rsi - 50) < 0 and adverse_pair:
                queued_market_exit = "MOMENTUM_EXIT"
        if queued_market_exit is None:
            proposed = scheduled_protection(policy, plan, bars, features, j, entry_time, fill, actual_risk,
                                            tp1_filled, close, execution, tick, pointvalue)
            if (proposed is not None and side * (proposed - stop) >= tick * .5
                    and side * (close - proposed) >= tick * .5):
                stop_changes.append({"known_close": b["time_close"], "previous_stop": stop, "new_stop": proposed,
                                     "earliest_effective_open": bars[j + 1]["time"] if j + 1 < len(bars) else None})
                stop = proposed
    return Campaign(plan.id, plan.model, side, plan.bar, plan.time, i, entry_time, fill, units, initial_risk,
                    plan.stop, stop, plan.targets, tuple(exits), not live, gap_invalid, tuple(stop_changes))


def s5_campaigns(campaigns):
    return [
        Campaign(t.id, t.engine, t.side, t.decision_bar, t.decision_time, t.entry_bar, t.entry_time, t.entry_price,
                 t.units, t.initial_risk, t.stop, t.stop, (), t.exits, True, False, ())
        for t in observed_offers(campaigns)
    ]


def replay(offers, bars, initial_capital, pointvalue, first="S5"):
    """Reserve one slot and one cash ledger across all enabled sources."""
    by_bar = defaultdict(list)
    for offer in offers:
        by_bar[offer.decision_bar].append(offer)
    active = None
    admitted, rejected = [], []
    cash = Decimal(str(initial_capital))
    for i, pending in sorted(by_bar.items()):
        if active is not None and i >= active.release:
            cash += active.cash
            active = None
        candidates = [offer for offer in pending if offer.engine != "S5"]
        opposite = len({offer.side for offer in candidates}) > 1
        pending.sort(key=lambda offer: (offer.engine != "S5") if first == "S5" else (offer.engine == "S5"))
        same_bar = None
        for offer in pending:
            reason, occupant = None, None
            if active is not None:
                reason, occupant = "POSITION_OCCUPIED", active
            elif offer.engine != "S5" and opposite:
                primary = candidates[0]
                reason = ("POSITION_CONFLICT" if offer is primary or offer.side != primary.side
                          else "PORTFOLIO_PRIORITY")
            elif same_bar is not None:
                reason, occupant = "PORTFOLIO_PRIORITY", same_bar
            elif Decimal(str(bars[i]["close"])) * offer.units * pointvalue > cash:
                reason = "CAPITAL"
            if reason is not None:
                rejected.append({"id": offer.id, "side": offer.side, "model": offer.engine,
                                 "bar": i, "time": offer.decision_time, "state": "ENTRY_REJECTED",
                                 "reason": reason, "occupant_model": occupant.engine if occupant else None,
                                 "occupant_id": occupant.id if occupant else None,
                                 "known_offer_outcome_usd": offer.cash if offer.complete else None})
            else:
                chosen = offer
                if offer.entry_price is not None and Decimal(str(offer.entry_price)) * offer.units * pointvalue > cash:
                    # A next-open margin failure cannot free the slot on the submitting close.
                    chosen = replace(offer, entry_time=None, entry_price=None, initial_risk=None, exits=(),
                                     complete=False, gap_invalid=False, stop_changes=(), cancelled_at_entry=True)
                    rejected.append({"id": offer.id, "side": offer.side, "model": offer.engine,
                                     "bar": offer.entry_bar, "time": offer.entry_time, "state": "ENTRY_FAILED",
                                     "reason": "CAPITAL_AT_FILL", "occupant_model": None, "occupant_id": None,
                                     "known_offer_outcome_usd": None})
                admitted.append(chosen)
                same_bar = chosen
        active = same_bar if same_bar is not None else active
    return admitted, rejected
