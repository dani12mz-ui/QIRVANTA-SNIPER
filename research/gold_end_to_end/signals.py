"""Generate the two frozen causal entry hypotheses and their ablations.

G observes the standard scheduled Monday–Thursday 17:00–18:00 New York OANDA
maintenance break. At the first complete reopen candle's close, the signed
opening gap from the pre-break close must be at least 0.50 prior ATR14.
G_BASE additionally needs its close beyond the old close in the gap direction.
G_HOLD also needs its entire candle strictly beyond that level.

M uses the fully observed weekday UTC 00:00–06:00 range of the same day.
Its frozen midpoint is available at 06:00. During (06:00, 12:00) by close,
M_CROSS confirms on a first same-side midpoint cross. M_HOLD requires the
exactly next contiguous closed candle wholly beyond the midpoint and allows a
fresh later cross after a failed hold. Each arm confirms at most once per side
per day. Every fresh M_HOLD seed is logged. A failed seed cannot immediately
re-seed the same side; an independent opposite cross can seed that side.

Entries are proposals for a separate replay. All observation times are native
bar close clocks. A signal needs 250 preceding observed chart bars. Freeze the stop beyond the extreme of the relevant setup
and confirmation candles, buffered by 0.12 signal-close Wilder ATR14 and
rounded away to the 0.001 price tick. No targets or outcomes enter this module.
"""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from gold_pro.replay import round_away
from research.data.loader import validate_bars
from research.features.causal import Smooth


MINUTE = 60_000
HOUR = 60 * MINUTE
DAY = 24 * HOUR
WARMUP = 250
GAP_MINIMUM_ATR = 0.50
STOP_BUFFER_ATR = 0.12
TICK = 0.001
ARMS = ("G_BASE", "G_HOLD", "M_CROSS", "M_HOLD")
NY = ZoneInfo("America/New_York")
TRADE_SIDES = (1, -1)


def _is_reopen(previous, current):
    closing, opening = previous["time_close"], current["time"]
    if opening - closing != HOUR:
        return False
    local_close = datetime.fromtimestamp(closing / 1000, timezone.utc).astimezone(NY)
    local_open = datetime.fromtimestamp(opening / 1000, timezone.utc).astimezone(NY)
    return (
        local_close.date() == local_open.date()
        and local_open.weekday() <= 3
        and (local_close.hour, local_close.minute, local_close.second, local_close.microsecond) == (17, 0, 0, 0)
        and (local_open.hour, local_open.minute, local_open.second, local_open.microsecond) == (18, 0, 0, 0)
    )


def _directional_extreme(bar, side):
    return bar["low"] if side == 1 else bar["high"]


def _emit(tapes, arm, index, bar, seed_index, seed_bar, side, level, stage, **details):
    known = bar["time_close"]
    seed = seed_bar["time_close"]
    identity = f"{arm}|{side}|{seed}"
    tapes[arm]["observations"].append({
        "id": f"{identity}|{stage}|{known}",
        "setup_id": identity,
        "source": arm,
        "stage": stage,
        "side": side,
        "index": index,
        "known_ms": known,
        "seed_index": seed_index,
        "seed_ms": seed,
        "frozen_level": level,
        **details,
    })
    return identity


def _confirm(tapes, arm, index, bar, seed_index, seed_bar, side, level, atr, **details):
    identity = _emit(tapes, arm, index, bar, seed_index, seed_bar, side, level, "CONFIRMED", **details)
    adverse = min if side == 1 else max
    anchor = adverse(_directional_extreme(seed_bar, side), _directional_extreme(bar, side))
    stop = round_away(anchor - side * STOP_BUFFER_ATR * atr, TICK, -side)
    tapes[arm]["offers"].append({
        "id": f"{identity}|{bar['time_close']}",
        "model": arm,
        "side": side,
        "index": index,
        "known_ms": bar["time_close"],
        "reference": bar["close"],
        "stop": stop,
        "atr": atr,
        "seed_ms": seed_bar["time_close"],
        "frozen_level": level,
        "structural_anchor": anchor,
        **details,
    })


def _gap_arms(tapes, index, previous, bar, prior_atr, atr):
    if previous is None or prior_atr is None or prior_atr <= 0 or atr <= 0 or not _is_reopen(previous, bar):
        return
    level = previous["close"]
    signed_gap = bar["open"] - level
    if abs(signed_gap) < GAP_MINIMUM_ATR * prior_atr:
        return
    side = 1 if signed_gap > 0 else -1
    details = {"gap_atr": abs(signed_gap) / prior_atr, "prior_atr": prior_atr,
               "reference_known_ms": previous["time_close"]}
    for arm in ARMS[:2]:
        _emit(tapes, arm, index, bar, index, bar, side, level, "WATCH", **details)
        closes_beyond = (bar["close"] - level) * side > 0
        fully_beyond = (_directional_extreme(bar, side) - level) * side > 0
        if closes_beyond and (arm == "G_BASE" or fully_beyond):
            _confirm(tapes, arm, index, bar, index, bar, side, level, atr, **details)


class MidpointSession:
    def __init__(self, day_open, interval):
        self.day_open = day_open
        self.interval = interval
        self.freeze = day_open + 6 * HOUR
        self.expiry = day_open + 12 * HOUR
        self.weekday = datetime.fromtimestamp(day_open / 1000, timezone.utc).weekday() < 5
        self.range_expected = day_open
        self.high = self.low = self.midpoint = None
        self.range_complete = True
        self.confirmed_cross = set()
        self.confirmed_hold = set()
        self.pending = None

    def update_range(self, bar):
        if bar["time"] != self.range_expected or bar["time_close"] > self.freeze:
            self.range_complete = False
            return
        self.range_expected += self.interval
        self.high = bar["high"] if self.high is None else max(self.high, bar["high"])
        self.low = bar["low"] if self.low is None else min(self.low, bar["low"])
        if self.range_complete and self.range_expected == self.freeze and self.high > self.low:
            self.midpoint = (self.high + self.low) / 2

    def observe(self, tapes, index, previous, bar, atr):
        if not self.weekday:
            return
        if bar["time"] < self.freeze:
            self.update_range(bar)
            return
        if self.midpoint is None or bar["time_close"] >= self.expiry:
            self.pending = None
            return
        adjacent = previous is not None and previous["time_close"] == bar["time"]
        if not adjacent or atr is None or atr <= 0:
            self.pending = None
            return
        level = self.midpoint
        details = {"reference_known_ms": self.freeze}
        pending_side = None
        if self.pending:
            seed_index, seed, pending_side = self.pending
            self.pending = None
            if index == seed_index + 1 and (_directional_extreme(bar, pending_side) - level) * pending_side > 0:
                _confirm(tapes, "M_HOLD", index, bar, seed_index, seed, pending_side, level, atr, **details)
                self.confirmed_hold.add(pending_side)
        for side in TRADE_SIDES:
            crossed = (previous["close"] - level) * side <= 0 < (bar["close"] - level) * side
            if not crossed:
                continue
            if side not in self.confirmed_cross:
                _confirm(tapes, "M_CROSS", index, bar, index, bar, side, level, atr, **details)
                self.confirmed_cross.add(side)
            if side not in self.confirmed_hold and side != pending_side:
                _emit(tapes, "M_HOLD", index, bar, index, bar, side, level, "WATCH", **details)
                self.pending = index, bar, side


def scan(bars, minutes):
    """Return per-arm immutable-known-at observation and offer dictionaries."""
    if minutes not in (5, 15, 30):
        raise ValueError("Supply M5, M15, or M30 bars")
    interval = minutes * MINUTE
    candles = tuple(bars)
    duration = validate_bars(candles)
    if duration is not None and duration != interval:
        raise ValueError("Requested timeframe does not match observed bar duration")
    if any(b["time"] % interval or b["time_close"] % interval for b in candles):
        raise ValueError("Candles must sit on exact native UTC bar boundaries")
    tapes = {arm: {"observations": [], "offers": []} for arm in ARMS}
    atr = Smooth(14, wilder=True)
    session = None
    for index, bar in enumerate(candles):
        previous = candles[index - 1] if index else None
        previous_close = previous["close"] if previous else bar["close"]
        prior_atr = atr.value
        signal_atr = atr.add(max(bar["high"] - bar["low"], abs(bar["high"] - previous_close),
                                 abs(bar["low"] - previous_close)))
        eligible_atr = signal_atr if index >= WARMUP else None
        if eligible_atr is not None:
            _gap_arms(tapes, index, previous, bar, prior_atr, eligible_atr)
        day_open = bar["time"] // DAY * DAY
        if session is None or session.day_open != day_open:
            session = MidpointSession(day_open, interval)
        session.observe(tapes, index, previous, bar, eligible_atr)
    return tapes
