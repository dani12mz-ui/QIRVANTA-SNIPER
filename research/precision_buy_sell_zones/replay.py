"""Causal replay of the preregistered OANDA:XAUUSD observation-only zones."""

from dataclasses import dataclass
from hashlib import sha256
import json
from math import floor
from pathlib import Path

from research.data.loader import validate_bars
from research.features.causal import Smooth


PROTOCOL = Path(__file__).with_name("PROTOCOL.json")
PROTOCOL_SHA256 = "7406878761ffa1d6db918b72a2e283efde224f542723238a49f10cf80023ecc4"
TICK = 0.001
FORMED = "FORMATION_DETECTED"
TOUCH = "FIRST_TOUCH"
PARTIAL = "PARTIALLY_MITIGATED"
FULL = "FULLY_MITIGATED"
REJECTION = "REJECTION_CONFIRMED"
INVALIDATED = "INVALIDATED"
EXPIRED = "EXPIRED"
REPLACED = "REPLACED"
TERMINAL = {INVALIDATED, EXPIRED, REPLACED}


@dataclass(frozen=True)
class Pivot:
    price: float
    known_ms: int


@dataclass(frozen=True)
class Zone:
    id: str
    side: int
    origin_open_ms: int
    known_ms: int
    formed_index: int
    proximal: float
    distal: float
    break_level: float
    pivot_known_ms: int
    atr: float
    fvg: bool


@dataclass(frozen=True)
class ZoneEvent:
    zone_id: str
    state: str
    known_ms: int
    bar_index: int
    atr: float | None
    reason: str


@dataclass(frozen=True)
class ZoneView:
    zone: Zone
    status: str
    first_touch_ms: int | None
    rejection_ms: int | None


@dataclass(frozen=True)
class Snapshot:
    known_ms: int
    buy: ZoneView | None
    sell: ZoneView | None


@dataclass(frozen=True)
class Inputs:
    atr14: float | None
    pivot_high: float | None
    pivot_low: float | None
    fvg_buy: bool
    fvg_sell: bool


@dataclass(frozen=True)
class Replay:
    zones: tuple[Zone, ...]
    events: tuple[ZoneEvent, ...]
    snapshots: tuple[Snapshot, ...]
    inputs: tuple[Inputs, ...]


@dataclass
class ActiveZone:
    zone: Zone
    first_touch_ms: int | None = None
    rejection_ms: int | None = None
    mitigation: str | None = None

    def view(self):
        status = (REJECTION if self.rejection_ms is not None else self.mitigation or
                  (TOUCH if self.first_touch_ms is not None else "UNTESTED"))
        return ZoneView(self.zone, status, self.first_touch_ms, self.rejection_ms)


def read_protocol():
    raw = PROTOCOL.read_bytes()
    if sha256(raw).hexdigest() != PROTOCOL_SHA256:
        raise ValueError("The preregistered precision-zone protocol changed")
    return json.loads(raw)


def update_pivots(bars, index, current, rules):
    """Confirm a pivot on the right edge, choosing the rightmost tied extremum."""
    left, right = rules["pivot_left"], rules["pivot_right"]
    middle = index - right
    if middle < left:
        return {}
    center = bars[middle]
    past = bars[middle - left:middle]
    future = bars[middle + 1:index + 1]
    confirmed = {}
    for side, price in ((1, "high"), (-1, "low")):
        level = center[price]
        if (all(side * (level - row[price]) >= 0 for row in past)
                and all(side * (level - row[price]) > 0 for row in future)):
            current[side] = Pivot(level, bars[index]["time_close"])
            confirmed[side] = level
    return confirmed


def locate_origin(bars, index, side, atr, tick, rules):
    for age in range(1, min(index, 5) + 1):
        origin = bars[index - age]
        if bars[index - age + 1]["time"] != origin["time_close"]:
            break
        if side * (origin["close"] - origin["open"]) >= 0:
            continue
        proximal = origin["open"]
        distal = origin["low" if side > 0 else "high"]
        height = side * (proximal - distal)
        if rules["min_height_ticks"] <= floor(height / tick + 0.5) and height <= rules["max_height_atr"] * atr:
            return origin["time"], proximal, distal
    return None


def current_fvg(bars, index, side, atr):
    if index < 2:
        return False
    current, earlier = bars[index], bars[index - 2]
    width = current["low"] - earlier["high"] if side > 0 else earlier["low"] - current["high"]
    return width > 0 and width >= 0.04 * atr


def find_zone(bars, index, pivots, atr, frame, tick, rules, symbol):
    if index == 0 or atr is None or atr <= 0:
        return None
    current, previous = bars[index], bars[index - 1]
    if current["time"] != previous["time_close"]:
        return None
    body = current["close"] - current["open"]
    if body == 0:
        return None
    side = 1 if body > 0 else -1
    pivot = pivots.get(side)
    if pivot is None or not side * (current["close"] - pivot.price) > 0 >= side * (previous["close"] - pivot.price):
        return None
    raw_range = current["high"] - current["low"]
    if floor(raw_range / tick + 0.5) < rules["minimum_range_ticks"]:
        return None
    candle_range = max(raw_range, rules["minimum_range_ticks"] * tick)
    favorable = current["close"] - current["low"] if side > 0 else current["high"] - current["close"]
    if abs(body) < rules["minimum_displacement_body_atr"] * atr or favorable / candle_range < rules["minimum_close_location"]:
        return None
    origin = locate_origin(bars, index, side, atr, tick, rules)
    if origin is None:
        return None
    origin_open, proximal, distal = origin
    known = current["time_close"]
    direction = "BUY" if side > 0 else "SELL"
    identity = f"{symbol}|{frame}|{direction}|{known}|{origin_open}"
    return Zone(identity, side, origin_open, known, index, proximal, distal, pivot.price, pivot.known_ms,
                atr, current_fvg(bars, index, side, atr))


def advance(active, current, previous, index, atr, rules, emit):
    zone = active.zone
    if index - zone.formed_index >= rules["expiry_bars"]:
        emit(zone, EXPIRED, "AGE_LIMIT")
        return False
    close, side = current["close"], zone.side
    has_atr = atr is not None and atr > 0
    if has_atr and side * (close - zone.distal) <= -rules["invalidation_close_atr"] * atr:
        emit(zone, INVALIDATED, "CONFIRMED_CLOSE_PAST_DISTAL")
        return False
    low, high = sorted((zone.proximal, zone.distal))
    overlap = current["low"] <= high and current["high"] >= low
    if overlap:
        if active.first_touch_ms is None:
            active.first_touch_ms = current["time_close"]
            emit(zone, TOUCH, "CONFIRMED_RANGE_OVERLAP")
        wick = current["low"] if side > 0 else current["high"]
        if active.mitigation != FULL and side * (wick - zone.distal) <= 0:
            active.mitigation = FULL
            emit(zone, FULL, "WICK_REACHED_DISTAL")
        elif active.mitigation is None and side * (wick - zone.proximal) < 0:
            active.mitigation = PARTIAL
            emit(zone, PARTIAL, "WICK_PAST_PROXIMAL")
    if (active.first_touch_ms is not None and active.rejection_ms is None and has_atr and previous is not None
            and current["time"] == previous["time_close"] and (overlap or low <= previous["close"] <= high)
            and side * (close - zone.proximal) > 0 and side * (close - current["open"]) >= rules["rejection_body_atr"] * atr):
        active.rejection_ms = current["time_close"]
        emit(zone, REJECTION, "DIRECTIONAL_CLOSE_PAST_PROXIMAL")
    return True


def replay(bars, frame, rules=None, *, tick=TICK):
    """Return all formation and lifecycle events plus immutable per-close active views."""
    rules = read_protocol() if rules is None else rules
    frame = str(frame)
    if frame not in rules["timeframes"] or tick <= 0:
        raise ValueError("Expected OANDA:XAUUSD M5/M15/M30 and a positive tick")
    bars = tuple(bars)
    interval = validate_bars(bars)
    if interval is not None and interval != int(frame) * 60_000:
        raise ValueError("Chart interval does not match the replay timeframe")
    atr_filter, previous, pivots, active = Smooth(14, wilder=True), None, {}, {}
    zones, events, snapshots, inputs = [], [], [], []
    for index, current in enumerate(bars):
        high, low = current["high"], current["low"]
        tr = high - low if previous is None else max(high - low, abs(high - previous["close"]), abs(low - previous["close"]))
        atr = atr_filter.add(tr)
        known = current["time_close"]

        def emit(zone, state, reason):
            events.append(ZoneEvent(zone.id, state, known, index, atr, reason))

        for side in (1, -1):
            prior_zone = active.get(side)
            if prior_zone is not None and not advance(prior_zone, current, previous, index, atr, rules["lifecycle"], emit):
                del active[side]
        zone = (find_zone(bars, index, pivots, atr, frame, tick, rules["zone_rule"], rules["symbol"])
                if index >= rules["evaluation"]["warmup_chart_bars"] else None)
        if zone is not None:
            if zone.side in active:
                emit(active[zone.side].zone, REPLACED, "FRESHER_SAME_SIDE_FORMATION")
            active[zone.side] = ActiveZone(zone)
            zones.append(zone)
            emit(zone, FORMED, "CONFIRMED_PIVOT_BREAK")
        snapshots.append(Snapshot(known, active[1].view() if 1 in active else None, active[-1].view() if -1 in active else None))
        confirmed = update_pivots(bars, index, pivots, rules["zone_rule"])
        inputs.append(Inputs(atr, confirmed.get(1), confirmed.get(-1),
                             current_fvg(bars, index, 1, atr) if atr is not None else False,
                             current_fvg(bars, index, -1, atr) if atr is not None else False))
        previous = current
    return Replay(tuple(zones), tuple(events), tuple(snapshots), tuple(inputs))
