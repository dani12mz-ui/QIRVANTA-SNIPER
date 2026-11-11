"""Causal V2.2 signal reconstruction on archived chart OHLC."""

from bisect import bisect_right
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from operator import gt, lt
from statistics import mean
from zoneinfo import ZoneInfo

from research.features.causal import Smooth
from unified.tools.evaluate_v21_impulse import HOUR, known_pivot

MODELS = ("A", "B", "C")
NY = ZoneInfo("America/New_York")
H4_STARTS = (60, 300, 540, 780, 1080, 1260)


def htf_key(time, minutes):
    if minutes == 60:
        return time // HOUR
    local = datetime.fromtimestamp(time / 1000, timezone.utc).astimezone(NY)
    slot = bisect_right(H4_STARTS, local.hour * 60 + local.minute) - 1
    return local.toordinal() * len(H4_STARTS) + slot


class HTF:
    """Release an aggregate only when the next observed HTF bucket opens."""

    def __init__(self, minutes):
        self.minutes = minutes
        self.key = None
        self.last_close = None
        self.last_close_time = None
        self.confirmed_close = None
        self.confirmed_at = None
        self.ema21 = self.ema55 = None
        self.direction = 0
        self.closed_count = 0

    def add(self, bar):
        key = htf_key(bar["time"], self.minutes)
        if self.key is not None and key != self.key:
            self.confirmed_close = self.last_close
            self.confirmed_at = self.last_close_time
            self.ema21 = ema(self.ema21, self.confirmed_close, 21)
            self.ema55 = ema(self.ema55, self.confirmed_close, 55)
            self.direction = (1 if self.confirmed_close > self.ema21 > self.ema55 else
                              -1 if self.confirmed_close < self.ema21 < self.ema55 else 0)
            self.closed_count += 1
        self.key = key
        self.last_close = bar["close"]
        self.last_close_time = bar["time_close"]


def ema(previous, close, length):
    alpha = 2 / (length + 1)
    return close if previous is None else alpha * close + (1 - alpha) * previous


@dataclass(frozen=True)
class Feature:
    atr: float | None
    current_atr: float | None
    ema21: float
    ema55: float
    adx: float | None
    di: int
    rsi: float | None
    trend: int
    h1: int
    h4: int
    h1_time: int | None
    h4_time: int | None
    h1_close: float | None
    h4_close: float | None
    htf_warm: bool
    highs: tuple
    lows: tuple
    newest_high: float | None
    newest_low: float | None
    spike: bool
    displacement: int
    box: tuple
    mean_tr: float
    structure: int
    bos: int
    mss: bool
    regime: str
    regime_reason: str


def feature_rows(bars, settings, earlier=()):
    prior_atr = None
    atr, dm_range, plus, minus, adx, gain, loss = (Smooth(14, wilder=True) for _ in range(7))
    ema21 = ema55 = None
    tr_window = deque(maxlen=settings["C"]["box_bars"])
    atr_window = deque(maxlen=20)
    pivots = {1: deque(maxlen=2), -1: deque(maxlen=2)}
    radius = settings["pivot_left_and_right_bars"]
    h1, h4 = HTF(60), HTF(240)
    for bar in earlier:
        if bar["time_close"] > bars[0]["time"]:
            raise ValueError("HTF prehistory overlaps chart bars")
        h1.add(bar)
        h4.add(bar)
    features = []
    break_bias = 0
    previous_momentum = None
    for i, b in enumerate(bars):
        h1.add(b)
        h4.add(b)
        if any(h.confirmed_at is not None and h.confirmed_at > b["time"] for h in (h1, h4)):
            raise ValueError("HTF aggregate contains information unavailable at chart open")
        previous = bars[i - 1] if i else b
        close, op, high, low = (b[k] for k in ("close", "open", "high", "low"))
        candle_range = high - low
        tr = max(candle_range, abs(high - previous["close"]), abs(low - previous["close"]))
        current_atr = atr.add(tr)
        tr_window.append(tr)
        rng = dm_range.add(tr if i else None)
        # Match Pine's nine-decimal float comparisons so equal directional
        # moves do not choose a side because of binary subtraction.
        up = round(high - previous["high"], 9)
        down = round(previous["low"] - low, 9)
        pdm = plus.add((up if up > max(0, down) else 0) if i else None)
        mdm = minus.add((down if down > max(0, up) else 0) if i else None)
        valid_dm = rng is not None and rng > 0 and pdm is not None and mdm is not None
        dx = (100 * abs(pdm - mdm) / (pdm + mdm) if pdm + mdm else 0) if valid_dm else None
        adx_value = adx.add(dx)
        di = ((pdm > mdm) - (mdm > pdm)) if valid_dm else 0
        change = close - previous["close"]
        up_avg, dn_avg = gain.add(max(0, change) if i else None), loss.add(max(0, -change) if i else None)
        rsi = (100 * up_avg / (up_avg + dn_avg) if up_avg + dn_avg else 50) if up_avg is not None else None
        ema21, ema55 = ema(ema21, close, 21), ema(ema55, close, 55)
        highs, lows = tuple(pivots[1]), tuple(pivots[-1])
        trend = structure = displacement = 0
        if len(highs) == len(lows) == 2:
            if highs[-1][1] > highs[0][1] and lows[-1][1] > lows[0][1]:
                structure = 1
            elif highs[-1][1] < highs[0][1] and lows[-1][1] < lows[0][1]:
                structure = -1
        slope = settings["trend_slope_bars"]
        if i >= slope:
            prev = features[i - slope]
            for d in (1, -1):
                if (min(d * (close - ema21), d * (ema21 - ema55), d * (ema21 - prev.ema21),
                        d * (ema55 - prev.ema55)) > 0 and
                        (structure == d or (adx_value is not None and adx_value >=
                                           settings["local_trend_minimum_adx"] and di == d))):
                    trend = d
        if prior_atr and candle_range > 0:
            body = abs(close - op)
            directional = 1 if close > op else -1
            favorable_wick = high - close if directional == 1 else close - low
            if (body >= settings["displacement_body_atr"] * prior_atr
                    and body >= settings["displacement_body_range"] * candle_range
                    and favorable_wick <= settings["displacement_close_range"] * candle_range):
                displacement = directional
        recent = bars[max(0, i + 1 - settings["C"]["box_bars"]):i + 1]
        box = (min(x["low"] for x in recent), max(x["high"] for x in recent))
        mean_tr = mean(tr_window)
        ready = i >= settings["warmup_bars"] and prior_atr is not None and prior_atr > 0
        bos = 0
        mss = False
        momentum = None
        regime, regime_reason = "WARMUP", "INSUFFICIENT_BARS"
        if ready:
            if highs and previous["close"] <= highs[-1][1] < close:
                bos = 1
            elif lows and previous["close"] >= lows[-1][1] > close:
                bos = -1
            mss = bool(bos and break_bias == -bos)
            if bos:
                break_bias = bos
            momentum = (close - bars[i - 3]["close"]) / prior_atr
            extension = abs(close - ema21) / prior_atr
            upper_wick = high - max(op, close)
            lower_wick = min(op, close) - low
            upper = (previous_momentum is not None and previous_momentum >= .8 and
                     high - ema21 >= 2.5 * prior_atr and upper_wick >= .50 * candle_range and
                     momentum < previous_momentum)
            lower = (previous_momentum is not None and previous_momentum <= -.8 and
                     ema21 - low >= 2.5 * prior_atr and lower_wick >= .50 * candle_range and
                     momentum > previous_momentum)
            narrow = (box[1] - box[0] <= settings["C"]["box_max_range_atr"] * prior_atr and
                      mean_tr <= settings["C"]["mean_true_range_atr"] * prior_atr)
            if extension > 2.5 or (candle_range > 0 and (upper or lower)):
                regime = "EXHAUSTION / OVEREXTENSION"
                regime_reason = "EMA_DISTANCE" if extension > 2.5 else "WICK_AND_DECELERATION"
            elif mss:
                regime, regime_reason = "POTENTIAL REVERSAL", "OPPOSITE_STRUCTURE_BREAK"
            elif tr >= 1.5 * prior_atr:
                regime, regime_reason = "VOLATILITY EXPANSION", "TRUE_RANGE_EXPANSION"
            elif narrow or (len(atr_window) == 20 and prior_atr <= .8 * mean(atr_window)):
                regime, regime_reason = "COMPRESSION", "NARROW_BOX" if narrow else "ATR_CONTRACTION"
            elif trend:
                regime = "BULLISH TREND" if trend == 1 else "BEARISH TREND"
                regime_reason = "EMA_DIRECTION_WITH_STRUCTURE_OR_DMI"
            else:
                regime, regime_reason = "SIDEWAYS / RANGE", "NO_QUALIFIED_TREND"
        high_pivot = known_pivot(bars, i, radius, "high", gt)
        low_pivot = known_pivot(bars, i, radius, "low", lt)
        features.append(Feature(prior_atr, current_atr, ema21, ema55, adx_value, di, rsi, trend, h1.direction,
                                h4.direction, h1.confirmed_at, h4.confirmed_at, h1.confirmed_close,
                                h4.confirmed_close, min(h1.closed_count, h4.closed_count) >= 55, highs, lows,
                                high_pivot[1] if high_pivot else None, low_pivot[1] if low_pivot else None,
                                bool(prior_atr and tr > settings["volatility_spike_true_range_atr"] * prior_atr),
                                displacement, box, mean_tr, structure, bos, mss, regime, regime_reason))
        for d, pivot in ((1, high_pivot), (-1, low_pivot)):
            if pivot:
                pivots[d].append(pivot)
        prior_atr = current_atr
        if current_atr is not None:
            atr_window.append(current_atr)
        previous_momentum = momentum
    return features


@dataclass
class Watch:
    side: int
    index: int
    trigger: float
    stop: float
    anchor: float
    atr: float
    pivot: int | None = None


@dataclass(frozen=True)
class Candidate:
    model: str
    side: int
    index: int
    time: int
    watch_time: int
    trigger: float
    stop_base: float
    reference: float
    atr: float
    extension: float
    barrier: float | None
    spike: bool
    htf_conflict: bool


def scan(bars, features, settings):
    watches = dict.fromkeys(MODELS)
    last_confirmation = dict.fromkeys(MODELS, -10_000)
    consumed_pivot = {}
    decisions, candidates = [], []
    regime = "INITIAL"
    for i, (b, f) in enumerate(zip(bars, features)):
        t = b["time_close"]
        if regime != f.regime:
            decisions.append({"model": "REGIME", "bar": i, "time": t, "side": f.trend,
                              "state": "REGIME_TRANSITION", "from": regime, "to": f.regime,
                              "reason": f.regime_reason})
            regime = f.regime
        if i < settings["warmup_bars"] or f.atr is None or f.atr <= 0:
            continue
        prior, f_prior = bars[i - 1], features[i - 1]
        close, previous_close = b["close"], prior["close"]
        for model in MODELS:
            watch = watches[model]
            candidate = False

            def emit(state, reason):
                watch_time = bars[watch.index]["time_close"]
                decisions.append({"model": model, "side": watch.side, "bar": i, "time": t,
                                  "state": state, "reason": reason, "watch_time": watch_time,
                                  "setup_id": f"{model}-{'BUY' if watch.side == 1 else 'SELL'}-{watch_time}",
                                  "trigger": watch.trigger, "stop_base": watch.stop, "anchor": watch.anchor})

            def cross(level, side):
                return side * (previous_close - level) <= 0 < side * (close - level)

            if watch:
                d, age = watch.side, i - watch.index
                pivot_set = f.highs if d == 1 else f.lows
                invalid_reason = None
                if age > settings[model]["ttl_bars"]:
                    emit("EXPIRED", "EXPIRED")
                    watch = None
                elif f.trend == -d:
                    invalid_reason = "STRUCTURE_INVALID"
                elif model == "A":
                    if not pivot_set or watch.pivot != pivot_set[-1][0]:
                        invalid_reason = "STRUCTURE_INVALID"
                    else:
                        if age == 1:
                            emit("CONFIRMING", "WAITING_FOR_STRUCTURE_BREAK")
                        if cross(watch.trigger, d):
                            consumed_pivot[d] = watch.pivot
                            candidate = f.trend == d and (
                                not settings["A"]["require_displacement"] or f.displacement == d)
                            if not candidate:
                                invalid_reason = "NO_CONFIRMATION"
                elif model == "B":
                    if d * (close - watch.anchor) < -settings["B"]["invalidation_band_atr"] * watch.atr:
                        invalid_reason = "STRUCTURE_INVALID"
                    else:
                        if age == 1:
                            emit("CONFIRMING", "WAITING_FOR_PULLBACK_REJECTION")
                        watch.stop = min(watch.stop, b["low"]) if d == 1 else max(watch.stop, b["high"])
                        candidate = (f.trend == d and d * (close - watch.trigger) > 0
                                     and d * (close - b["open"]) > 0)
                elif d * (close - watch.stop) < 0:
                    invalid_reason = "STRUCTURE_INVALID"
                else:
                    if age == 1:
                        emit("CONFIRMING", "WAITING_FOR_BOX_BREAK")
                    if d * (close - watch.trigger) > 0:
                        candidate = f.trend == d and f.displacement == d
                        if not candidate:
                            invalid_reason = "NO_CONFIRMATION"
                if invalid_reason:
                    emit("INVALIDATED", invalid_reason)
                    watch = None

            if not watch and i - last_confirmation[model] > settings["cooldown_bars"]:
                d = f_prior.trend if model == "B" else f.trend
                if model == "A" and d:
                    piv = f.highs if d == 1 else f.lows
                    opp = f.lows if d == 1 else f.highs
                    if piv and opp and consumed_pivot.get(d) != piv[-1][0]:
                        level = piv[-1][1]
                        crossed = cross(level, d)
                        near = 0 <= d * (level - close) <= settings["A"]["watch_distance_atr"] * f.atr
                        if near or crossed:
                            watch = Watch(d, i, level, opp[-1][1], level, f.atr, piv[-1][0])
                            emit("WATCH", "KNOWN_SWING_APPROACH")
                            if crossed:
                                emit("CONFIRMING", "STRUCTURE_BREAK")
                                consumed_pivot[d] = watch.pivot
                                candidate = not settings["A"]["require_displacement"] or f.displacement == d
                                if not candidate:
                                    emit("INVALIDATED", "NO_CONFIRMATION")
                                    watch = None
                elif model == "B" and d and f.trend != -d:
                    zone = f_prior.ema21
                    threshold = settings["B"]["touch_band_atr"] * f.atr
                    adverse = b["low"] if d == 1 else b["high"]
                    touch = d * (adverse - zone) <= threshold
                    if (d * (previous_close - zone) > threshold and touch and
                            d * (close - zone) >= -settings["B"]["invalidation_band_atr"] * f.atr):
                        trigger = b["high"] if d == 1 else b["low"]
                        watch = Watch(d, i, trigger, adverse, zone, f.atr)
                        emit("WATCH", "PRIOR_EMA21_PULLBACK")
                elif model == "C" and d:
                    low, high = f.box
                    older = bars[i + 1 - settings["C"]["box_bars"]]
                    tightening = b["low"] > older["low"] if d == 1 else b["high"] < older["high"]
                    if (high - low <= settings["C"]["box_max_range_atr"] * f.atr and tightening
                            and f.mean_tr <= settings["C"]["mean_true_range_atr"] * f.atr):
                        trigger, stop = (high, low) if d == 1 else (low, high)
                        watch = Watch(d, i, trigger, stop, trigger, f.atr)
                        emit("WATCH", "DIRECTIONAL_COMPRESSION")
            if candidate:
                d = watch.side
                if model == "A":
                    latest_opposite = f.lows if d == 1 else f.highs
                    watch.stop = latest_opposite[-1][1]
                levels = f.highs if d == 1 else f.lows
                barrier = min((price for _, price in levels if d * (price - close) > 0),
                              key=lambda price: d * (price - close), default=None)
                candidates.append(Candidate(model, d, i, t, bars[watch.index]["time_close"], watch.trigger,
                                            watch.stop, close, f.atr, abs(close - f.ema21) / f.atr, barrier,
                                            f.spike, -d in (f.h1, f.h4)))
                emit("CONFIRMED", {"A": "STRUCTURE_BREAK", "B": "PULLBACK_REJECTION",
                                   "C": "BOX_BREAK_WITH_DISPLACEMENT"}[model])
                last_confirmation[model] = i
                watch = None
            watches[model] = watch
    return decisions, candidates
