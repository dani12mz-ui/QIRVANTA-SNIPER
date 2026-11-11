"""Closed-bar Direct Trend Breakout research. Produces diagnostic candidates only."""

from collections import deque
from dataclasses import dataclass, replace
from math import isfinite

from research.data.loader import validate_bars
from research.features.causal import Smooth

from .replay import Costs, Plan, round_away


STRUCTURAL = "STRUCTURAL_BREAK"
CONSOLIDATION = "CONSOLIDATION_BREAK"
RETEST = "RETEST_CONTINUATION"
MODELS = (STRUCTURAL, CONSOLIDATION, RETEST)
SIDES = (1, -1)
PIVOT_RADIUS = 2
CONSOLIDATION_BARS = 4
WINDOW_BARS = 6
BREAK_BUFFER_ATR = .05
WATCH_DISTANCE_ATR = .30
BREAK_BODY_ATR = .50
BREAK_TERMINAL_RANGE = .35
CONSOLIDATION_MAX_ATR = 1.50
RETEST_TOUCH_ATR = .20
RETEST_CLOSE_ATR = .10
RETEST_BODY_ATR = .15
STOP_BUFFER_ATR = .10
MIN_STOP_ATR = .25
MAX_STOP_ATR = 2.50
MAX_EXTENSION_ATR = 1.00
MAX_FRICTION_R = .30
MIN_NET_ROOM_R = .70
UNITS = 3
CASH_RISK_LIMIT = 250.
MAX_HOLD = 48


@dataclass(frozen=True)
class Pivot:
    price: float
    source_ms: int
    known_ms: int


@dataclass(frozen=True)
class Context:
    prior_atr: float | None
    prior_ema: float | None
    prior_ema_slope: float | None
    known_highs: tuple[Pivot, ...]
    known_lows: tuple[Pivot, ...]
    confirmed_high: Pivot | None
    confirmed_low: Pivot | None
    segment_bars: int


@dataclass(frozen=True)
class Setup:
    model: str
    side: int
    level: float
    level_known_ms: int
    source_ms: int
    first_bar: int
    first_ms: int
    breakout_ms: int | None = None
    origin_model: str | None = None

    @property
    def identity(self):
        return self.model, self.side, self.source_ms, self.level


def closed_context(bars):
    """Yield prior inputs and pivots that become known at this bar's close."""
    validate_bars(bars)
    atr, ema = Smooth(14, wilder=True), Smooth(20)
    ema_history = deque(maxlen=4)
    highs, lows = deque(maxlen=8), deque(maxlen=8)
    segment_bars = 0
    for i, bar in enumerate(bars):
        previous = bars[i - 1] if i else None
        segment_bars = (segment_bars + 1 if previous and bar["time"] == previous["time_close"] else 1)
        new_high = new_low = None
        if segment_bars > 2 * PIVOT_RADIUS:
            center = bars[i - PIVOT_RADIUS]
            neighbors = bars[i - 2 * PIVOT_RADIUS:i - PIVOT_RADIUS] + bars[i - PIVOT_RADIUS + 1:i + 1]
            if all(center["high"] > other["high"] for other in neighbors):
                new_high = Pivot(center["high"], center["time"], bar["time_close"])
            if all(center["low"] < other["low"] for other in neighbors):
                new_low = Pivot(center["low"], center["time"], bar["time_close"])
        slope = None
        if len(ema_history) == 4 and ema_history[0] is not None:
            slope = ema_history[-1] - ema_history[0]
        yield Context(atr.value, ema.value, slope, tuple(highs), tuple(lows),
                      new_high, new_low, segment_bars)
        if new_high:
            highs.append(new_high)
        if new_low:
            lows.append(new_low)
        previous_close = previous["close"] if previous else bar["close"]
        atr.add(max(bar["high"] - bar["low"], abs(bar["high"] - previous_close),
                    abs(bar["low"] - previous_close)))
        ema_history.append(ema.add(bar["close"]))


def _setups(bars, index, ctx, side):
    bar, previous = bars[index], bars[index - 1]
    defended, directional = (ctx.known_highs, ctx.known_lows) if side == 1 else (ctx.known_lows, ctx.known_highs)
    if defended and len(directional) >= 2 and side * (directional[-1].price - directional[-2].price) > 0:
        pivot = defended[-1]
        yield Setup(STRUCTURAL, side, pivot.price, pivot.known_ms, pivot.source_ms, index, bar["time_close"])
    if ctx.segment_bars <= CONSOLIDATION_BARS or ctx.prior_ema_slope is None:
        return
    if min(side * (previous["close"] - ctx.prior_ema), side * ctx.prior_ema_slope) <= 0:
        return
    box = bars[index - CONSOLIDATION_BARS:index]
    high_bar = max(box, key=lambda row: row["high"])
    low_bar = min(box, key=lambda row: row["low"])
    high, low = high_bar["high"], low_bar["low"]
    if high - low <= CONSOLIDATION_MAX_ATR * ctx.prior_atr:
        level, source = (high, high_bar) if side == 1 else (low, low_bar)
        yield Setup(CONSOLIDATION, side, level, previous["time_close"], source["time"], index, bar["time_close"])


def _plan(bars, index, ctx, setup, costs):
    bar = bars[index]
    side, atr, close = setup.side, ctx.prior_atr, bar["close"]
    anchor_bars = {STRUCTURAL: 3, CONSOLIDATION: CONSOLIDATION_BARS + 1, RETEST: 2}
    window = bars[index + 1 - min(anchor_bars[setup.model], ctx.segment_bars):index + 1]
    stop_base = min(x["low"] for x in window) if side == 1 else max(x["high"] for x in window)
    stop = round_away(stop_base - side * STOP_BUFFER_ATR * atr, costs.tick, -side)
    risk = round(side * (close - stop), 9)
    friction = costs.spread + 2 * costs.slip + 2 * costs.commission / costs.pointvalue
    extension = max(0., side * (close - setup.level) / atr)
    obstacles = ctx.known_highs if side == 1 else ctx.known_lows
    remaining = min((side * (p.price - close) for p in obstacles if side * (p.price - close) > 0), default=None)
    net_room = (min(risk, remaining) - friction) / risk if risk > 0 and remaining is not None else (
        (risk - friction) / risk if risk > 0 else None)
    risk_cash = (risk + friction) * costs.pointvalue * UNITS
    reasons = []
    if risk < max(costs.tick, MIN_STOP_ATR * atr):
        reasons.append("STOP_TOO_SMALL")
    if risk > MAX_STOP_ATR * atr:
        reasons.append("STOP_TOO_WIDE")
    if extension > MAX_EXTENSION_ATR:
        reasons.append("LATE_EXTENSION")
    if risk > 0 and friction > MAX_FRICTION_R * risk:
        reasons.append("COST_TOO_HIGH")
    if net_room is not None and net_room < MIN_NET_ROOM_R:
        reasons.append("INSUFFICIENT_NET_ROOM")
    if risk_cash > CASH_RISK_LIMIT:
        reasons.append("CASH_RISK_LIMIT")
    targets = tuple(round_away(close + side * step * risk, costs.tick, side) for step in (1, 2, 3)) if risk > 0 else ()
    plan_id = f"DT|{setup.model}|{side}|{bar['time_close']}"
    details = dict(
        plan_id=plan_id, reference=close, stop=stop, targets=targets, risk=risk, risk_cash=risk_cash,
        extension_atr=extension, friction_r=friction / risk if risk > 0 else None, net_room_r=net_room,
        obstacle=close + side * remaining if remaining is not None else None, reasons=reasons,
    )
    plan = Plan(plan_id, setup.model, side, index, close, stop, targets, atr, MAX_HOLD, UNITS)
    return details, plan if not reasons else None


def _expired(setup, index, bar, interval):
    return (index - setup.first_bar > WINDOW_BARS
            or bar["time_close"] - setup.first_ms > WINDOW_BARS * interval)


def scan(bars, costs=Costs(), *, enabled=None):
    """Return known-at-close observations and diagnostic plans with frozen geometry."""
    enabled = frozenset(MODELS if enabled is None else enabled)
    if not enabled or not enabled <= set(MODELS):
        raise ValueError("Invalid Direct Trend Breakout model selection")
    cost_values = (costs.tick, costs.pointvalue, costs.spread, costs.commission, costs.slip)
    if (not all(map(isfinite, cost_values)) or costs.tick <= 0 or costs.pointvalue <= 0
            or min(costs.spread, costs.commission, costs.slip) < 0):
        raise ValueError("Invalid research execution costs")
    observations, plans = [], []
    watched, retests, consumed = {}, {}, set()
    for index, ctx in enumerate(closed_context(bars)):
        bar = bars[index]
        interval = bar["time_close"] - bar["time"]
        atr = ctx.prior_atr
        if atr is None or atr <= 0 or index == 0:
            continue
        close, opening = bar["close"], bar["open"]
        previous_close = bars[index - 1]["close"]
        claimed_sides = set()

        def emit(setup, state, reason, **extra):
            if setup.model in enabled:
                observations.append(dict(
                    time_ms=bar["time_close"], bar=index, side=setup.side, model=setup.model, state=state,
                    reason=reason, level=setup.level, level_known_ms=setup.level_known_ms, source_ms=setup.source_ms,
                    watch_ms=setup.first_ms, breakout_ms=setup.breakout_ms, origin_model=setup.origin_model,
                    prior_atr=atr, close=close, **extra,
                ))

        def geometry(setup):
            if setup.model in enabled:
                details, plan = _plan(bars, index, ctx, setup, costs)
                if plan and setup.side in claimed_sides:
                    details["reasons"] = ["OVERLAP_SAME_BAR"]
                    plan = None
                emit(setup, "ENTRY CANDIDATE" if plan else "REJECTED",
                     "DIAGNOSTIC_ONLY" if plan else details["reasons"][0], **details)
                if plan:
                    claimed_sides.add(setup.side)
                    plans.append(plan)

        for side in SIDES:
            if (retest := retests.get(side)) is not None:
                if ctx.segment_bars == 1 or _expired(retest, index, bar, interval):
                    emit(retest, "EXPIRED", "DATA_GAP" if ctx.segment_bars == 1 else "RETEST_WINDOW_EXPIRED")
                    del retests[side]
                elif side * (close - retest.level) < -RETEST_CLOSE_ATR * atr:
                    emit(retest, "INVALIDATED", "CLOSE_BACK_THROUGH_BREAK_LEVEL")
                    del retests[side]
                elif (abs(bar["low" if side == 1 else "high"] - retest.level) <= RETEST_TOUCH_ATR * atr
                      and side * (close - retest.level) >= RETEST_CLOSE_ATR * atr
                      and side * (close - opening) >= RETEST_BODY_ATR * atr):
                    emit(retest, "BREAKOUT CONFIRMED", "RETEST_CONFIRMED")
                    geometry(retest)
                    del retests[side]

            offers = {setup.model: setup for setup in _setups(bars, index, ctx, side)} if ctx.segment_bars > 1 else {}
            for model in (STRUCTURAL, CONSOLIDATION):
                key = model, side
                setup, watch = offers.get(model), watched.get(key)
                if watch is not None:
                    if ctx.segment_bars == 1 or _expired(watch, index, bar, interval):
                        emit(watch, "EXPIRED", "DATA_GAP" if ctx.segment_bars == 1 else "WATCH_WINDOW_EXPIRED")
                        del watched[key]
                    elif setup is None or setup.identity != watch.identity:
                        emit(watch, "INVALIDATED", "STRUCTURE_CHANGED")
                        del watched[key]
                    if key not in watched:
                        consumed.add(watch.identity)
                        watch = None
                if setup is None or setup.identity in consumed or (model not in enabled and RETEST not in enabled):
                    continue
                level = setup.level
                approaching = 0 <= side * (level - close) <= WATCH_DISTANCE_ATR * atr and side * (close - previous_close) > 0
                crossed = (side * (previous_close - level) <= BREAK_BUFFER_ATR * atr
                           < side * (close - level))
                distance_from_extreme = bar["high"] - close if side == 1 else close - bar["low"]
                displacement = (side * (close - opening) >= BREAK_BODY_ATR * atr
                                and distance_from_extreme <= BREAK_TERMINAL_RANGE * (bar["high"] - bar["low"]))
                if watch is None and (approaching or (crossed and displacement)):
                    watched[key] = watch = setup
                    emit(watch, "WATCH", "APPROACHING_LEVEL" if approaching else "FIRST_OBSERVED_ON_BREAK")
                if not crossed:
                    continue
                if not displacement:
                    emit(watch or setup, "REJECTED", "INSUFFICIENT_DISPLACEMENT", reasons=["INSUFFICIENT_DISPLACEMENT"])
                    continue
                confirmed = replace(watch, breakout_ms=bar["time_close"])
                emit(confirmed, "BREAKOUT CONFIRMED", "CONFIRMED_CLOSE_CROSS")
                geometry(confirmed)
                consumed.add(confirmed.identity)
                del watched[key]
                if RETEST in enabled and side not in retests:
                    retest = replace(confirmed, model=RETEST, first_bar=index, first_ms=bar["time_close"],
                                     origin_model=model)
                    retests[side] = retest
                    emit(retest, "RETEST WATCH", "CONFIRMED_BREAK_LEVEL")
    return observations, plans
