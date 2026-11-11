"""Research-only entry decisions on the existing confirmed Direct Trend Shadow tape."""

from dataclasses import dataclass
from decimal import Decimal
from math import isclose, isfinite

from research.data.loader import validate_bars

from .direct_trend_breakout import (
    CASH_RISK_LIMIT,
    CONSOLIDATION,
    MAX_EXTENSION_ATR,
    MAX_FRICTION_R,
    MAX_HOLD,
    MAX_STOP_ATR,
    MIN_NET_ROOM_R,
    MIN_STOP_ATR,
    MODELS,
    RETEST,
    STRUCTURAL,
    UNITS,
    WINDOW_BARS,
    scan,
)
from .replay import Costs, Plan


# Registered before the comparison. The existing shadow detector owns geometry and lifecycle.
PRECISION_MIN_ROOM_R = 0.90
STRUCTURAL_MAX_EXTENSION_ATR = 0.75
RETEST_MIN_AGE_BARS = 2
RETEST_MIN_SEPARATION_ATR = 0.25
CONSOLIDATION_MIN_BODY_ATR = 0.65
MODEL_NAMES = (STRUCTURAL, RETEST, CONSOLIDATION)
_LINEAGE_FIELDS = (
    "model", "side", "bar", "time_ms", "watch_ms", "source_ms",
    "level", "level_known_ms", "breakout_ms", "origin_model", "prior_atr", "close",
)


@dataclass(frozen=True)
class EntryAssessment:
    event: dict
    plan: Plan | None
    detection_ms: int
    confirmation_ms: int
    origin_detection_ms: int
    origin_confirmation_ms: int
    next_executable_ms: int
    expires_ms: int
    costs: Costs
    stop_risk: float
    friction_price: float
    risk_cash: float
    net_room_r: float | None
    extension_atr: float
    body_atr: float
    retest_age_bars: int | None
    retest_separation_atr: float | None
    shadow_reasons: tuple[str, ...]
    original_reasons: tuple[str, ...]
    precision_reasons: tuple[str, ...]

    @property
    def source(self):
        return self.event["model"]

    @property
    def direction(self):
        return "BUY" if self.event["side"] == 1 else "SELL"

    @property
    def reasons(self):
        return self.original_reasons + self.precision_reasons

    @property
    def accepted(self):
        return self.plan is not None and not self.reasons

    @property
    def risk_rejection_reason(self):
        return self.reasons[0] if self.reasons else None


def _number(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)) or not isfinite(value):
        raise ValueError(f"Invalid shadow number {field}")
    return float(value)


def _stamp(event, field):
    value = event.get(field)
    if type(value) is not int:
        raise ValueError(f"Shadow {field} must be an integer UTC timestamp")
    return value


def _origin_key(event, model=None):
    return (model or event["model"], event["side"], _stamp(event, "breakout_ms"),
            _stamp(event, "source_ms"), _number(event["level"], "level"))


def _confirmed_pairs(bars, events, interval):
    """Join only same-close geometry and previously seen frozen origins."""
    origins = {}
    active_retests = set()
    pending = None
    last_bar = -1
    for event in events:
        index = event.get("bar")
        if type(index) is not int or not 0 <= index < len(bars) or index < last_bar:
            raise ValueError("Shadow events must be ordered and refer to the supplied chart")
        known = _stamp(event, "time_ms")
        if known != bars[index]["time_close"]:
            raise ValueError("Shadow event is not known at the corresponding chart close")
        last_bar = index
        state = event.get("state")
        if pending is not None:
            confirmation, origin = pending
            if (state not in {"ENTRY CANDIDATE", "REJECTED"} or "plan_id" not in event
                    or any(event.get(field) != confirmation.get(field) for field in _LINEAGE_FIELDS)):
                raise ValueError("Confirmed shadow event is missing its immediate same-close geometry")
            yield confirmation, event, origin
            pending = None
            continue
        if "plan_id" in event:
            raise ValueError("Shadow geometry has no immediately preceding confirmation")
        model, side = event.get("model"), event.get("side")
        if model not in MODELS or type(side) is not int or side not in (-1, 1):
            raise ValueError("Invalid shadow model or direction")
        if state == "RETEST WATCH" or (model == RETEST and state in {"EXPIRED", "INVALIDATED"}):
            origin_model = event.get("origin_model")
            if model != RETEST or origin_model not in (STRUCTURAL, CONSOLIDATION):
                raise ValueError("Retest must cite an existing origin model")
            key = _origin_key(event, origin_model)
            if state == "RETEST WATCH":
                if key not in origins or key[2] != known or _stamp(event, "watch_ms") != known:
                    raise ValueError("Retest watch has no confirmed same-close origin")
                active_retests.add(key)
            else:
                active_retests.discard(key)
        if state != "BREAKOUT CONFIRMED":
            continue
        if not (_stamp(event, "source_ms") <= _stamp(event, "level_known_ms")
                <= _stamp(event, "watch_ms") <= known):
            raise ValueError("Shadow source or detection is unavailable at confirmation")
        if model == RETEST:
            origin_model = event.get("origin_model")
            if origin_model not in (STRUCTURAL, CONSOLIDATION):
                raise ValueError("Retest must cite an existing origin model")
            key = _origin_key(event, origin_model)
            if key not in active_retests:
                raise ValueError("Retest confirmation has no active known-at origin")
            origin = origins[key]
            age = index - origin["bar"]
            if not (1 <= age <= WINDOW_BARS and key[2] == _stamp(event, "watch_ms")
                    and known - key[2] == age * interval):
                raise ValueError("Retest confirmation exceeds its contiguous shadow lifecycle")
            active_retests.remove(key)
        else:
            key = _origin_key(event)
            if key[2] != known or key in origins:
                raise ValueError("Invalid or duplicate origin confirmation")
            origins[key] = origin = event
        pending = event, origin
    if pending is not None:
        raise ValueError("Confirmed shadow event is missing its geometry")


def _validate_costs(costs):
    values = (costs.tick, costs.pointvalue, costs.commission, costs.ticks, costs.spread)
    if (not all(map(isfinite, values)) or costs.tick <= 0 or costs.pointvalue <= 0
            or min(costs.commission, costs.ticks, costs.spread) < 0):
        raise ValueError("Invalid precision research costs")


def _original_gates(risk, atr, extension, friction, room, risk_cash, tick):
    # Reprice the existing risk checks for the chosen costs. An old same-bar
    # overlap is portfolio arbitration and cannot reject a standalone hypothesis.
    return tuple(reason for reject, reason in (
        (risk < max(tick, MIN_STOP_ATR * atr), "STOP_TOO_SMALL"),
        (risk > MAX_STOP_ATR * atr, "STOP_TOO_WIDE"),
        (extension > MAX_EXTENSION_ATR, "LATE_EXTENSION"),
        (risk > 0 and friction > MAX_FRICTION_R * risk, "COST_TOO_HIGH"),
        (room is not None and room < MIN_NET_ROOM_R, "INSUFFICIENT_NET_ROOM"),
        (risk_cash > CASH_RISK_LIMIT, "CASH_RISK_LIMIT"),
    ) if reject)


def _evaluate(bars, interval, confirmation, geometry, origin, costs):
    index, model, side = (confirmation[key] for key in ("bar", "model", "side"))
    known = confirmation["time_ms"]
    bar = bars[index]
    reference, stop, risk, atr = (
        _number(value, field) for value, field in (
            (geometry["reference"], "reference"), (geometry["stop"], "stop"),
            (geometry["risk"], "risk"), (confirmation["prior_atr"], "prior_atr"),
        )
    )
    if (atr <= 0 or reference != bar["close"] or reference != _number(confirmation["close"], "close")
            or not isclose(risk, side * (reference - stop), rel_tol=0, abs_tol=2e-8)):
        raise ValueError("Frozen shadow geometry disagrees with the confirmed chart bar")
    if geometry["plan_id"] != f"DT|{model}|{side}|{known}":
        raise ValueError("Shadow plan identity does not match its confirmed decision")
    targets = tuple(_number(value, "target") for value in geometry["targets"])
    if risk > 0:
        if len(targets) != 3 or any(side * (right - left) <= 0
                                    for left, right in zip((reference,) + targets, targets)):
            raise ValueError("Invalid frozen shadow target ordering")
        plan = Plan(geometry["plan_id"], model, side, index, reference, stop, targets, atr, MAX_HOLD, UNITS)
    else:
        plan = None
    raw_obstacle = geometry.get("obstacle")
    remaining = None if raw_obstacle is None else side * (_number(raw_obstacle, "obstacle") - reference)
    if remaining is not None and remaining <= 0:
        raise ValueError("Shadow obstacle must be ahead of the confirmed reference")
    friction = costs.spread + 2 * costs.slip + 2 * costs.commission / costs.pointvalue
    room = ((risk if remaining is None else min(risk, remaining)) - friction) / risk if risk > 0 else None
    risk_cash = (risk + friction) * costs.pointvalue * UNITS
    extension = max(0.0, side * (reference - _number(confirmation["level"], "level")) / atr)
    body = side * (bar["close"] - bar["open"]) / atr
    age = separation = None
    precision = []
    if room is not None and room < PRECISION_MIN_ROOM_R:
        precision.append("PE_INSUFFICIENT_NET_ROOM")
    if model == STRUCTURAL and extension > STRUCTURAL_MAX_EXTENSION_ATR:
        precision.append("PE_STRUCTURAL_MAX_EXTENSION")
    if model == RETEST:
        age = index - origin["bar"]
        origin_atr = _number(origin["prior_atr"], "origin prior_atr")
        if origin_atr <= 0:
            raise ValueError("Invalid ATR at the retest origin")
        level = _number(confirmation["level"], "level")
        separation = max((side * (row["close"] - level) / origin_atr
                          for row in bars[origin["bar"] + 1:index]), default=None)
        if age < RETEST_MIN_AGE_BARS:
            precision.append("PE_RETEST_TOO_EARLY")
        if separation is None or separation < RETEST_MIN_SEPARATION_ATR:
            precision.append("PE_RETEST_UNESTABLISHED")
    if model == CONSOLIDATION and body < CONSOLIDATION_MIN_BODY_ATR:
        precision.append("PE_CONSOLIDATION_WEAK_BODY")
    original = _original_gates(risk, atr, extension, friction, room, risk_cash, costs.tick)
    return EntryAssessment(
        event=dict(confirmation),
        plan=plan,
        detection_ms=confirmation["watch_ms"],
        confirmation_ms=known,
        origin_detection_ms=origin["watch_ms"],
        origin_confirmation_ms=origin["time_ms"],
        next_executable_ms=known,
        expires_ms=known + interval,
        costs=costs,
        stop_risk=risk,
        friction_price=friction,
        risk_cash=risk_cash,
        net_room_r=room,
        extension_atr=extension,
        body_atr=body,
        retest_age_bars=age,
        retest_separation_atr=separation,
        shadow_reasons=tuple(geometry["reasons"]),
        original_reasons=original,
        precision_reasons=tuple(precision),
    )


def assess(bars, events=None, costs=Costs()):
    """Return a decision at each confirmed shadow break with no lookahead to fills.

    Supply the full shadow tape for the same chart (or let the pinned scan build
    it). The planned fill time is the immediate next contiguous open, and the
    replay owns gap rejection, actual fills, occupancy and eventual outcomes.
    """
    interval = validate_bars(bars)
    _validate_costs(costs)
    if events is None:
        events, _ = scan(bars)
    return tuple(_evaluate(bars, interval, confirmed, geometry, origin, costs)
                 for confirmed, geometry, origin in _confirmed_pairs(bars, events, interval))
