"""Causal replay of the fixed OANDA:XAUUSD Donchian 20/10 research protocol."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from math import ceil, floor, isfinite
from zoneinfo import ZoneInfo


def utc_ms(value: str) -> int:
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Provide an explicit timezone offset.")
    return int(stamp.timestamp() * 1000)


START = utc_ms("2016-01-01T00:00:00Z")
CUTOFF = utc_ms("2026-11-11T00:00:00Z")
INITIAL_CAPITAL = 100_000.0
RISK_FRACTION = 0.0025
WARMUP = 100
HOUR_MS = 3_600_000


@dataclass(frozen=True)
class Bar:
    time_ms: int
    close_ms: int
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class Instrument:
    tick: float
    quantity_step: float
    pointvalue: float

    def __post_init__(self):
        if not all(isfinite(x) and x > 0 for x in (self.tick, self.quantity_step, self.pointvalue)):
            raise ValueError("Tick, quantity step and point value must be finite and positive.")


@dataclass(frozen=True)
class Costs:
    name: str
    spread: float
    adverse_ticks: int
    commission: float
    annual_financing: float

    def __post_init__(self):
        if not self.name or type(self.adverse_ticks) is not int or self.adverse_ticks < 0:
            raise ValueError("Costs require a scenario name and nonnegative integer ticks.")
        if not all(isfinite(x) and x >= 0 for x in (self.spread, self.commission, self.annual_financing)):
            raise ValueError("Spread, commission and financing must be finite and nonnegative.")


BASE = Costs("baseline", 0.35, 2, 0.175, 0.04)
ADVERSE = Costs("adverse", 0.70, 5, 0.2625, 0.08)
SEVERE = Costs("severe", 1.05, 10, 0.35, 0.12)
SCENARIOS = (BASE, ADVERSE, SEVERE)


@dataclass(frozen=True)
class Signal:
    id: str
    side: int
    time_ms: int
    bar: int
    atr: float
    stop_ticks: int
    qty: float
    equity: float
    risk_budget: float
    risk_usd: float
    margin_capped: bool
    signal_close: float
    entry_level: float


@dataclass(frozen=True)
class Position:
    signal: Signal
    entry_ms: int
    entry_bar: int
    entry_mid: float
    entry: float
    stop: float
    fill_risk_usd: float
    fill_capital_used_usd: float
    financing_usd: float
    channel_exit_ms: int | None


@dataclass(frozen=True)
class Trade:
    id: str
    side: int
    signal_ms: int
    entry_ms: int
    exit_ms: int
    exit_seen_ms: int
    entry_bar: int
    exit_bar: int
    qty: float
    entry: float
    stop: float
    exit: float
    reason: str
    planned_risk_usd: float
    fill_risk_usd: float
    net_usd: float
    gross_usd: float
    spread_usd: float
    slippage_usd: float
    commission_usd: float
    financing_usd: float
    net_r: float
    holding_hours: float
    max_holding_hours: float
    entry_mid: float
    exit_mid: float
    signal_equity_usd: float
    fill_capital_used_usd: float
    exit_level: float | None


@dataclass(frozen=True)
class Event:
    tag: str
    data: dict


@dataclass(frozen=True)
class EquityMark:
    time_ms: int
    equity_usd: float
    balance_usd: float
    position: str | None
    exposed: bool
    financing_usd: float
    bar_start_ms: int
    cumulative_spread_usd: float
    cumulative_slippage_usd: float
    cumulative_commission_usd: float


@dataclass(frozen=True)
class Replay:
    trades: tuple[Trade, ...]
    events: tuple[Event, ...]
    equity: tuple[EquityMark, ...]
    pending: Signal | None
    open_trade: Position | None
    gaps: tuple[tuple[int, int], ...]
    first_bar_ms: int | None
    first_score_ms: int | None
    last_score_ms: int | None
    cutoff_ms: int
    costs: Costs
    instrument: Instrument
    native: bool


def validate_bars(bars: tuple[Bar, ...]) -> None:
    previous_close = None
    for i, bar in enumerate(bars):
        if type(bar.time_ms) is not int or type(bar.close_ms) is not int or bar.time_ms >= bar.close_ms:
            raise ValueError(f"Invalid open/close timestamps at bar {i}")
        if previous_close is not None and bar.time_ms < previous_close:
            raise ValueError(f"Duplicate, overlapping or out-of-order timestamps at bar {i}")
        if not all(isfinite(x) and x > 0 for x in (bar.open, bar.high, bar.low, bar.close)):
            raise ValueError(f"Invalid OHLC at bar {i}")
        if not bar.low <= min(bar.open, bar.close) <= max(bar.open, bar.close) <= bar.high:
            raise ValueError(f"Inconsistent OHLC at bar {i}")
        previous_close = bar.close_ms


@lru_cache(maxsize=1)
def _ny_zone() -> ZoneInfo:
    return ZoneInfo("America/New_York")


@lru_cache(maxsize=8192)
def _rollover(day: date) -> tuple[int, int] | None:
    weekday = day.weekday()
    if weekday > 4:
        return None
    timestamp = datetime.combine(day, time(17), _ny_zone())
    return int(timestamp.timestamp() * 1000), 3 if weekday == 2 else 1


def rollovers(after_ms: int, through_ms: int) -> tuple[tuple[int, int], ...]:
    """Scheduled research charges in (after_ms, through_ms], using New York DST."""
    if through_ms <= after_ms:
        return ()
    zone = _ny_zone()
    day = datetime.fromtimestamp(after_ms / 1000, zone).date()
    end = datetime.fromtimestamp(through_ms / 1000, zone).date()
    result = []
    while day <= end:
        event = _rollover(day)
        if event is not None and after_ms < event[0] <= through_ms:
            result.append(event)
        day += timedelta(days=1)
    return tuple(result)


def _friction_price(costs: Costs, instrument: Instrument, price: float) -> float:
    return ((costs.spread + 2 * costs.adverse_ticks * instrument.tick) * instrument.pointvalue
            + 2 * costs.commission + price * instrument.pointvalue * costs.annual_financing / 365)


def _signal(bar: Bar, index: int, side: int, level: float, atr: float, balance: float,
            instrument: Instrument, costs: Costs, native: bool) -> tuple[Signal | None, Event]:
    tag = "L" if side == 1 else "S"
    id_ = f"D20|{tag}|{bar.close_ms}"
    ticks = ceil(2 * atr / instrument.tick)
    unit_risk = ticks * instrument.tick * instrument.pointvalue + _friction_price(costs, instrument, bar.close)
    budget = balance * RISK_FRACTION
    nominal_price = bar.close + costs.adverse_ticks * instrument.tick
    if not native:
        nominal_price += costs.spread / 2
    unit_capital = nominal_price * instrument.pointvalue + costs.commission
    step = instrument.quantity_step
    risk_qty = budget / unit_risk if unit_risk > 0 else 0
    cap_qty = balance / unit_capital
    qty = floor(min(risk_qty, cap_qty) / step) * step
    if ticks <= 0 or qty < step:
        reason = ("ZERO_STOP_DISTANCE" if ticks <= 0 else
                  "RISK_BELOW_MIN_QTY" if risk_qty < step else "CAPITAL_BELOW_MIN_QTY")
        return None, Event("D20_REJECT", {
            "id": id_, "side": side, "time_ms": bar.close_ms, "bar": index, "reason": reason,
            "equity": balance, "risk_budget": budget, "min_qty": step,
        })
    signal = Signal(id_, side, bar.close_ms, index, atr, ticks, qty, balance, budget,
                    qty * unit_risk, cap_qty < risk_qty, bar.close, level)
    return signal, Event("D20_SIGNAL", vars(signal))


def _price_with_cost(mid, side, tick, ticks, spread, native):
    if native:
        # Historical OANDA OHLC can be half a tick off-grid. The native emulator
        # rounds to the nearest valid tick, with exact halves toward the higher price.
        step = Decimal(str(tick))
        units = Decimal(str(mid)) / step + side * ticks
        return float(units.quantize(Decimal(1), rounding=ROUND_HALF_UP) * step)
    return mid + side * (spread / 2 + ticks * tick)


def _position(signal: Signal, bar: Bar, index: int, instrument: Instrument, costs: Costs,
              native: bool) -> tuple[Position, Event]:
    price = _price_with_cost(bar.open, signal.side, instrument.tick,
                             costs.adverse_ticks, costs.spread, native)
    distance = signal.stop_ticks * instrument.tick
    stop = price - signal.side * distance
    cap = signal.qty * (price * instrument.pointvalue + costs.commission)
    risk = signal.qty * (distance * instrument.pointvalue + _friction_price(costs, instrument, price))
    position = Position(signal, bar.time_ms, index, bar.open, price, stop, risk, cap, 0.0, None)
    fill = Event("D20_FILL", {
        "id": signal.id, "side": signal.side, "entry_ms": bar.time_ms, "bar": index,
        "seen_ms": bar.close_ms, "price": price, "open": bar.open, "qty": signal.qty, "sl": stop,
        "planned_risk_usd": signal.risk_usd, "fill_risk_usd": risk,
        "fill_capital_used_usd": cap, "fill_risk_over_budget": risk > signal.risk_budget,
        "fill_capital_over_budget": cap > signal.equity,
    })
    return position, fill


def run(bars, instrument: Instrument, costs: Costs = BASE, *, start_ms: int | None = None,
        cutoff_ms: int = CUTOFF, warmup_bars: int = WARMUP, native: bool = False) -> Replay:
    """Use the native tape's order and timestamps. Mark open risk at the last complete close."""
    bars = tuple(bars)
    validate_bars(bars)
    start = START if start_ms is None else start_ms
    if cutoff_ms <= start or warmup_bars < 20:
        raise ValueError("Invalid replay interval or ATR warmup.")
    half_spread = 0.0 if native else costs.spread / 2
    annual = 0.0 if native else costs.annual_financing
    pointvalue = instrument.pointvalue
    balance = INITIAL_CAPITAL
    position = pending = queued_exit_level = None
    equity, trades, events, gaps = [], [], [], []
    atr = None
    seed = 0.0
    paid_spread = paid_slip = paid_commission = paid_financing = 0.0

    def fund(after_ms, through_ms, mid):
        nonlocal balance, paid_financing, position
        if position is None or not annual:
            return
        units = position.signal.qty
        for stamp, multiplier in rollovers(after_ms, through_ms):
            cost = units * mid * pointvalue * annual / 365 * multiplier
            balance -= cost
            paid_financing += cost
            position = replace(position, financing_usd=position.financing_usd + cost)
            events.append(Event("D20_FINANCING", {
                "id": position.signal.id, "time_ms": stamp, "multiplier": multiplier,
                "mid": mid, "qty": units, "usd": cost,
            }))

    def close(mid, reason, bar, index, intrabar=False):
        nonlocal balance, paid_spread, paid_slip, paid_commission, position, queued_exit_level
        signal = position.signal
        side, qty = signal.side, signal.qty
        exit_price = _price_with_cost(mid, -side, instrument.tick, costs.adverse_ticks, costs.spread, native)
        gross = side * (mid - position.entry_mid) * pointvalue * qty
        spread_usd = 2 * half_spread * pointvalue * qty
        entry_slip = (side * (position.entry - position.entry_mid) - half_spread) * pointvalue * qty
        exit_slip = (side * (mid - exit_price) - half_spread) * pointvalue * qty
        slip_usd = entry_slip + exit_slip
        commission_usd = 2 * costs.commission * qty
        fill_to_fill = side * (exit_price - position.entry) * pointvalue * qty
        net = fill_to_fill - commission_usd - position.financing_usd
        balance += fill_to_fill - costs.commission * qty
        paid_spread += spread_usd / 2
        paid_slip += exit_slip
        paid_commission += commission_usd / 2
        end_ms = bar.close_ms if intrabar else bar.time_ms
        trades.append(Trade(
            signal.id, side, signal.time_ms, position.entry_ms, bar.time_ms, bar.close_ms,
            position.entry_bar, index, qty, position.entry, position.stop, exit_price,
            reason, signal.risk_usd, position.fill_risk_usd, net, gross, spread_usd, slip_usd,
            commission_usd, position.financing_usd, net / signal.risk_usd,
            (bar.time_ms - position.entry_ms) / HOUR_MS, (end_ms - position.entry_ms) / HOUR_MS,
            position.entry_mid, mid, signal.equity, position.fill_capital_used_usd, queued_exit_level,
        ))
        events.append(Event("D20_EXIT", {
            "id": signal.id, "side": side, "entry_ms": position.entry_ms, "exit_ms": bar.time_ms,
            "bar": index, "seen_ms": bar.close_ms, "entry": position.entry, "price": exit_price,
            "qty": qty, "commission": commission_usd, "pnl": net, "reason": reason,
            "open": bar.open, "same_bar_sl": reason == "SL" and position.entry_bar == index,
            "same_bar_high": bar.high if reason == "SL" and position.entry_bar == index else None,
            "same_bar_low": bar.low if reason == "SL" and position.entry_bar == index else None,
        }))
        position = None
        queued_exit_level = None

    for i, bar in enumerate(bars):
        if bar.close_ms > cutoff_ms:
            break
        previous = bars[i - 1] if i else None
        true_range = bar.high - bar.low
        if previous is not None:
            true_range = max(true_range, abs(bar.high - previous.close), abs(bar.low - previous.close))
            if previous.close_ms < bar.time_ms:
                gaps.append((previous.close_ms, bar.time_ms))
        if i < 20:
            seed += true_range
            if i == 19:
                atr = seed / 20
        else:
            atr = (1 / 20) * true_range + (1 - 1 / 20) * atr
        if i < warmup_bars or bar.close_ms < start:
            continue
        if previous is not None:
            fund(previous.close_ms, bar.time_ms, previous.close)

        closed_this_bar = False
        exposure = False
        if position is not None:
            side = position.signal.side
            through_stop = side * (bar.open - side * half_spread - position.stop) <= 1e-10
            if position.channel_exit_ms is not None and (native or not through_stop):
                close(bar.open, "CHANNEL", bar, i)
                closed_this_bar = True
            elif through_stop:
                close(bar.open, "SL" if native else "SL_GAP", bar, i)
                closed_this_bar = True

        if pending is not None:
            position, fill = _position(pending, bar, i, instrument, costs, native)
            pending = None
            if not native and fill.data["fill_capital_over_budget"]:
                events.append(Event("D20_CAPITAL_REJECT_AT_OPEN", {
                    "id": position.signal.id, "side": position.signal.side, "time_ms": bar.time_ms,
                    "bar": i, "needed_usd": position.fill_capital_used_usd,
                    "equity": position.signal.equity, "reason": "CAPITAL_INSUFFICIENT_AT_FILL",
                }))
                position = None
            else:
                qty = position.signal.qty
                balance -= qty * costs.commission
                paid_commission += qty * costs.commission
                paid_spread += qty * half_spread * pointvalue
                paid_slip += qty * (position.signal.side * (position.entry - bar.open) - half_spread) * pointvalue
                events.append(fill)

        if position is not None:
            side = position.signal.side
            quote_at_open = bar.open - side * half_spread
            at_open = side * (quote_at_open - position.stop) <= 1e-10
            exposure = not at_open
            adverse = bar.low if side == 1 else bar.high
            stopped = side * (adverse - side * half_spread - position.stop) <= 1e-10
            if not at_open:
                fund(bar.time_ms, bar.close_ms - 1, bar.open)
            if stopped:
                # The stop was active on entry. If rollover and SL both lie inside this
                # OHLC bar, conservatively accrue the fee before the unobserved stop tick.
                mid = bar.open if at_open else position.stop + side * half_spread
                close(mid, "SL", bar, i, intrabar=not at_open)
                closed_this_bar = True
            else:
                fund(bar.close_ms - 1, bar.close_ms, bar.close)
                exit_window = bars[i - 10:i]
                exit_level = min(x.low for x in exit_window) if side == 1 else max(x.high for x in exit_window)
                if side * (bar.close - exit_level) < 0:
                    position = replace(position, channel_exit_ms=bar.close_ms)
                    queued_exit_level = exit_level
                    events.append(Event("D20_CHANNEL", {
                        "id": position.signal.id, "side": side, "time_ms": bar.close_ms,
                        "bar": i, "exit_level": exit_level,
                    }))

        if position is None and not closed_this_bar and bar.close_ms < cutoff_ms:
            entry_window = bars[i - 20:i]
            high, low = max(x.high for x in entry_window), min(x.low for x in entry_window)
            side = 1 if bar.close > high else -1 if bar.close < low else 0
            if side:
                pending, event = _signal(bar, i, side, high if side == 1 else low, atr, balance,
                                         instrument, costs, native)
                events.append(event)

        marked = balance
        if position is not None:
            side = position.signal.side
            marked += side * (bar.close - side * half_spread - position.entry) * pointvalue * position.signal.qty
        equity.append(EquityMark(bar.close_ms, marked, balance, position.signal.id if position else None,
                                 exposure, paid_financing, bar.time_ms, paid_spread, paid_slip,
                                 paid_commission))

    return Replay(tuple(trades), tuple(events), tuple(equity), pending, position, tuple(gaps),
                  bars[0].time_ms if bars else None, equity[0].bar_start_ms if equity else None,
                  equity[-1].time_ms if equity else None, cutoff_ms, costs, instrument, native)


def verify_prefixes(bars, instrument: Instrument, costs: Costs = BASE, **settings) -> dict:
    """Recompute from the identical first bar through every distinct decision bar."""
    bars = tuple(bars)
    full = run(bars, instrument, costs, **settings)
    tested = 0
    cutoffs = {event.data["bar"] + 1 for event in full.events if "bar" in event.data}
    for stop in sorted(cutoffs):
        prefix = run(bars[:stop], instrument, costs, **settings)
        known_through = bars[stop - 1].close_ms
        expected_events = tuple(
            event for event in full.events
            if event.data.get("seen_ms", event.data.get("time_ms")) <= known_through
        )
        expected_equity = tuple(mark for mark in full.equity if mark.time_ms <= known_through)
        if prefix.events != expected_events or prefix.equity != expected_equity:
            return {"status": "MISMATCH", "tested_prefixes": tested,
                    "first_difference_at_close_ms": known_through,
                    "events_match": prefix.events == expected_events, "equity_match": prefix.equity == expected_equity}
        tested += 1
    return {"status": "MATCH" if tested else "NO_DECISIONS", "tested_prefixes": tested}
