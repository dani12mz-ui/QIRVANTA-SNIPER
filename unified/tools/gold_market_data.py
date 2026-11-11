"""Causal GOLD PRO market context for archived OANDA XAUUSD chart candles."""

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import floor, isfinite
from zoneinfo import ZoneInfo


HTF_WINDOW = 110
FINGERPRINT_WINDOW = 200
PRIMES = (1_000_000_007, 1_000_000_009)
MAX_AGE_MS = {60: 7_200_000, 240: 28_800_000, 1440: 345_600_000}
HOUR = 3_600_000
NEW_YORK = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")
TOKYO = ZoneInfo("Asia/Tokyo")
H4_HOURS = (1, 5, 9, 13, 18, 21)
SESSION_ORDER = ("LONDON–NEW YORK", "NEW YORK", "LONDON", "ASIA")


@dataclass(frozen=True)
class Bar:
    open_at: int
    close_at: int
    open: float
    high: float
    low: float
    close: float

    def __post_init__(self):
        if (not isinstance(self.open_at, int) or not isinstance(self.close_at, int)
                or self.open_at < 0 or self.close_at <= self.open_at
                or not all(isfinite(price) and price > 0 for price in self.ohlc)
                or not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high):
            raise ValueError(f"Invalid confirmed OHLC at {self.open_at}")

    @classmethod
    def from_mapping(cls, row):
        return cls(row["time"], row["time_close"], *(float(row[key]) for key in ("open", "high", "low", "close")))

    @property
    def ohlc(self):
        return self.open, self.high, self.low, self.close


@dataclass(frozen=True)
class HtfContext:
    open_at: int | None = None
    known_at: int | None = None
    close: float | None = None
    ema21: float | None = None
    ema55: float | None = None
    side: int | None = None


@dataclass(frozen=True)
class ObservedRange:
    high: float
    low: float
    observed_since: int
    complete_to_current: bool


@dataclass(frozen=True)
class MarketContext:
    bar: Bar
    h1: HtfContext
    h4: HtfContext
    d1: HtfContext
    fingerprint: tuple[int, int] | None
    session: str
    session_range: ObservedRange | None

    @property
    def ready(self):
        return self.h1.side is not None and self.h4.side is not None


def finite_emas(closes):
    """Use exactly 110 chronological, completed closes and seed on the oldest."""
    if len(closes) != HTF_WINDOW or any(value is None for value in closes):
        return None, None
    prices = iter(closes)
    fast = slow = next(prices)
    for close in prices:
        fast += (close - fast) * (2.0 / 22.0)
        slow += (close - slow) * (2.0 / 56.0)
    return fast, slow


def htf_side(open_at, known_at, close, fast, slow, chart_open, minutes):
    if (None in (open_at, known_at, close, fast, slow)
            or not open_at < known_at <= chart_open
            or chart_open - known_at > MAX_AGE_MS[minutes]):
        return None
    last, f, s = (floor(value * 1_000_000 + 0.5) for value in (close, fast, slow))
    return 1 if last > f > s else -1 if last < f < s else 0


def fingerprint_tokens(bar, tick):
    if not isfinite(tick) or tick <= 0:
        raise ValueError("Tick size must be finite and positive")
    o, h, l, c = (floor(value / tick + 0.5) for value in bar.ohlc)
    first, last = bar.open_at // 1000, bar.close_at // 1000
    return (
        (first + 3 * last + 5 * o + 7 * h + 11 * l + 13 * c) % PRIMES[0],
        (last + (1 + first % 65521) * (17 * o + 19 * h + 23 * l + 29 * c)) % PRIMES[1],
    )


class RollingFingerprint:
    def __init__(self, tick):
        self.tick = tick
        self.window = deque()
        self.first = self.second = 0

    def add(self, bar):
        one, two = fingerprint_tokens(bar, self.tick)
        self.window.append((one, two))
        self.first += one
        self.second += two
        if len(self.window) > FINGERPRINT_WINDOW:
            oldest_one, oldest_two = self.window.popleft()
            self.first -= oldest_one
            self.second -= oldest_two
        if len(self.window) == FINGERPRINT_WINDOW:
            return self.first % PRIMES[0], self.second % PRIMES[1]
        return None


def utc_from_ms(stamp):
    return datetime.fromtimestamp(stamp / 1000, timezone.utc)


def _millis(value):
    return int(value.timestamp() * 1000)


def _wall_time(date, hour):
    return datetime(date.year, date.month, date.day, hour, tzinfo=NEW_YORK)


def bucket_bounds(open_at, minutes):
    """Infer OANDA boundaries from the observed New York session convention."""
    if minutes == 60:
        first = open_at // HOUR * HOUR
        return first, first + HOUR
    local = utc_from_ms(open_at).astimezone(NEW_YORK)
    date = local.date()
    if minutes == 1440:
        if local.hour < 18:
            date -= timedelta(days=1)
        return _millis(_wall_time(date, 18)), _millis(_wall_time(date + timedelta(days=1), 17))
    if minutes != 240:
        raise ValueError(f"Unsupported HTF duration {minutes}")
    start = max((hour for hour in H4_HOURS if hour <= local.hour), default=None)
    if start is None:
        date -= timedelta(days=1)
        start = 21
    if start == 21:
        stop = _wall_time(date + timedelta(days=1), 1)
    else:
        stop = _wall_time(date, start + (3 if start == 18 else 4))
    return _millis(_wall_time(date, start)), _millis(stop)


class _HigherFrame:
    def __init__(self, minutes):
        self.minutes = minutes
        self.bounds = None
        self.formed = False
        self.previous = None
        self.confirmed = HtfContext()
        self.closes = deque(maxlen=HTF_WINDOW)

    def add(self, chart):
        bounds = bucket_bounds(chart.open_at, self.minutes)
        if bounds != self.bounds:
            if self.bounds is not None:
                first, end = self.bounds
                if self.formed and self.previous.close_at == end:
                    self.closes.append(self.previous.close)
                    fast, slow = finite_emas(self.closes)
                    self.confirmed = HtfContext(first, end, self.previous.close, fast, slow)
                else:
                    self.closes.clear()
                    self.confirmed = HtfContext()
            self.bounds = bounds
            self.formed = chart.open_at == bounds[0]
        else:
            self.formed = self.formed and self.previous.close_at == chart.open_at
        self.formed = self.formed and chart.close_at <= bounds[1]
        self.previous = chart
        closed = self.confirmed
        side = htf_side(closed.open_at, closed.known_at, closed.close, closed.ema21, closed.ema55,
                        chart.open_at, self.minutes)
        return HtfContext(closed.open_at, closed.known_at, closed.close, closed.ema21, closed.ema55, side)


def active_sessions(open_at):
    stamp = utc_from_ms(open_at)
    tokyo, london, ny = (stamp.astimezone(zone) for zone in (TOKYO, LONDON, NEW_YORK))
    slots = {}
    if tokyo.weekday() < 5 and 9 <= tokyo.hour < 18:
        slots["ASIA"] = tokyo.date(), tokyo.hour == 9 and tokyo.minute == 0
    if london.weekday() < 5 and 8 <= london.hour < 17:
        slots["LONDON"] = london.date(), london.hour == 8 and london.minute == 0
    if ny.weekday() < 5 and 8 <= ny.hour < 17:
        slots["NEW YORK"] = ny.date(), ny.hour == 8 and ny.minute == 0
        if "LONDON" in slots:
            slots["LONDON–NEW YORK"] = slots["NEW YORK"]
    return slots


class SessionRanges:
    def __init__(self):
        self.active = {}
        self.previous_close = None

    def add(self, chart):
        current = {}
        for label, (date, at_open) in active_sessions(chart.open_at).items():
            previous_date, prior = self.active.get(label, (None, None))
            if date != previous_date:
                observed = ObservedRange(chart.high, chart.low, chart.open_at, at_open)
            else:
                observed = ObservedRange(
                    max(prior.high, chart.high), min(prior.low, chart.low), prior.observed_since,
                    prior.complete_to_current and self.previous_close == chart.open_at,
                )
            current[label] = date, observed
        self.active = current
        self.previous_close = chart.close_at
        label = next((item for item in SESSION_ORDER if item in current), "OFF SESSION")
        return label, current[label][1] if label in current else None


def context_rows(bars, tick=0.001, earlier=()):
    """Yield context at chart closes. Optional earlier bars warm HTF only."""
    frames = [_HigherFrame(minutes) for minutes in (60, 240, 1440)]
    fingerprints = RollingFingerprint(float(tick))
    sessions = SessionRanges()
    previous_close = None
    for market_slice, emit in ((earlier, False), (bars, True)):
        for source in market_slice:
            bar = source if isinstance(source, Bar) else Bar.from_mapping(source)
            if previous_close is not None and bar.open_at < previous_close:
                raise ValueError(f"Chart OHLC overlaps or reverses at {bar.open_at}")
            previous_close = bar.close_at
            h1, h4, d1 = (frame.add(bar) for frame in frames)
            if emit:
                fingerprint = fingerprints.add(bar)
                session, observed = sessions.add(bar)
                yield MarketContext(bar, h1, h4, d1, fingerprint, session, observed)
