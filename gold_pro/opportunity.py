"""Causal early warnings and independently executable follow-up setups."""

from collections import defaultdict
from dataclasses import dataclass
from statistics import median

from unified.tools.evaluate_v21_impulse import HOUR, hourly_events
from .replay import Plan, round_away


@dataclass
class Watch:
    bar: int
    anchor: float
    structure_bar: int | None = None
    rejected_bar: int | None = None


def _geometry(side, i, bars, features, watch, atr, stop_base, costs, policy, equity):
    b, f = bars[i], features[i]
    close = b["close"]
    stop = round_away(stop_base - side * .10 * atr, costs.tick, -side)
    risk = round(side * (close - stop), 9)
    friction = costs.spread + 2 * costs.slip + 2 * costs.commission / costs.pointvalue
    prior_day = b.get("priorDayHigh" if side == 1 else "priorDayLow")
    pivots = f.highs if side == 1 else f.lows
    known_barriers = [p[1] for p in pivots] + [prior_day]
    distances = [side * (level - close) for level in known_barriers
                 if level is not None and side * (level - close) > 0]
    barrier = min(distances, default=None)
    gross_room = min(risk, barrier) if barrier is not None else risk
    details = {
        "invalidation": stop, "initial_move_atr": max(0., side * (close - watch.anchor) / atr),
        "stop_distance": risk, "known_barrier": close + side * barrier if barrier is not None else None,
        "net_room_r": (gross_room - friction) / risk if risk > 0 else None,
        "signal_age_bars": i - watch.bar,
        "signal_age_minutes": (b["time_close"] - bars[watch.bar]["time_close"]) / 60_000,
        "estimated_risk_cash": (risk + friction) * costs.pointvalue * 3,
    }
    reasons = []
    if risk < costs.tick or risk < .25 * atr:
        reasons.append("STRUCTURE_INVALID")
    if risk > policy["max_stop_atr"] * atr:
        reasons.append("OVERSIZED_STOP")
    if (risk + friction) * costs.pointvalue * 3 > equity * .0025:
        reasons.append("CASH_RISK_CAP")
    if risk > 0 and friction / risk > .30:
        reasons.append("COST_TOO_HIGH")
    if details["net_room_r"] is not None and details["net_room_r"] < .70:
        reasons.append("INSUFFICIENT_NET_ROOM")
    return stop, risk, reasons, details


def scan(bars, features, context, settings, costs, equity, *, mode, enabled=None):
    """Consume bars once. No observations, geometry or admissions use an event label."""
    if len(bars) != len(features) or len(bars) != len(context):
        raise ValueError("Opportunity inputs must align one-to-one")
    policy = settings[mode.lower()]
    models = settings["models"]
    enabled = set(models if enabled is None else enabled)
    if not enabled or not enabled <= set(models):
        raise ValueError("Invalid model subset")
    watches, known_break, last_model_bar = {}, {}, {}
    previous_trigger = {1: False, -1: False}
    decisions, plans = [], []

    for i in range(settings["warmup_bars"], len(bars)):
        b, f, prev = bars[i], features[i], features[i - 1]
        atr = f.atr
        if atr is None or atr <= 0:
            continue
        close, opening = b["close"], b["open"]
        fast = mode == "FAST"
        i_time = b["time_close"]
        recent = bars[i-4:i]
        ranges = [x["high"] - x["low"] for x in recent]
        displacement = (close - bars[i-3]["close"]) / atr
        direction_path = sum(abs(bars[j]["close"] - bars[j-1]["close"]) for j in range(i-3, i+1))
        efficiency = abs(close - recent[0]["close"]) / direction_path if direction_path else 0.
        for side in (1, -1):
            far = "high" if side == 1 else "low"
            near = "low" if side == 1 else "high"
            choose_far = max if side == 1 else min
            choose_near = min if side == 1 else max
            continuation_level = choose_far(x[far] for x in recent[-3:])
            local_stop = choose_near(x[near] for x in bars[i-1:i+1])
            wide_stop = choose_near(x[near] for x in bars[i-4:i+1])
            advancing = side * displacement
            prior_momentum = side * (bars[i-1]["close"] - bars[i-4]["close"]) / atr
            favorable_close = side * (close - opening) > 0
            swing_set = f.highs if side == 1 else f.lows
            structural_level = swing_set[-1][1] if swing_set else None
            crossed = side * (close - continuation_level) > 0
            shallow = abs(recent[-1]["close"] - recent[-2]["close"]) <= .65 * atr
            acceleration = advancing - prior_momentum
            early = f.bos == side or (crossed and efficiency >= .54 and favorable_close and
                                       (f.displacement == side or (advancing >= .65 and acceleration >= .15)))
            watch = watches.get(side)

            def emit(state, model="DIRECTION", reason=None, **extra):
                row = {"time_ms": i_time, "bar": i, "mode": mode, "side": side,
                       "model": model, "state": state, "reason": reason,
                       "root_watch_ms": bars[watch.bar]["time_close"] if watch else i_time}
                decisions.append({**row, **extra})

            if watch and (i - watch.bar >= settings["watch_bars"]
                          or i_time - bars[watch.bar]["time_close"] > settings["watch_bars"] * (b["time_close"] - b["time"])):
                emit("OPPORTUNITY_EXPIRED", reason="WINDOW_EXPIRED")
                watches.pop(side)
                watch = None
            if watch and f.mss and f.bos == -side:
                emit("OPPORTUNITY_EXPIRED", reason="OPPOSITE_STRUCTURE")
                watches.pop(side)
                watch = None
            if f.mss and f.bos == -side:
                known_break.pop(side, None)
            if early and watch is None and (not previous_trigger[side] or f.bos == side):
                anchor = choose_near(x[near] for x in recent)
                watch = watches[side] = Watch(i, anchor, i if f.bos == side else None)
                emit("WATCH", reason="STRUCTURAL_BREAK" if f.bos == side else "DIRECTIONAL_EXPANSION",
                     known_structure_ms=i_time if f.bos == side else None, efficiency=efficiency)
            previous_trigger[side] = early
            retest = known_break.get(side)
            if f.bos == side and structural_level is not None:
                known_break[side] = (i, structural_level)
                if watch and watch.structure_bar is None:
                    watch.structure_bar = i
            relevant_retest = retest is not None and 1 <= i - retest[0] <= settings["watch_bars"]
            if relevant_retest:
                relevant_retest = i_time - bars[retest[0]]["time_close"] <= settings["watch_bars"] * (b["time_close"] - b["time"])
            near_retest = (relevant_retest and abs(b[near] - retest[1]) <= .25 * atr
                           and side * (close - retest[1]) > .10 * atr and favorable_close)
            near_ema = (f.trend == side and abs(b[near] - prev.ema21) <= .30 * atr
                        and side * (close - prev.ema21) > .10 * atr and favorable_close)
            prior_box_range = max(x["high"] for x in recent) - min(x["low"] for x in recent)
            box_break = side * (close - choose_far(x[far] for x in recent)) > 0
            conditions = {
                "BREAKOUT": f.bos == side or (fast and early and crossed),
                "PULLBACK": near_ema,
                "RETEST": near_retest,
                "CONTINUATION": f.trend == side and crossed and shallow and efficiency >= .50,
                "COMPRESSION": prior_box_range <= 1.40 * atr and median(ranges) <= .75 * atr and box_break,
            }
            for model in models:
                if model not in enabled or not conditions[model]:
                    continue
                if advancing < policy["min_momentum_atr"] and not (fast and f.bos == side):
                    continue
                if not fast and (f.trend != side or (model == "BREAKOUT" and f.displacement != side)):
                    continue
                key = (model, side)
                if i - last_model_bar.get(key, -100) <= settings["model_cooldown_bars"]:
                    continue
                last_model_bar[key] = i
                if watch is None:
                    watch = watches[side] = Watch(i, choose_near(x[near] for x in recent),
                                                  i if f.bos == side else None)
                    emit("WATCH", reason="INDEPENDENT_MODEL")
                second = watch.rejected_bar is not None and i > watch.rejected_bar and model != "BREAKOUT"
                plan_id = f"{mode}|{model}|{side}|{i_time}"
                h1, h4 = context[i]
                stop_base = local_stop if fast or second or model in {"PULLBACK", "RETEST"} else wide_stop
                stop, risk, reasons, details = _geometry(side, i, bars, features, watch, atr,
                                                          stop_base, costs, policy, equity)
                # A fresh compact structure gets its own local extension test.
                extension_anchor = (retest[1] if model == "RETEST" else
                                    prev.ema21 if model == "PULLBACK" else continuation_level)
                extension = max(0., side * (close - (extension_anchor if second else f.ema21)) / atr)
                if h1 is None or h4 is None:
                    reasons.append("HTF_UNKNOWN")
                elif -side in (h1, h4):
                    reasons.append("HTF_CONFLICT")
                if extension > policy["max_extension_atr"]:
                    reasons.append("EXCESSIVE_EXTENSION")
                if f.spike:
                    reasons.append("VOLATILITY_SPIKE")
                deterioration = advancing < prior_momentum - .35 and f.displacement != side
                if deterioration and not (fast and f.bos == side):
                    reasons.append("MOMENTUM_DETERIORATION")
                details.update({
                    "extension_atr": extension, "momentum_atr": advancing,
                    "momentum_deteriorating": deterioration, "regime": f.regime,
                    "reversal_evidence": f.mss, "exhaustion_evidence": f.regime.startswith("EXHAUSTION"),
                    "h1": h1, "h4": h4, "plan_id": plan_id, "second_chance": second,
                    "first_structure_ms": bars[watch.structure_bar]["time_close"] if watch.structure_bar is not None else None,
                })
                emit("TREND_CONFIRMED", model, **details)
                if reasons:
                    emit("RISK_REJECTED", model, reasons[0], all_reasons=reasons, **details)
                    if any(r in {"EXCESSIVE_EXTENSION", "OVERSIZED_STOP", "HTF_CONFLICT"} for r in reasons):
                        watch.rejected_bar = i
                        emit("WAIT_FOR_PULLBACK", model, reasons[0], **details)
                    continue
                targets = tuple(round_away(close + side * r * risk, costs.tick, side) for r in (1, 2, 3))
                plans.append(Plan(plan_id, model, side, i, close, stop, targets, atr, 48, regime=f.regime))
                emit("ENTRY_AVAILABLE", model, **details)
                if second:
                    emit("SECOND_CHANCE", model, **details)
    return decisions, plans


def event_audit(bars, minutes, event_config, decisions, valid, fills, rejected):
    """Score independent directional-hour outcomes after execution."""
    events, _ = hourly_events(bars, minutes, event_config)
    rows = []
    by_hour = defaultdict(list)
    for d in decisions:
        by_hour[d["time_ms"] // HOUR].append(d)
    valid_by_hour, fills_by_hour, rejected_by_hour = (defaultdict(list) for _ in range(3))
    for plan in valid:
        valid_by_hour[bars[plan.bar]["time_close"] // HOUR].append(plan)
    for fill in fills:
        if fill.entry_bar is not None:
            fills_by_hour[bars[fill.entry_bar]["time"] // HOUR].append(fill)
    for plan, reason in rejected:
        rejected_by_hour[bars[plan.bar]["time_close"] // HOUR].append((plan, reason))
    outcome_by_id = {fill.plan.id: fill for fill in fills}
    same_event = {(e["start"] // HOUR, e["side"]) for e in events}
    off_event = defaultdict(lambda: {"confirmations": 0, "losses": 0})
    off_event_fills = 0
    for d in decisions:
        h = d["time_ms"] // HOUR
        if d["state"] == "TREND_CONFIRMED" and (h, d["side"]) not in same_event:
            off_event[d["time_ms"]]["confirmations"] += 1
    for fill in fills:
        if fill.entry_bar is None:
            continue
        t = bars[fill.entry_bar]["time"]
        if (t // HOUR, fill.plan.side) not in same_event:
            off_event_fills += 1
            if fill.state == "CLOSED" and fill.net < 0:
                off_event[bars[fill.release]["time"]]["losses"] += 1

    for event in events:
        t, side = event["start"], event["side"]
        hour = t // HOUR
        observations = [d for d in by_hour[hour] if d["side"] == side]
        same_plans = [p for p in valid_by_hour[hour] if p.side == side]
        same_fills = sorted((f for f in fills_by_hour[hour] if f.plan.side == side),
                            key=lambda f: (f.entry_bar, f.plan.id))
        first = same_fills[0] if same_fills else None
        confirmations = [d for d in observations if d["state"] == "TREND_CONFIRMED"]
        risk_rejections = [d for d in observations if d["state"] == "RISK_REJECTED"]
        blocked = [(p.id, reason) for p, reason in rejected_by_hour[hour] if p.side == side]
        unavailable = [(p.id, outcome_by_id[p.id].state) for p in same_plans if p.id in outcome_by_id
                       and outcome_by_id[p.id].entry_bar is None]
        filled_ids = {f.plan.id for f in same_fills}
        admitted = [p for p in same_plans if p.id in outcome_by_id and outcome_by_id[p.id].state != "RISK_INVALID"]
        watch_at = min((d["time_ms"] for d in observations if d["state"] == "WATCH"), default=None)
        confirm_at = min((d["time_ms"] for d in confirmations), default=None)
        executable_at = min((bars[p.bar]["time_close"] for p in same_plans), default=None)
        admitted_at = min((bars[p.bar]["time_close"] for p in admitted), default=None)
        fill_at = bars[first.entry_bar]["time"] if first else None
        capture = net_r = None
        if first:
            final_bar = min(event["last_bar"], first.release)
            # Boundary candles can contain extrema before entry or after a partial/final exit.
            field = "high" if side == 1 else "low"
            available = [first.fill] + [bars[i][field] for i in range(first.entry_bar + 1, final_bar)]
            available.extend(p.price for p in first.exits if p.bar <= event["last_bar"])
            event_move = max(side * (bars[i][field] - bars[event["first_bar"]]["open"])
                             for i in range(event["first_bar"], event["last_bar"]+1))
            if event_move > 0:
                capture = max(side * (price - first.fill) for price in available) / event_move
            initial_risk = abs(first.fill - first.plan.stop) * first.plan.units
            if first.state == "CLOSED" and initial_risk > 0:
                net_r = first.net / initial_risk
        rows.append({
            "id": f"{t}|{side}", "time_ms": t, "side": side,
            "detected": watch_at is not None or confirm_at is not None or executable_at is not None or first is not None,
            "actionable": executable_at is not None or first is not None, "filled": first is not None,
            "capture_fraction": capture, "net_r": net_r,
            "watch_at": watch_at, "confirmed_at": confirm_at, "executable_at": executable_at,
            "admitted_at": admitted_at, "fill_at": fill_at,
            "fill_delay_minutes": (fill_at - t)/60_000 if fill_at is not None else None,
            "confirmation_delay_minutes": (confirm_at - t)/60_000 if confirm_at is not None else None,
            "executable_delay_minutes": (executable_at - t)/60_000 if executable_at is not None else None,
            "valid_intents_without_same_hour_fill": sum(p.id not in filled_ids for p in same_plans),
            "blocked": blocked, "unfilled": unavailable,
            "risk_rejection_codes": [d["reason"] for d in risk_rejections],
            "second_chance_intents": sum(d["state"] == "SECOND_CHANCE" for d in observations),
        })
    return rows, [{"time_ms": t, **counts} for t, counts in sorted(off_event.items())], {
        "event_count": len(rows), "off_event_fills": off_event_fills,
        "missed_actionable_events": sum(r["actionable"] and not r["filled"] for r in rows),
        "eligible_events_without_detection": sum(not r["detected"] for r in rows),
    }
