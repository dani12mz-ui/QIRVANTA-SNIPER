"""Causal XAUUSD condition labels and the separate registered large-move scorer."""

from collections import deque
from decimal import Decimal
import json
from statistics import median

from gold_pro.direct_breakout_research import EVENT_CONFIG, HOUR, independent_hours, pinned


MINUTE = 60_000
SUPPORTED_MINUTES = (5, 15, 30, 60)
TREND_RANGE_MULTIPLE = Decimal("2.0")
TREND_EFFICIENCY = Decimal("0.25")
HIGH_RANGE_MULTIPLE = Decimal("1.50")
LOW_RANGE_MULTIPLE = Decimal("0.67")


def utc_session(close_ms):
    hour = close_ms // HOUR % 24
    if hour < 7:
        return "ASIA"
    if hour < 13:
        return "EUROPE"
    if hour < 21:
        return "US"
    return "ROLLOVER"


def completed_hour_regime(history):
    if len(history) < 24:
        return "UNKNOWN", "UNKNOWN"
    candles = tuple(history)
    widths = [bar[1] for bar in candles]
    typical_range = median(widths)
    reference_range = median(widths[:18])
    recent_range = median(widths[18:])
    trend = "UNKNOWN"
    if typical_range > 0:
        prices = (candles[0][0], *(bar[2] for bar in candles))
        displacement = prices[-1] - prices[0]
        distance = sum(abs(current - prior) for prior, current in zip(prices, prices[1:]))
        directional = (distance > 0 and abs(displacement) >= TREND_RANGE_MULTIPLE * typical_range
                       and abs(displacement) >= TREND_EFFICIENCY * distance)
        trend = ("UP" if displacement > 0 else "DOWN") if directional else "SIDEWAYS"
    volatility = "UNKNOWN"
    if reference_range > 0:
        if recent_range >= HIGH_RANGE_MULTIPLE * reference_range:
            volatility = "HIGH"
        elif recent_range <= LOW_RANGE_MULTIPLE * reference_range:
            volatility = "LOW"
        else:
            volatility = "NORMAL"
    return trend, volatility


def chart_prices(bar):
    prices = tuple(Decimal(str(bar[k])) for k in ("open", "high", "low", "close"))
    opening, high, low, closing = prices
    if not all(value.is_finite() for value in prices) or not 0 < low <= min(opening, closing) <= max(opening, closing) <= high:
        raise ValueError(f"Invalid chart OHLC at {bar['time']}")
    return prices


def regime_tape(bars, minutes):
    """Return trend, volatility, and UTC session known at each chart close.

    Twenty-four observed complete UTC hours may straddle scheduled market gaps.
    Within an hour, a missing bar disqualifies the entire hour. A completed hour
    joins history at its final chart close. Inputs must already be UTC sorted.
    """
    if minutes not in SUPPORTED_MINUTES:
        raise ValueError(f"Expected one of {SUPPORTED_MINUTES} chart minutes")
    step = minutes * MINUTE
    history = deque(maxlen=24)
    tape = {}
    hour = None
    previous_open = None
    valid_hour = False
    trend, volatility = "UNKNOWN", "UNKNOWN"
    for bar in bars:
        start, known = bar["time"], bar["time_close"]
        if (type(start) is not int or type(known) is not int or start % step != 0
                or known != start + step or previous_open is not None and start <= previous_open):
            raise ValueError(f"Invalid or disordered UTC chart clock at {start}")
        opening, high, low, closing = chart_prices(bar)
        bucket = start // HOUR * HOUR
        if bucket != hour:
            first_open, hour_high, hour_low = opening, high, low
            valid_hour = start == bucket
            hour = bucket
        else:
            valid_hour = valid_hour and start == previous_open + step
            hour_high, hour_low = max(hour_high, high), min(hour_low, low)
        if valid_hour and known == hour + HOUR:
            history.append((first_open, hour_high - hour_low, closing))
            trend, volatility = completed_hour_regime(history)
        tape[known] = {"trend": trend, "volatility": volatility, "session": utc_session(known)}
        previous_open = start
    return tape


def independent_move_labels(bars, minutes):
    """Return existing directional hour outcomes; call only in post-trade scoring."""
    rules = json.loads(pinned(EVENT_CONFIG))["event"]
    return independent_hours(bars, minutes, rules)

