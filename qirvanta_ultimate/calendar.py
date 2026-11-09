"""Offline point-in-time release snapshots and fail-closed event windows."""
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from .common import finite, identifier, milliseconds, read_json


def source_host(value):
    uri = urlsplit(value)
    if uri.scheme != "https" or not uri.hostname or uri.username or uri.password or uri.query or uri.fragment:
        raise ValueError("Source requires credential-free HTTPS provenance")
    return uri.hostname


@dataclass(frozen=True)
class ReleaseRevision:
    event_id: str
    scheduled_ms: int
    observed_ms: int
    country: str
    currency: str
    title: str
    impact: str
    timezone: str
    source: str
    previous: float | None = None
    consensus: float | None = None
    consensus_observed_ms: int | None = None
    actual: float | None = None
    cancelled: bool = False

    def __post_init__(self):
        for v in (self.event_id, self.country, self.currency):
            identifier(v)
        milliseconds(self.scheduled_ms)
        milliseconds(self.observed_ms)
        source_host(self.source)
        ZoneInfo(self.timezone)
        if self.impact not in ("HIGH", "MEDIUM", "LOW", "UNKNOWN"):
            raise ValueError("Invalid impact")
        if not isinstance(self.title, str) or not self.title.strip() or len(self.title) > 200:
            raise ValueError("Invalid title")
        if type(self.cancelled) is not bool:
            raise ValueError("Invalid cancellation state")
        for value in (self.previous, self.consensus, self.actual):
            if value is not None:
                finite(value)
        if (self.consensus is None) != (self.consensus_observed_ms is None):
            raise ValueError("Forecast requires an availability timestamp")
        if self.consensus_observed_ms is not None:
            milliseconds(self.consensus_observed_ms)
            if not self.consensus_observed_ms <= self.observed_ms or self.consensus_observed_ms >= self.scheduled_ms:
                raise ValueError("Forecast must have been observed before release")
        if self.actual is not None and (self.observed_ms < self.scheduled_ms or self.cancelled):
            raise ValueError("Actual is unavailable before release or on a cancellation")

    @property
    def surprise(self):
        return None if self.actual is None or self.consensus is None else self.actual - self.consensus


@dataclass(frozen=True)
class CalendarSnapshot:
    provider: str
    source: str
    observed_ms: int
    coverage_start_ms: int
    coverage_end_ms: int
    currencies: tuple[str, ...]
    revisions: tuple[ReleaseRevision, ...]
    coverage_scope: tuple[str, ...] = ()

    def __post_init__(self):
        identifier(self.provider)
        host = source_host(self.source)
        for stamp in (self.observed_ms, self.coverage_start_ms, self.coverage_end_ms):
            milliseconds(stamp)
        if not self.coverage_start_ms < self.coverage_end_ms or not self.currencies:
            raise ValueError("Explicit nonempty calendar coverage required")
        for currency in self.currencies:
            identifier(currency)
        for scope in self.coverage_scope:
            identifier(scope)
        seen = set()
        for event in self.revisions:
            key = (event.event_id, event.observed_ms)
            if key in seen or event.observed_ms > self.observed_ms or source_host(event.source) != host:
                raise ValueError("Invalid revision provenance or duplicate revision")
            if event.currency not in self.currencies:
                raise ValueError("Release is outside declared currency coverage")
            seen.add(key)

    @classmethod
    def from_file(cls, path):
        return cls.from_object(read_json(path))

    @classmethod
    def from_object(cls, obj):
        obj = dict(obj)
        if obj.pop("schema", None) != "qirvanta.calendar.v1":
            raise ValueError("Unsupported calendar snapshot schema")
        obj["currencies"] = tuple(obj["currencies"])
        obj["revisions"] = tuple(ReleaseRevision(**r) for r in obj["revisions"])
        obj["coverage_scope"] = tuple(obj.get("coverage_scope", ()))
        return cls(**obj)


class CalendarProvider(Protocol):
    def snapshot(self, as_of_ms: int) -> CalendarSnapshot | None: ...


@dataclass(frozen=True)
class ArchivedCalendar:
    snapshots: tuple[CalendarSnapshot, ...]

    def snapshot(self, as_of_ms):
        return max((s for s in self.snapshots if s.observed_ms <= as_of_ms),
                   key=lambda s: s.observed_ms, default=None)


@dataclass(frozen=True)
class NewsRisk:
    status: str
    blocked: bool
    event_ids: tuple[str, ...] = ()
    provider: str | None = None


def assess_news(provider, *, as_of_ms, currencies=("USD",), before_ms=900000,
                after_ms=900000, max_age_ms=3600000, manual_pause=False,
                required_scope=("US_MAJOR_RELEASES",)):
    milliseconds(as_of_ms)
    if type(manual_pause) is not bool or any(type(v) is not int or v < 0 for v in (before_ms, after_ms, max_age_ms)):
        raise ValueError("Invalid news risk policy")
    if manual_pause:
        return NewsRisk("MANUAL_PAUSE", True)
    snapshot = None if provider is None else provider.snapshot(as_of_ms)
    if (snapshot is None or not 0 <= as_of_ms - snapshot.observed_ms <= max_age_ms
            or not set(currencies).issubset(snapshot.currencies)
            or not set(required_scope).issubset(snapshot.coverage_scope)
            or snapshot.coverage_start_ms > as_of_ms - after_ms
            or snapshot.coverage_end_ms < as_of_ms + before_ms):
        return NewsRisk("NEWS_DATA_UNAVAILABLE", True)
    latest = {}
    for revision in sorted(snapshot.revisions, key=lambda e: e.observed_ms):
        if revision.observed_ms <= as_of_ms:
            latest[revision.event_id] = revision
    active = tuple(sorted(e.event_id for e in latest.values()
                          if not e.cancelled and e.currency in currencies
                          and e.impact in ("HIGH", "UNKNOWN")
                          and e.scheduled_ms - before_ms <= as_of_ms <= e.scheduled_ms + after_ms))
    return NewsRisk("EVENT_WINDOW" if active else "CLEAR_IN_DECLARED_COVERAGE",
                    bool(active), active, snapshot.provider)
