"""Causal wick-cluster detection, frozen formations and research entry offers."""

from dataclasses import dataclass
from math import isfinite

from gold_pro.replay import Costs, Plan, round_away
from unified.tools.gold_market_data import Bar


@dataclass(frozen=True)
class Settings:
    atr_bars: int = 14
    formation_bars: int = 4
    min_rejections: int = 2
    min_range_atr: float = 0.50
    wick_fraction: float = 0.45
    opposing_wick_fraction: float = 0.30
    max_height_atr: float = 2.00
    breakout_window: int = 8
    breakout_clearance_atr: float = 0.05
    breakout_body_atr: float = 0.35
    retest_window: int = 6
    retest_touch_atr: float = 0.15
    retest_hold_atr: float = 0.10
    structure_bars: int = 20
    structure_proximity_atr: float = 0.40
    volume_bars: int = 20
    dominance_min: float = 0.65
    activity_min: float = 1.25
    stop_buffer_atr: float = 0.10
    minimum_risk_atr: float = 0.25
    maximum_risk_atr: float = 2.75
    max_friction_r: float = 0.30
    min_net_room_r: float = 0.70
    cash_risk_fraction: float = 0.0025
    max_hold: int = 72
    units: float = 3.0

    def __post_init__(self):
        counts = (self.atr_bars, self.breakout_window, self.retest_window, self.structure_bars,
                  self.volume_bars, self.max_hold)
        if any(value < 1 for value in counts) or not 2 <= self.min_rejections <= self.formation_bars:
            raise ValueError("Formation and lookback lengths are invalid")


@dataclass(frozen=True)
class ScanResult:
    events: tuple[dict, ...]
    plans: tuple[Plan, ...]
    features: tuple[dict, ...]


@dataclass
class Formation:
    side: int
    first_bar: int
    first_known_ms: int
    first_rejection_ms: int
    top: float
    bottom: float
    height_atr: float
    at_structure: bool
    formation_volume_available: bool
    pending: bool = False
    breakout_bar: int | None = None
    breakout_known_ms: int | None = None
    retest_attempted: bool = False

    @property
    def id(self):
        return f"WF|{self.side}|{self.first_known_ms}"

    @property
    def edge(self):
        return self.top if self.side == 1 else self.bottom

    @property
    def opposite_edge(self):
        return self.bottom if self.side == 1 else self.top


def _positive_volume(value):
    if isinstance(value, (float, int)) and not isinstance(value, bool) and isfinite(value) and value > 0:
        return value
    return None


def _mean_complete(values, size):
    if len(values) == size and all(value is not None for value in values):
        return sum(values) / size
    return None


def _in_window(index, now, first_index, first_known, maximum, interval):
    return index - first_index <= maximum and now - first_known <= maximum * interval


def _risk_geometry(candle, features, formation, costs, settings, equity, model):
    side, atr, reference = formation.side, features["prior_atr"], candle.close
    retest_extreme = candle.low if side == 1 else candle.high
    stop_base = retest_extreme if model == "WICK_RETEST" else formation.opposite_edge
    stop = round_away(stop_base - side * settings.stop_buffer_atr * atr, costs.tick, -side)
    risk = round(side * (reference - stop), 9)
    friction = costs.spread + 2 * costs.slip + 2 * costs.commission / costs.pointvalue
    barrier = features["prior_resistance" if side == 1 else "prior_support"]
    distance = side * (barrier - reference) if barrier is not None else None
    if distance is not None and distance < costs.tick - 1e-8:
        barrier = distance = None
    gross_room = min(risk, distance) if distance is not None else risk
    net_room = (gross_room - friction) / risk if risk > 0 else None
    cash_risk = (risk + friction) * costs.pointvalue * settings.units
    reasons = []
    if risk < max(costs.tick, settings.minimum_risk_atr * atr):
        reasons.append("STRUCTURE_INVALID")
    if risk > settings.maximum_risk_atr * atr:
        reasons.append("OVERSIZED_STOP")
    if cash_risk > equity * settings.cash_risk_fraction:
        reasons.append("CASH_RISK_CAP")
    if risk > 0 and friction / risk > settings.max_friction_r:
        reasons.append("COST_TOO_HIGH")
    if net_room is not None and net_room < settings.min_net_room_r:
        reasons.append("INSUFFICIENT_NET_ROOM")
    targets = tuple(round_away(reference + multiple * side * risk, costs.tick, side)
                    for multiple in (1, 2, 3)) if risk > 0 else ()
    return {
        "reference": reference, "stop": stop, "stop_distance": risk, "targets": targets,
        "known_barrier": barrier, "net_room_r": net_room, "estimated_risk_cash": cash_risk,
        "extension_atr": side * (reference - formation.edge) / atr,
        "risk_reasons": tuple(reasons),
    }


def scan(bars, costs=Costs(), *, minutes, context=None, settings=Settings(), equity=100_000):
    """Consume only the completed chart candles through each emitted decision.

    Context, when supplied, consists of completed H1/H4 side pairs aligned to
    chart bars. Context and volume flags classify an offer without changing the
    no-volume base arm. The caller chooses one execution arm and simulates fills.
    """
    if minutes not in (5, 15, 30):
        raise ValueError("Wick research supports M5, M15 and M30")
    if context is not None and len(context) != len(bars):
        raise ValueError("Wick and context inputs must align one-to-one")
    if not isfinite(equity) or equity <= 0:
        raise ValueError("Research equity must be finite and positive")
    if costs.tick <= 0 or costs.pointvalue <= 0:
        raise ValueError("Execution requires a positive tick size and point value")

    candles, true_ranges, volumes, features, events, plans = [], [], [], [], [], []
    interval = minutes * 60_000
    formation = None

    for index, row in enumerate(bars):
        candle = Bar.from_mapping(row)
        previous = candles[-1] if candles else None
        if candle.close_at - candle.open_at != interval or (previous and candle.open_at < previous.close_at):
            raise ValueError(f"Invalid candle time or overlap at {candle.open_at}")
        width = candle.high - candle.low
        prior_atr = _mean_complete(true_ranges[-settings.atr_bars:], settings.atr_bars)
        atr = prior_atr if prior_atr is not None and prior_atr > 0 else None
        true_ranges.append(width if previous is None else max(width, abs(candle.high - previous.close),
                                                               abs(candle.low - previous.close)))
        volume_mean = _mean_complete(volumes[-settings.volume_bars:], settings.volume_bars)
        volume = _positive_volume(row.get("volume"))
        volumes.append(volume)
        structure = candles[-settings.structure_bars:]
        prior_high = max(item.high for item in structure) if len(structure) == settings.structure_bars else None
        prior_low = min(item.low for item in structure) if len(structure) == settings.structure_bars else None
        lower_wick = (min(candle.open, candle.close) - candle.low) / width if width else None
        upper_wick = (candle.high - max(candle.open, candle.close)) / width if width else None
        eligible = atr is not None and width >= settings.min_range_atr * atr and width > 0
        buy = eligible and lower_wick >= settings.wick_fraction and upper_wick < settings.opposing_wick_fraction
        sell = eligible and upper_wick >= settings.wick_fraction and lower_wick < settings.opposing_wick_fraction
        near_support = (atr is not None and prior_low is not None
                        and abs(candle.low - prior_low) <= settings.structure_proximity_atr * atr)
        near_resistance = (atr is not None and prior_high is not None
                           and abs(candle.high - prior_high) <= settings.structure_proximity_atr * atr)
        h1, h4 = context[index] if context is not None else (None, None)
        previous_cluster = features[-(settings.formation_bars - 1):]
        window = candles[-(settings.formation_bars - 1):] + [candle]
        sequence_ready = (len(window) == settings.formation_bars and atr is not None
                          and all(item["prior_atr"] is not None for item in previous_cluster)
                          and all(a.close_at == b.open_at for a, b in zip(window, window[1:])))
        buy_count = int(buy) + sum(item["buy_rejection"] for item in previous_cluster)
        sell_count = int(sell) + sum(item["sell_rejection"] for item in previous_cluster)
        feature = {
            "bar": index, "time_ms": candle.close_at, "prior_atr": atr, "upper_wick_fraction": upper_wick,
            "lower_wick_fraction": lower_wick, "buy_rejection": buy, "sell_rejection": sell,
            "buy_sequence_count": buy_count, "sell_sequence_count": sell_count,
            "sequence_ready": sequence_ready, "near_support": near_support,
            "near_resistance": near_resistance, "prior_resistance": prior_high, "prior_support": prior_low,
            "volume_available": volume is not None, "volume_activity_ratio": (
                volume / volume_mean if volume is not None and volume_mean is not None else None),
            "h1": h1, "h4": h4,
        }
        features.append(feature)
        candles.append(candle)

        def emit(state, side, reason=None, model=None, plan_id=None, **extra):
            active = formation
            directional_location = None if volume is None or not width or side == 0 else (
                (candle.close - candle.low) / width if side == 1 else (candle.high - candle.close) / width)
            activity = feature["volume_activity_ratio"]
            events.append({
                "bar": index, "time_ms": candle.close_at, "state": state, "side": side,
                "formation_id": active.id if active else None,
                "first_known_ms": active.first_known_ms if active else None,
                "root_watch_ms": active.first_known_ms if active else None,
                "first_rejection_ms": active.first_rejection_ms if active else None,
                "top": active.top if active else None, "bottom": active.bottom if active else None,
                "formation_bars": settings.formation_bars if active else None,
                "height_atr": active.height_atr if active else None,
                "at_structure": active.at_structure if active else None,
                "age_bars": index - active.first_bar if active else None,
                "age_ms": candle.close_at - active.first_known_ms if active else None,
                "expires_ms": (
                    (active.breakout_known_ms + settings.retest_window * interval)
                    if active.breakout_known_ms is not None
                    else (active.first_known_ms + settings.breakout_window * interval)
                ) if active else None,
                "breakout_bar": active.breakout_bar if active else None,
                "breakout_known_ms": active.breakout_known_ms if active else None,
                "formation_volume_available": active.formation_volume_available if active else None,
                "volume_available": volume is not None, "directional_close_location": directional_location,
                "volume_dominance": directional_location >= settings.dominance_min if directional_location is not None else None,
                "volume_activity": activity >= settings.activity_min if activity is not None else None,
                "volume_activity_ratio": activity, "h1": h1, "h4": h4,
                "htf_aligned": h1 == h4 == side if side and h1 is not None and h4 is not None else None,
                "reason": reason, "model": model, "plan_id": plan_id, **extra,
            })

        def offer(model):
            side = formation.side
            plan_id = f"{model}|{side}|{formation.first_known_ms}|{candle.close_at}"
            geometry = _risk_geometry(candle, feature, formation, costs, settings, equity, model)
            if geometry["risk_reasons"]:
                emit("RISK_REJECTED", side, geometry["risk_reasons"][0], model, plan_id, **geometry)
                return
            emit("ENTRY_VALID", side, "PRICE_AND_RISK_VALID", model, plan_id, **geometry)
            plans.append(Plan(plan_id, model, side, index, candle.close, geometry["stop"],
                              geometry["targets"], atr, settings.max_hold, settings.units))

        if formation is not None:
            side, edge = formation.side, formation.edge
            if formation.breakout_bar is None:
                if not _in_window(index, candle.close_at, formation.first_bar, formation.first_known_ms,
                                  settings.breakout_window, interval):
                    emit("FORMATION_EXPIRED", side, "BREAKOUT_WINDOW_EXPIRED")
                    formation = None
                elif side * (candle.close - formation.opposite_edge) < 0:
                    emit("FORMATION_EXPIRED", side, "OPPOSITE_EDGE_CLOSE")
                    formation = None
                elif atr is not None:
                    through = side * (candle.close - edge)
                    body = side * (candle.close - candle.open)
                    extreme = candle.high if side == 1 else candle.low
                    opposite_extreme = candle.low if side == 1 else candle.high
                    clearance = settings.breakout_clearance_atr * atr
                    if through >= clearance and body >= settings.breakout_body_atr * atr:
                        formation.breakout_bar, formation.breakout_known_ms = index, candle.close_at
                        emit("BUY_BREAKOUT_CONFIRMED" if side == 1 else "SELL_BREAKOUT_CONFIRMED", side,
                             "DIRECTIONAL_CLOSE", "WICK_BREAKOUT")
                        offer("WICK_BREAKOUT")
                        emit("RETEST_WATCH", side, "BREAKOUT_EDGE_KNOWN", "WICK_RETEST")
                    elif not formation.pending and side * (extreme - edge) >= clearance:
                        formation.pending = True
                        both_edges = side * (opposite_extreme - formation.opposite_edge) <= -clearance
                        reason = "BOTH_EDGES_PROBED" if both_edges else (
                            "INSUFFICIENT_CLOSE_DISPLACEMENT" if through >= clearance else "EXTREME_WITHOUT_CLOSE")
                        emit("BREAKOUT_PENDING", side, reason, "WICK_BREAKOUT")
            elif not _in_window(index, candle.close_at, formation.breakout_bar, formation.breakout_known_ms,
                                settings.retest_window, interval):
                emit("FORMATION_EXPIRED", side, "RETEST_WINDOW_EXPIRED")
                formation = None
            elif side * (candle.close - edge) <= 0:
                emit("FALSE_BREAKOUT", side, "CLOSE_BACK_INSIDE", "WICK_BREAKOUT")
                formation = None
            elif atr is not None and not formation.retest_attempted:
                touch = candle.low if side == 1 else candle.high
                if (abs(touch - edge) <= settings.retest_touch_atr * atr
                        and side * (candle.close - edge) >= settings.retest_hold_atr * atr
                        and side * (candle.close - candle.open) > 0):
                    formation.retest_attempted = True
                    offer("WICK_RETEST")
            continue

        cluster = features[-settings.formation_bars:]
        if not sequence_ready:
            continue
        if buy_count and sell_count:
            if buy or sell:
                emit("FORMATION_BUILDING", 0, "OPPOSING_REJECTIONS")
            continue
        side = 1 if buy_count else -1 if sell_count else 0
        if not side:
            continue
        evidence_key = "buy_rejection" if side == 1 else "sell_rejection"
        if max(buy_count, sell_count) < settings.min_rejections:
            if feature[evidence_key]:
                emit("FORMATION_BUILDING", side, "FIRST_REJECTION")
            continue
        top, bottom = max(item.high for item in window), min(item.low for item in window)
        height = (top - bottom) / atr
        if height > settings.max_height_atr:
            if feature[evidence_key]:
                emit("FORMATION_BUILDING", side, "RANGE_TOO_WIDE")
            continue
        first_rejection = next(item["time_ms"] for item in cluster if item[evidence_key])
        near_key = "near_support" if side == 1 else "near_resistance"
        at_structure = any(item[evidence_key] and item[near_key] for item in cluster)
        formation = Formation(side, index, candle.close_at, first_rejection, top, bottom, height,
                              at_structure,
                              all(item["volume_available"] for item in cluster))
        emit("BUY_WATCH" if side == 1 else "SELL_WATCH", side, "WICK_CLUSTER_CONFIRMED")

    return ScanResult(tuple(events), tuple(plans), tuple(features))
