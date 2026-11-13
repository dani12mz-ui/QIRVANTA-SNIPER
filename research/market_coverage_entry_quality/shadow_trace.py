"""Trace the existing Shadow observation gates on the registered hours."""

from bisect import bisect_left
from collections import defaultdict

from gold_pro.direct_trend_breakout import (
    BREAK_BODY_ATR,
    BREAK_BUFFER_ATR,
    BREAK_TERMINAL_RANGE,
    CONSOLIDATION,
    CONSOLIDATION_BARS,
    CONSOLIDATION_MAX_ATR,
    STRUCTURAL,
    WATCH_DISTANCE_ATR,
    _setups,
    closed_context,
)


PRE_HOUR_MS = 15 * 60_000
FIRST_MODELS = (STRUCTURAL, CONSOLIDATION)


def identity(event):
    return event["model"], event["side"], event["source_ms"], event["level"]


def structure_gate(ctx, side):
    defended, directional = (ctx.known_highs, ctx.known_lows) if side > 0 else (ctx.known_lows, ctx.known_highs)
    result = {
        "defended_count": len(defended),
        "directional_count": len(directional),
        "directional_delta": side * (directional[-1].price - directional[-2].price)
        if len(directional) >= 2 else None,
    }
    reasons = []
    if not defended:
        reasons.append("NO_CONFIRMED_DEFENDED_PIVOT")
    if len(directional) < 2:
        reasons.append("FEWER_THAN_TWO_DIRECTIONAL_PIVOTS")
    elif result["directional_delta"] <= 0:
        reasons.append("DIRECTIONAL_PIVOT_SHAPE")
    return result, reasons


def consolidation_gate(bars, index, ctx, side):
    if ctx.segment_bars <= CONSOLIDATION_BARS:
        return {}, ["CONSOLIDATION_HISTORY_RESET"]
    if ctx.prior_ema_slope is None:
        return {}, ["EMA_SLOPE_UNAVAILABLE"]
    side_of_ema = side * (bars[index - 1]["close"] - ctx.prior_ema)
    slope = side * ctx.prior_ema_slope
    result = {"directional_ema_distance": side_of_ema, "directional_ema_slope": slope}
    reasons = []
    if side_of_ema <= 0:
        reasons.append("PREVIOUS_CLOSE_WRONG_SIDE_OF_EMA")
    if slope <= 0:
        reasons.append("EMA_SLOPE_WRONG_DIRECTION")
    if not reasons:
        box = bars[index - CONSOLIDATION_BARS:index]
        width = max(bar["high"] for bar in box) - min(bar["low"] for bar in box)
        result.update(box_width=width, max_box_width=CONSOLIDATION_MAX_ATR * ctx.prior_atr)
        if width > result["max_box_width"]:
            reasons.append("CONSOLIDATION_TOO_WIDE")
    return result, reasons


def model_gate(bar, previous, atr, side, offer, active, consumed, observed):
    ended = {identity(event) for event in observed if event["state"] in {"EXPIRED", "INVALIDATED"}}
    active_for_offer = active == offer.identity and active not in ended
    distance = side * (offer.level - bar["close"])
    direction_step = side * (bar["close"] - previous["close"])
    beyond = -distance
    previous_beyond = side * (previous["close"] - offer.level)
    buffer = BREAK_BUFFER_ATR * atr
    approaching = 0 <= distance <= WATCH_DISTANCE_ATR * atr and direction_step > 0
    crossed = previous_beyond <= buffer < beyond
    body = side * (bar["close"] - bar["open"])
    wick = bar["high"] - bar["close"] if side > 0 else bar["close"] - bar["low"]
    wick_cap = BREAK_TERMINAL_RANGE * (bar["high"] - bar["low"])
    displaced = body >= BREAK_BODY_ATR * atr and wick <= wick_cap
    used = offer.identity in consumed or offer.identity in ended
    reasons = []
    if used:
        reasons.append("SETUP_IDENTITY_ALREADY_CONSUMED")
    elif not any(event["state"] in {"WATCH", "BREAKOUT CONFIRMED"} for event in observed):
        if active_for_offer:
            reasons.append("WATCH_ALREADY_ACTIVE")
        elif not approaching:
            if distance < 0:
                reasons.append("CLOSE_ALREADY_PAST_WATCH_LEVEL")
            elif distance > WATCH_DISTANCE_ATR * atr:
                reasons.append("OUTSIDE_WATCH_DISTANCE")
            if direction_step <= 0:
                reasons.append("NO_DIRECTIONAL_APPROACH")
        if not crossed:
            if beyond <= buffer:
                reasons.append("CLOSE_DID_NOT_CLEAR_BREAK_BUFFER")
            elif previous_beyond > buffer:
                reasons.append("NO_FIRST_BUFFER_CROSS")
        elif not displaced:
            if body < BREAK_BODY_ATR * atr:
                reasons.append("BREAK_BODY_BELOW_MINIMUM")
            if wick > wick_cap:
                reasons.append("BREAK_CLOSE_TOO_FAR_FROM_EXTREME")
    return {
        "level": offer.level,
        "level_known_ms": offer.level_known_ms,
        "level_source_ms": offer.source_ms,
        "watch_identity_at_start": active,
        "active_watch_for_offer": active_for_offer,
        "consumed_identity": used,
        "distance_to_watch_level": distance,
        "directional_close_change": direction_step,
        "previous_distance_beyond_level": previous_beyond,
        "break_buffer": buffer,
        "directional_body": body,
        "terminal_wick": wick,
        "approach_qualified": approaching,
        "first_cross": crossed,
        "displacement_qualified": displaced,
    }, reasons


def event_detail(event):
    return {name: event[name] for name in ("state", "reason", "level", "level_known_ms", "source_ms", "watch_ms")
            if name in event}


def trace(bars, events, hours):
    """Return same-side diagnostic rows keyed by the significant hour's start ms.

    Use the complete ordered, unfiltered Shadow stream from the existing scan.
    Hours select report rows only. Indicator state and gates never read them.
    """
    by_bar = defaultdict(list)
    previous_index = -1
    for event in events:
        index = event["bar"]
        if not 0 <= index < len(bars) or index < previous_index or event["time_ms"] != bars[index]["time_close"]:
            raise ValueError("Shadow events must be ordered and match the exact OHLC tape")
        by_bar[index].append(event)
        previous_index = index

    hours = tuple(hours)
    report = {hour["start"]: [] for hour in hours}
    if len(report) != len(hours):
        raise ValueError("Significant hour starts must be unique")
    by_stamp = defaultdict(list)
    stamps = [bar["time_close"] for bar in bars]
    for hour in hours:
        if hour["side"] not in (1, -1) or hour["end"] <= hour["start"]:
            raise ValueError("Invalid significant hour")
        first = bisect_left(stamps, hour["start"] - PRE_HOUR_MS)
        end = bisect_left(stamps, hour["end"])
        for stamp in stamps[first:end]:
            by_stamp[stamp].append(hour)

    watched, consumed = {}, set()
    for index, (bar, ctx) in enumerate(zip(bars, closed_context(bars))):
        source_rows = by_bar.get(index, ())
        targets = by_stamp.get(bar["time_close"], ())
        if targets:
            previous = bars[index - 1] if index else None
            for hour in targets:
                side = hour["side"]
                details = {
                    "known_ms": bar["time_close"],
                    "side": side,
                    "segment_bars": ctx.segment_bars,
                    "prior_atr": ctx.prior_atr,
                }
                if previous is None or not ctx.prior_atr or ctx.prior_atr <= 0:
                    common = "PRIOR_ATR_OR_BAR_UNAVAILABLE"
                else:
                    common = "DATA_GAP_RESET" if ctx.segment_bars <= 1 else None
                offers = {offer.model: offer for offer in _setups(bars, index, ctx, side)} if not common else {}
                for model, name in ((STRUCTURAL, "structure"), (CONSOLIDATION, "consolidation")):
                    observed = [row for row in source_rows if row["side"] == side and row["model"] == model]
                    if common:
                        context, blockers = {}, [common]
                    elif model == STRUCTURAL:
                        context, blockers = structure_gate(ctx, side)
                    else:
                        context, blockers = consolidation_gate(bars, index, ctx, side)
                    offer = offers.get(model)
                    if bool(offer) == bool(blockers):
                        raise ValueError(f"Trace disagrees with {model} offer at bar {index}")
                    if offer:
                        shape, barriers = model_gate(bar, previous, ctx.prior_atr, side, offer,
                                                     watched.get((model, side)), consumed, observed)
                        context.update(shape)
                        blockers.extend(barriers)
                    details[name] = {
                        "setup_eligible": offer is not None,
                        **context,
                        "blockers": blockers,
                        "events": [event_detail(row) for row in observed],
                    }
                report[hour["start"]].append(details)

        # The native order is expiry/invalidity, potential WATCH, then confirmation.
        # Observe those changes after recording the current bar's eligibility.
        for event in source_rows:
            model, side, state = event["model"], event["side"], event["state"]
            if model not in FIRST_MODELS:
                continue
            key = model, side
            if state == "WATCH":
                watched[key] = identity(event)
            elif state in {"EXPIRED", "INVALIDATED", "BREAKOUT CONFIRMED"}:
                consumed.add(identity(event))
                watched.pop(key, None)
    return report
