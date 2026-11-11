"""Independent closed-bar reconstruction of Wick Sequence Breakout.

The public author script remains outside this repository. This module records its
formation decisions separately from the registered QIRVANTA OHLC fill model.
"""

import argparse
from collections import Counter, deque
import csv
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
from math import isfinite
from pathlib import Path
from statistics import mean, median

from gold_pro.archive import ROOT, _hash, _ms, _read, _read_json, cash_campaign, native_outcomes
from gold_pro.opportunity import event_audit
from gold_pro.replay import Costs, Plan, portfolio, simulate
from gold_wick.engine import scan
from gold_wick.research import cash_by_cost, registered_cost, scoring_windows, wick_decisions
from research.data.loader import load_bars, validate_bars
from unified.tools.audit_premium_s5 import verify_bars
from unified.tools.audit_tv_observations import inspect


SYMBOL = "OANDA:XAUUSD"
SOURCE_URL = "https://www.tradingview.com/script/D6AvF0jw-Wick-Sequence-Breakout/"
REFERENCE = ROOT / "docs/premium_evidence/s5_screen_registration.json"
REGISTRATION = ROOT / "gold_pro/registration.json"
WICK_REGISTRATION = ROOT / "gold_wick/research_protocol.json"
FORWARD_SEAL = ROOT / "docs/gold_pro_evidence/forward_seal_m5.json"
EVENT_REGISTRATION = ROOT / "docs/v21_evidence/event_evaluation_registration.json"
REFERENCE_LEVELS = ("4049.570", "4058.231", "4040.909", "4032.248", "4023.588")
CASE_CUTOFF = _ms("2026-11-11T16:00:00Z")


@dataclass(frozen=True)
class Settings:
    count: int = 3
    window: int = 5
    bear_upper: float = .45
    bear_lower: float = .25
    bull_lower: float = .45
    bull_upper: float = .25
    wait_bars: int = 100
    close_breakout: bool = True
    minimum_range: bool = False
    minimum_range_atr: float = .30
    filter_atr_length: int = 14
    breakout_size_filter: bool = False
    breakout_size_multiple: float = 1.5
    breakout_size_length: int = 20
    absorption: bool = True
    volume_length: int = 20
    absorption_volume_multiple: float = 1.10
    absorption_max_atr: float = .60
    absorption_close: float = .60
    concurrent: int = 2
    mixed: bool = True
    allow_buy: bool = True
    allow_sell: bool = True
    show_targets: bool = True
    use_atr_sl: bool = True
    stop_atr_length: int = 14
    stop_atr_multiple: float = 1.5
    stop_percent: float = 1.0
    target_r: tuple[float, float, float] = (1., 2., 3.)


@dataclass
class Zone:
    first_bar: int
    formed_bar: int
    top: float
    bottom: float
    formation_side: int
    buy_weight: float | None = 0.
    sell_weight: float | None = 0.

    def add(self, bar, tick):
        if bar["high"] <= self.bottom or bar["low"] >= self.top:
            return
        volume = bar.get("volume")
        if volume is None or self.buy_weight is None:
            self.buy_weight = self.sell_weight = None
            return
        width = max(bar["high"] - bar["low"], tick)
        self.buy_weight += volume * (bar["close"] - bar["low"]) / width
        self.sell_weight += volume * (bar["high"] - bar["close"]) / width


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def wilder_atr(bars, length):
    values, total, prior_close = [], 0., None
    for i, bar in enumerate(bars):
        width = bar["high"] - bar["low"]
        true_range = width if prior_close is None else max(
            width, abs(bar["high"] - prior_close), abs(bar["low"] - prior_close))
        if i < length:
            total += true_range
            value = total / length if i + 1 == length else None
        else:
            value = values[-1] + (true_range - values[-1]) / length
        values.append(value)
        prior_close = bar["close"]
    return values


def _pine_number(value):
    # Pine comparisons round floating-point operands to nine decimal places.
    return round(value, 9)


def candle_flags(bar, atr, avg_volume, settings):
    high, low, opening, close = (bar[k] for k in ("high", "low", "open", "close"))
    width = high - low
    upper = _pine_number((high - max(opening, close)) / width * 100.) if width else 0.
    lower = _pine_number((min(opening, close) - low) / width * 100.) if width else 0.
    close_pct = _pine_number((close - low) / width * 100.) if width else 50.
    bear_shape = upper >= settings.bear_upper * 100. and lower <= settings.bear_lower * 100.
    bull_shape = lower >= settings.bull_lower * 100. and upper <= settings.bull_upper * 100.
    range_ok = not settings.minimum_range or (
        atr is not None and _pine_number(width) >= _pine_number(atr * settings.minimum_range_atr))
    buy_wick, sell_wick = bull_shape and range_ok, bear_shape and range_ok
    volume = bar.get("volume")
    absorbed = (
        settings.absorption and volume is not None and avg_volume is not None and avg_volume > 0
        and _pine_number(volume) >= _pine_number(avg_volume * settings.absorption_volume_multiple)
        and atr is not None and 0 < _pine_number(width) <= _pine_number(atr * settings.absorption_max_atr)
        and not buy_wick and not sell_wick
    )
    buy_abs = absorbed and (bull_shape or (not bear_shape and close_pct >= settings.absorption_close * 100.))
    sell_abs = absorbed and (bear_shape or (not bull_shape and close_pct <= 100. - settings.absorption_close * 100.))
    return buy_wick, sell_wick, buy_abs, sell_abs


def reconstruct(bars, settings=Settings(), *, tick=.001, max_hold=72):
    """Re-evaluate the publication's default decision order on completed OHLCV."""
    if not bars or validate_bars(bars) not in (300_000, 900_000, 1_800_000):
        raise ValueError("WSB reconstruction requires standard M5, M15 or M30 OHLCV")
    for bar in bars:
        volume = bar.get("volume")
        if volume is not None and (not isfinite(volume) or volume < 0):
            raise ValueError(f"Invalid volume at {bar['time']}")
    atr = wilder_atr(bars, settings.filter_atr_length)
    stop_atr = wilder_atr(bars, settings.stop_atr_length)
    volumes = deque(maxlen=settings.volume_length)
    ranges = deque(maxlen=settings.breakout_size_length)
    window = deque(maxlen=settings.window)
    active, events, signals, plans, facts = [], [], [], [], []
    old_pattern = (False, False, False)

    for i, bar in enumerate(bars):
        width = bar["high"] - bar["low"]
        previous_range = mean(ranges) if len(ranges) == settings.breakout_size_length else None
        ranges.append(width)
        if bar.get("volume") is not None:
            volumes.append(bar["volume"])
        avg_volume = mean(volumes) if len(volumes) == settings.volume_length else None
        buy_wick, sell_wick, buy_abs, sell_abs = candle_flags(bar, atr[i], avg_volume, settings)
        window.append((buy_wick or buy_abs, sell_wick or sell_abs))
        buys = sum(buy for buy, _ in window)
        sells = sum(sell for _, sell in window)
        any_count = sum(buy or sell for buy, sell in window)
        bull = buys >= settings.count and sells < settings.count
        bear = sells >= settings.count and buys < settings.count
        mixed = settings.mixed and any_count >= settings.count and not (bull or bear)
        pattern = (bull, bear, mixed)
        rises = tuple(now and not before for now, before in zip(pattern, old_pattern))
        old_pattern = pattern
        recent = any(zone.formed_bar > i - settings.window for zone in active)
        known = bar["time_close"]
        if (rises[0] or rises[1] or (rises[2] and not recent)) and len(active) < settings.concurrent:
            first = i + 1 - settings.window
            candles = bars[max(0, first):i + 1]
            zone = Zone(first, i, max(row["high"] for row in candles), min(row["low"] for row in candles),
                        1 if rises[0] else -1 if rises[1] else 0)
            for row in reversed(candles):
                zone.add(row, tick)
            active.append(zone)
            events.append(_event("FORMED", zone, i, known, zone.formation_side,
                                 buy_count=buys, sell_count=sells, any_count=any_count))

        chosen = set()
        range_ok = not settings.breakout_size_filter or (
            previous_range is not None
            and _pine_number(width) >= _pine_number(settings.breakout_size_multiple * previous_range))
        for j in range(len(active) - 1, -1, -1):
            zone = active[j]
            cross_buy = (bar["close"] if settings.close_breakout else bar["high"]) > zone.top
            cross_sell = (bar["close"] if settings.close_breakout else bar["low"]) < zone.bottom
            if cross_buy or cross_sell:
                volume_buy = zone.buy_weight is not None and _pine_number(zone.buy_weight) >= _pine_number(zone.sell_weight)
                volume_sell = zone.buy_weight is not None and _pine_number(zone.sell_weight) > _pine_number(zone.buy_weight)
                side = 1 if cross_buy and volume_buy else -1 if cross_sell and volume_sell else 0
                enabled = settings.allow_buy if side == 1 else settings.allow_sell
                reason = ("VOLUME_MISMATCH" if not side else "BREAKOUT_SIZE" if not range_ok else
                          "SIDE_DISABLED" if not enabled else "SIGNAL")
                if reason == "SIGNAL":
                    chosen.add(side)
                events.append(_event(reason, zone, i, known, side,
                                     crossed_buy=cross_buy, crossed_sell=cross_sell, breakout_range=width,
                                     prior_average_range=previous_range))
                del active[j]
            elif settings.wait_bars and i - zone.formed_bar > settings.wait_bars:
                if i > zone.formed_bar:
                    zone.add(bar, tick)
                events.append(_event("EXPIRED", zone, i, known, zone.formation_side))
                del active[j]
            elif i > zone.formed_bar:
                zone.add(bar, tick)
        if chosen:
            signals.append({"bar": i, "known_ms": known, "directions": sorted(chosen)})
        facts.append({"atr": atr[i], "atr_tp": stop_atr[i], "avg_range_prev": previous_range,
                      "avg_volume": avg_volume, "bear_wick": sell_wick, "bull_wick": buy_wick,
                      "bear_abs": sell_abs, "bull_abs": buy_abs, "bear_count": sells,
                      "bull_count": buys, "any_count": any_count, "bear_signal": -1 in chosen,
                      "bull_signal": 1 in chosen})
        if settings.show_targets and len(chosen) == 1:
            side = next(iter(chosen))
            entry = bar["close"]
            risk = (None if stop_atr[i] is None else stop_atr[i] * settings.stop_atr_multiple
                    ) if settings.use_atr_sl else entry * settings.stop_percent / 100.
            if risk is not None and risk > 0 and atr[i] is not None:
                plans.append(Plan(f"WSB|{side}|{known}", "WSB", side, i, entry, entry - side * risk,
                                  tuple(entry + side * multiple * risk for multiple in settings.target_r),
                                  atr[i], max_hold))
    return {"events": events, "signals": signals, "plans": plans, "facts": facts}


def _event(state, zone, index, known, side, **extra):
    return {
        "state": state, "bar": index, "known_ms": known, "side": side,
        "formation_bar": zone.formed_bar, "window_left_bar": zone.first_bar,
        "formation_side": zone.formation_side, "top": zone.top, "bottom": zone.bottom,
        "buy_weight": zone.buy_weight, "sell_weight": zone.sell_weight, **extra,
    }


def displayed(plan):
    return tuple(_format(value) for value in (plan.reference, plan.stop, *plan.targets))


def read_native(path):
    """Parse the supplementary native logger without requiring the author's source file."""
    streams = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            tag, separator, payload = row["Message"].partition("|")
            if separator and tag.startswith("WSB_ORIGINAL_") and payload.startswith("{"):
                streams.setdefault(tag.removeprefix("WSB_ORIGINAL_"), []).append(json.loads(payload))
    contexts = streams.get("CTX", [])
    if not contexts:
        raise ValueError("Missing native context")
    first = contexts[0]
    frame = int(first["timeframe"])
    if frame not in (5, 15, 30) or any(
        x["symbol"] != SYMBOL or int(x["timeframe"]) != frame
        or x["index"] != first["index"] + i or x["first_open_ms"] != first["first_open_ms"]
        for i, x in enumerate(contexts)
    ):
        raise ValueError("Native context is discontinuous or mixes symbols or timeframes")
    bars = [{"time": x["open_ms"], "time_close": x["close_ms"],
             **{key: x[key] for key in ("open", "high", "low", "close", "volume")}} for x in contexts]
    if validate_bars(bars) != frame * 60_000:
        raise ValueError("Native timestamps do not match the declared timeframe")
    return streams, bars, frame


def _native_settings(rows):
    if len(rows) != 1:
        raise ValueError("Exactly one native WSB setting snapshot is required")
    row = rows[0]
    return Settings(
        count=row["min_count"], window=row["window"], bear_upper=row["bear_upper"] / 100.,
        bear_lower=row["bear_lower"] / 100., bull_lower=row["bull_lower"] / 100.,
        bull_upper=row["bull_upper"] / 100., wait_bars=row["wait_bars"], close_breakout=row["close_break"],
        minimum_range=row["range_filter"], minimum_range_atr=row["range_atr"],
        filter_atr_length=row["atr_len"], breakout_size_filter=row["size_filter"],
        breakout_size_multiple=row["size_mult"], breakout_size_length=row["size_len"],
        absorption=row["absorption"], volume_length=row["vol_len"], absorption_volume_multiple=row["vol_mult"],
        absorption_max_atr=row["abs_max_atr"], absorption_close=row["abs_close_pct"] / 100.,
        concurrent=row["concurrent"], mixed=row["mixed"], allow_buy=row["bull_signals"], allow_sell=row["bear_signals"],
        show_targets=row["show_targets"], use_atr_sl=row["sl_atr"], stop_atr_length=row["tp_atr_len"],
        stop_atr_multiple=row["sl_atr_mult"], stop_percent=row["sl_pct"],
        target_r=(row["rr1"], row["rr2"], row["rr3"]),
    )


def _equal(actual, expected):
    if actual is None or expected is None or isinstance(expected, (bool, str)):
        return actual == expected
    return abs(actual - expected) <= 1e-6


def _reconcile_events(streams, result, contexts, earliest):
    offset = contexts[0]["index"]
    formation_keys = ("top", "bottom", "buy_volume", "sell_volume", "bull_count", "bear_count", "any_count", "type")
    resolution_keys = ("top", "bottom", "buy_volume", "sell_volume", "mixed", "break_up", "break_down", "direction", "reason")
    expected_formations, expected_resolutions = {}, {}
    for e in result["events"]:
        if e["known_ms"] < earliest:
            continue
        common = {"top": e["top"], "bottom": e["bottom"],
                  "buy_volume": e["buy_weight"], "sell_volume": e["sell_weight"]}
        index = e["bar"] + offset
        formed = e["formation_bar"] + offset
        direction = "BUY" if e["side"] == 1 else "SELL" if e["side"] == -1 else ""
        if e["state"] == "FORMED":
            kind = "BULL" if e["formation_side"] == 1 else "BEAR" if e["formation_side"] == -1 else "MIXED"
            expected_formations[(index, index)] = {
                **common, "type": kind, "bull_count": e["buy_count"],
                "bear_count": e["sell_count"], "any_count": e["any_count"],
            }
        else:
            expected_resolutions[(index, formed)] = {
                **common, "mixed": e["formation_side"] == 0, "break_up": e.get("crossed_buy", False),
                "break_down": e.get("crossed_sell", False), "direction": direction, "reason": e["state"],
            }
    actual_formations = {(x["index"], x["index"]): x for x in streams.get("FORMATION", [])
                         if x["close_ms"] >= earliest}
    actual_resolutions = {(x["index"], x["formed_index"]): x for x in streams.get("RESOLUTION", [])
                          if x["close_ms"] >= earliest}
    mismatches = {}
    for tag, native, replay, fields in (
        ("formations", actual_formations, expected_formations, formation_keys),
        ("resolutions", actual_resolutions, expected_resolutions, resolution_keys),
    ):
        differences = []
        for key in sorted(native.keys() | replay.keys()):
            actual, expected = native.get(key), replay.get(key)
            if actual is None or expected is None:
                differences.append({"index": key, "native": actual is not None, "reconstructed": expected is not None})
            else:
                different = [field for field in fields if not _equal(actual[field], expected[field])]
                if different:
                    differences.append({"index": key, "fields": different,
                                        "native": {k: actual[k] for k in different},
                                        "reconstructed": {k: expected[k] for k in different}})
        mismatches[tag] = {"native": len(native), "reconstructed": len(replay), "mismatches": differences}
    return mismatches


def native_comparison(path, *, costs=Costs(), case_cutoff=CASE_CUTOFF):
    streams, bars, frame = read_native(path)
    _preseal(bars)
    settings = _native_settings(streams.get("SETTINGS", []))
    reconstructed = reconstruct(bars, settings, tick=costs.tick)
    contexts = streams["CTX"]
    first = contexts[0]
    seed_complete = first["index"] == 0 and first["first_open_ms"] == first["open_ms"]
    earliest = bars[0]["time_close"] if seed_complete else bars[min(250, len(bars) - 1)]["time_close"]
    all_native = streams.get("PLAN", [])
    source = {(r["close_ms"], _side(r["direction"])): r for r in all_native if r["sl"] is not None}
    replay = {(bars[p.bar]["time_close"], p.side): p for p in reconstructed["plans"]}
    mismatches = []
    for key in sorted(source.keys() | replay.keys()):
        if key[0] < earliest:
            continue
        row, plan = source.get(key), replay.get(key)
        if row is None or plan is None:
            mismatches.append({"known_utc": utc(key[0]), "side": key[1], "native": row is not None,
                               "reconstructed": plan is not None})
            continue
        expected = (plan.reference, plan.stop, *plan.targets)
        actual = tuple(row[k] for k in ("entry", "sl", "tp1", "tp2", "tp3"))
        if any(abs(a - b) > 1e-6 for a, b in zip(actual, expected)):
            mismatches.append({"known_utc": utc(key[0]), "side": key[1], "native_prices": actual,
                               "reconstructed_prices": expected})
    matches = [p for p in reconstructed["plans"] if p.side == -1 and displayed(p) == REFERENCE_LEVELS]
    exact_native = [r for r in source.values() if _side(r["direction"]) == -1 and
                    tuple(_format(r[k]) for k in ("entry", "sl", "tp1", "tp2", "tp3")) == REFERENCE_LEVELS]
    price_hits = [{**{k: x[k] for k in ("open_ms", "close_ms", "atr_tp", "bear_signal", "bull_signal")},
                   "open_utc": utc(x["open_ms"]), "close_utc": utc(x["close_ms"])}
                  for x in contexts if _format(x["close"]) == REFERENCE_LEVELS[0]]
    facts_diff = []
    for native, replay_facts in zip(contexts, reconstructed["facts"]):
        if native["close_ms"] >= earliest:
            different = [k for k, v in replay_facts.items() if not _equal(native[k], v)]
            if different:
                facts_diff.append({"known_utc": utc(native["close_ms"]), "fields": different,
                                   "native": {k: native[k] for k in different},
                                   "reconstructed": {k: replay_facts[k] for k in different}})
    return {
        "capture_sha256": _hash(Path(path)), "reconstruction_seed_complete": seed_complete,
        "frame_minutes": frame, "native_settings_match_public_defaults": settings == Settings(),
        "native_settings": streams["SETTINGS"][0],
        "bars": len(bars), "first_open_utc": utc(bars[0]["time"]), "last_close_utc": utc(bars[-1]["time_close"]),
        "comparison_first_utc": utc(earliest), "source_plan_count": len(source),
        "native_warmup_plans_without_levels": len(all_native) - len(source), "replay_plan_count": len(replay),
        "plan_mismatches": mismatches, "close_only_matches": price_hits, "context_mismatches": facts_diff,
        "events": _reconcile_events(streams, reconstructed, contexts, earliest),
        "exact_native_reference_matches": exact_native,
        "exact_python_reference_matches": [
            _case(p, bars, reconstructed, costs, frame, case_cutoff) for p in matches
        ],
        "case_setting_sensitivities": _case_setting_sensitivities(matches, bars, settings, costs),
        "alert_scope": "Historical alert condition only. Delivery and actual fills are unobserved.",
    }


def _format(value):
    return str(Decimal(str(value)).quantize(Decimal(".001"), rounding=ROUND_HALF_UP))


def _side(value):
    if value in (-1, "-1", "SELL", "BEAR"):
        return -1
    if value in (1, "1", "BUY", "BULL"):
        return 1
    raise ValueError(f"Unexpected native signal direction: {value!r}")


def _case_setting_sensitivities(matches, bars, settings, costs):
    if not matches:
        return {}
    comparisons = {}
    for key, alternative in (
        ("mixed_boxes_disabled", replace(settings, mixed=False)),
        ("absorption_disabled", replace(settings, absorption=False)),
        ("default_threshold_breakout_size_filter_enabled", replace(settings, breakout_size_filter=True)),
    ):
        model = reconstruct(bars, alternative, tick=costs.tick)
        comparisons[key] = [
            {"known_utc": utc(bars[case.bar]["time_close"]),
             "same_direction_signal": any(case.bar == e["bar"] and case.side in e["directions"]
                                          for e in model["signals"]),
             "same_displayed_plan": any(case.bar == plan.bar and case.side == plan.side
                                        and displayed(plan) == REFERENCE_LEVELS for plan in model["plans"]),
             "resolution_events": [e for e in model["events"] if e["bar"] == case.bar]}
            for case in matches
        ]
    return comparisons


def _case_formations(plan, bars, result):
    formed = {e["bar"]: e for e in result["events"] if e["state"] == "FORMED"}
    rows = []
    for e in result["events"]:
        if e["bar"] != plan.bar or e["state"] != "SIGNAL" or e["side"] != plan.side:
            continue
        i = e["formation_bar"]
        window_left = max(0, e["window_left_bar"])
        window = [
            {"open_utc": utc(bars[j]["time"]), "close_utc": utc(bars[j]["time_close"]),
             "ohlcv": {key: bars[j][key] for key in ("open", "high", "low", "close", "volume")},
             **{key: result["facts"][j][key] for key in ("bear_wick", "bull_wick", "bear_abs", "bull_abs")}}
            for j in range(window_left, i + 1)
        ]
        rows.append({
            **e, "formed_utc": utc(bars[i]["time_close"]), "window_left_utc": utc(bars[window_left]["time"]),
            "creation_event": formed[i], "formation_window": window,
            "breakout_clearance": plan.reference - e["top"] if plan.side == 1 else e["bottom"] - plan.reference,
            "dominant_volume_share": max(e["buy_weight"], e["sell_weight"]) / (e["buy_weight"] + e["sell_weight"])
            if e["buy_weight"] + e["sell_weight"] > 0 else None,
        })
    return rows


def _case_occupancy(plan, bars, result, costs, frame):
    plans = [p for p in result["plans"] if p.bar < len(bars)]
    outcomes, rejections = portfolio(plans, bars, costs, frame, 100_000)
    rejection = next((reason for p, reason in rejections if p.id == plan.id), None)
    admitted = next((outcome for outcome in outcomes if outcome.plan.id == plan.id), None)
    held = []
    for outcome in outcomes:
        if (outcome.plan.bar >= plan.bar or outcome.entry_bar is None
                or not outcome.entry_bar <= plan.bar < outcome.release):
            continue
        settled = sum(p.units for p in outcome.exits if p.bar <= plan.bar)
        held.append({
            "id": outcome.plan.id, "known_utc": utc(bars[outcome.plan.bar]["time_close"]),
            "side": outcome.plan.side, "entry_reference": outcome.plan.reference,
            "modeled_entry_utc": utc(bars[outcome.entry_bar]["time"]), "modeled_entry": outcome.fill,
            "remaining_units_at_case_confirmation": outcome.plan.units - settled,
            "modeled_exit_bar_close_utc": utc(bars[outcome.release]["time_close"])
            if outcome.state == "CLOSED" else None,
            "modeled_net_usd": outcome.net if outcome.state == "CLOSED" else None,
        })
    return {
        "scope": "Standalone WSB next-open shadow portfolio on the captured prefix, one slot and 100,000 USD equity.",
        "admitted": admitted is not None, "rejection": rejection, "held_at_case_confirmation": held,
    }


def _case(plan, bars, result, costs, frame, cutoff):
    source = bars[plan.bar]
    future = [row for row in bars[plan.bar + 1:] if row["time_close"] <= cutoff]
    resolution = _case_formations(plan, bars, result)
    if not future:
        return {"open_utc": utc(source["time"]), "known_utc": utc(source["time_close"]),
                "plan": asdict(plan), "formation_resolutions": resolution, "excursion": None}
    adverse = max(future, key=lambda row: row["high"]) if plan.side == -1 else min(future, key=lambda row: row["low"])
    favorable = min(future, key=lambda row: row["low"]) if plan.side == -1 else max(future, key=lambda row: row["high"])
    through_cutoff = bars[:plan.bar + len(future) + 1]
    outcome = simulate(plan, through_cutoff, costs, frame)
    modeled_fill = outcome.fill

    def first_touch(price, stop):
        for row in future:
            edge = ("high" if plan.side == -1 else "low") if stop else ("low" if plan.side == -1 else "high")
            if plan.side * (row[edge] - price) * (-1 if stop else 1) >= 0:
                return {
                    "bar_open_utc": utc(row["time"]), "bar_close_utc": utc(row["time_close"]),
                    "minutes_since_confirmation_at_bar_open": (row["time"] - source["time_close"]) / 60_000,
                    "minutes_since_confirmation_at_bar_close": (row["time_close"] - source["time_close"]) / 60_000,
                }
        return None

    high_low = ("high", "low") if plan.side == -1 else ("low", "high")
    excursion = {"cutoff_utc": utc(future[-1]["time_close"]), "observed_bars": len(future),
                 "mae_from_reference": max(0., plan.side * (plan.reference - adverse[high_low[0]])),
                 "mfe_from_reference": max(0., plan.side * (favorable[high_low[1]] - plan.reference)),
                 "mae_bar_open_utc": utc(adverse["time"]), "mfe_bar_open_utc": utc(favorable["time"]),
                 "stop_first_touch": first_touch(plan.stop, True),
                 "target_first_touches": [first_touch(price, False) for price in plan.targets]}
    if modeled_fill is not None:
        excursion["mae_from_modeled_fill"] = max(0., plan.side * (modeled_fill - adverse[high_low[0]]))
        excursion["mfe_from_modeled_fill"] = max(0., plan.side * (favorable[high_low[1]] - modeled_fill))
    risk = abs(modeled_fill - plan.stop) * plan.units if modeled_fill is not None else None
    runner = None
    if len(outcome.exits) == 3 and outcome.exits[-1].reason.startswith("TP3"):
        tp3_exit = outcome.exits[-1]
        after_tp3 = bars[tp3_exit.bar + 1:plan.bar + len(future) + 1]
        if after_tp3:
            edge = "low" if plan.side == -1 else "high"
            additional_move = max(plan.side * (row[edge] - tp3_exit.price) for row in after_tp3)
            runner = {
                "scope": "Price room after TP3. Original three-part model has no remaining position.",
                "completed_bars_after_tp3": len(after_tp3),
                "additional_favorable_quote_move": max(0., additional_move),
                "new_runner_executable_exit": None,
            }
    return {
        "open_utc": utc(source["time"]), "known_utc": utc(source["time_close"]), "signal_ohlcv": source,
        "plan": asdict(plan), "displayed": displayed(plan), "formation_resolutions": resolution,
        "next_bar_open_utc": utc(future[0]["time"]), "next_bar_open": future[0]["open"],
        "next_open_delay_ms": future[0]["time"] - source["time_close"],
        "modeled_fill": modeled_fill, "model_outcome": outcome.state,
        "model_scope": "Isolated plan with no prior position. Actual broker liquidity and fills are unavailable.",
        "modeled_exit_parts": [{**asdict(p), "bar_open_utc": utc(bars[p.bar]["time"]),
                               "bar_close_utc": utc(bars[p.bar]["time_close"])} for p in outcome.exits],
        "model_net_usd": outcome.net if outcome.state == "CLOSED" else None,
        "model_net_r": outcome.net / risk if outcome.state == "CLOSED" and risk else None,
        "excursion": excursion, "runner": runner,
        "standalone_one_slot_portfolio": _case_occupancy(plan, through_cutoff, result, costs, frame),
    }


def _preseal(bars):
    if not bars or bars[-1]["time_close"] >= _read_json(FORWARD_SEAL)["forward_start_ms"]:
        raise ValueError("The sealed forward period is outside this investigation")


def _score(outcomes, bars, costs, windows):
    closed = [cash_campaign(outcome, bars, costs) for outcome in outcomes if outcome.state == "CLOSED"]
    return {
        name: {side: cash_by_cost(
            [x for x in closed if start <= x.entry_ms and x.exit_ms < end and (not sign or x.side == sign)],
            {"base": 0, "stress": 1},
        ) for side, sign in (("all", 0), ("BUY", 1), ("SELL", -1))}
        for name, (start, end) in windows.items()
    }


def _audit_lane(name, bars, costs, windows, rules, plans, outcomes, rejections, decisions):
    rows, _, _ = event_audit(bars, 5, rules, decisions, plans, outcomes, rejections)
    start, end = windows["full"]
    rows = [r for r in rows if start <= r["time_ms"] and r["time_ms"] + 3_600_000 <= end]
    eligible = {(r["time_ms"] // 3_600_000, r["side"]) for r in rows}
    filled = [x for x in outcomes if x.entry_bar is not None and
              start <= bars[x.entry_bar]["time"] < end]
    off_event = [x for x in filled if (bars[x.entry_bar]["time"] // 3_600_000, x.plan.side) not in eligible]
    return {
        "method": name, "metrics_by_window_and_side": _score(outcomes, bars, costs, windows),
        "off_event_fills": len(off_event),
        "off_event_closed_losses": sum(x.state == "CLOSED" and x.net < 0 for x in off_event),
        "directional_hours": {
            side: _hour_summary([row for row in rows if not sign or row["side"] == sign])
            for side, sign in (("all", 0), ("BUY", 1), ("SELL", -1))
        },
        "offer_rejections": dict(Counter(reason for plan, reason in rejections
                                         if start <= bars[plan.bar]["time_close"] < end)),
    }


def _hour_summary(rows):
    result = {"eligible": len(rows), "missed_with_no_fill": sum(not row["filled"] for row in rows)}
    result.update({field: sum(row[field] for row in rows) for field in ("detected", "actionable", "filled")})
    for field in ("watch", "confirmed", "executable", "fill"):
        key = f"{field}_at"
        delays = [(row[key] - row["time_ms"]) / 60_000 for row in rows if row[key] is not None]
        result[f"median_{field}_delay_minutes"] = median(delays) if delays else None
    return result


def _false_breakouts(result, bars, outcomes, start, end, *, mixed_only=False):
    observed = {}
    for e in result["events"]:
        i, side = e["bar"], e["side"]
        if (e["state"] != "SIGNAL" or not start <= e["known_ms"] < end
                or i + 3 >= len(bars) or (mixed_only and e["formation_side"] != 0)):
            continue
        future = bars[i + 1:i + 4]
        if any(next_bar["time"] != previous["time_close"]
               for previous, next_bar in zip([bars[i], *future], future)) or future[-1]["time_close"] >= end:
            continue
        edge = e["top"] if side == 1 else e["bottom"]
        reclaimed = next((bar for bar in future if side * (bar["close"] - edge) <= 0), None)
        observed.setdefault((i, side), []).append((e, reclaimed))
    trade_by_key = {(x.plan.bar, x.plan.side): x for x in outcomes}
    counters = {}
    for side, label in ((1, "BUY"), (-1, "SELL")):
        candidates = [(key, observations) for key, observations in observed.items() if key[1] == side]
        failed = [(key, observations) for key, observations in candidates if all(r for _, r in observations)]
        examples = []
        for key, observations in failed[:3]:
            e, reclaim = observations[0]
            trade = trade_by_key.get(key)
            examples.append({
                "known_utc": utc(e["known_ms"]), "reclaim_utc": utc(reclaim["time_close"]),
                "entry_reference": bars[key[0]]["close"], "reclaim_close": reclaim["close"],
                "box_top": e["top"], "box_bottom": e["bottom"],
                "model_outcome": trade.state if trade else "NOT_ADMITTED",
                "net_usd": trade.net if trade and trade.state == "CLOSED" else None,
            })
        counters[label] = {"complete_three_bar_signals": len(candidates), "all_supporting_edges_reclaimed": len(failed),
                           "first_examples": examples}
    return counters


def historical_comparison(bars_path, s5_path):
    gold = _read_json(REGISTRATION)
    wick = _read_json(WICK_REGISTRATION)
    if _hash(FORWARD_SEAL) != wick["pins"]["forward_seal"]:
        raise ValueError("Forward seal changed")
    pin = _read_json(_read(REFERENCE, gold["s5_pin_sha256"]))["frames"]["5"]
    bars = load_bars(_read(Path(bars_path), pin["bars_sha256"]))
    _preseal(bars)
    s5 = inspect(_read(Path(s5_path), pin["log_sha256"]))
    if s5["mode"] != "S5":
        raise ValueError("The archived control must be S5")
    verify_bars(bars, s5["run"])
    costs, windows = registered_cost(gold), scoring_windows(wick)
    rules = _read_json(EVENT_REGISTRATION)["event"]
    native = native_outcomes(s5, costs)
    original_plans = [outcome.plan for outcome in native]
    wick_result = scan(bars, costs, minutes=5)
    wick_plans = [plan for plan in wick_result.plans if plan.model == "WICK_BREAKOUT"]
    arms = {
        "S5_ARCHIVED_NATIVE": _audit_lane("Recorded S5, only originally admitted offers", bars, costs, windows,
                                          rules, original_plans, native, [], []),
    }
    for name, plans, decisions in (
        ("S5_ADMITTED_OHLC", original_plans, []),
        ("QIRVANTA_WICK_A_OHLC", wick_plans, wick_decisions(wick_result.events)),
    ):
        outcomes, rejected = portfolio(plans, bars, costs, 5, 100_000)
        arms[name] = _audit_lane("Fixed stop-first OHLC replay", bars, costs, windows,
                                rules, plans, outcomes, rejected, decisions)
    for name, settings, mixed_only in (
        ("WSB_DEFAULT", Settings(), False),
        ("WSB_WITHOUT_ABSORPTION", replace(Settings(), absorption=False), False),
        ("WSB_HIGH_LOW_SENSITIVITY", replace(Settings(), close_breakout=False), False),
        ("WSB_MIXED_ONLY_POST_CASE", Settings(), True),
    ):
        result = reconstruct(bars, settings, tick=costs.tick)
        allowed = {(e["bar"], e["side"]) for e in result["events"]
                   if e["state"] == "SIGNAL" and (not mixed_only or e["formation_side"] == 0)}
        plans = [p for p in result["plans"] if p.bar >= 250 and (p.bar, p.side) in allowed]
        decisions = [
            {"time_ms": e["known_ms"], "state": "WATCH", "side": e["formation_side"]}
            for e in result["events"] if not mixed_only and e["state"] == "FORMED"
            and e["formation_side"] and e["bar"] >= 250
        ] + [
            {"time_ms": e["known_ms"], "state": "TREND_CONFIRMED", "side": side}
            for e in result["signals"] if e["bar"] >= 250 for side in e["directions"]
            if (e["bar"], side) in allowed
        ]
        outcomes, rejected = portfolio(plans, bars, costs, 5, 100_000)
        arms[name] = {
            **_audit_lane("Closed-bar WSB reconstruction with fixed stop-first OHLC execution", bars,
                          costs, windows, rules, plans, outcomes, rejected, decisions),
            "settings": asdict(settings),
            "scope": ("Post-case observation hypothesis. A mixed WATCH stays neutral until the close and volume "
                      "confirm direction. Only mixed-supported signals are eligible in this standalone shadow replay."
                      if mixed_only else "Publication-default reconstruction or declared setting sensitivity."),
            "false_breakouts": _false_breakouts(result, bars, outcomes, *windows["full"], mixed_only=mixed_only),
        }
        if mixed_only:
            start, end = windows["full"]
            arms[name]["neutral_mixed_watches"] = sum(
                e["state"] == "FORMED" and e["formation_side"] == 0 and start <= e["known_ms"] < end
                for e in result["events"])
    return {
        "feed": SYMBOL, "frame_minutes": 5, "bars": len(bars), "bars_sha256": pin["bars_sha256"],
        "native_s5_sha256": pin["log_sha256"], "windows_utc": {
            name: {"start": utc(a), "end_exclusive": utc(b)} for name, (a, b) in windows.items()
        },
        "modeled_execution": {
            "units": 3, "max_hold_bars_for_wsb": 72, "equity_usd": 100_000,
            "method": "Next chart open plus adverse slippage, fixed three-part brackets, stop first on ambiguous bars.",
            "invalidation": "Registered OHLC deviation/gap handling and risk/position occupancy apply equally to replay arms.",
            "costs": asdict(costs),
        },
        "independent_test": False, "holdout_used": False, "arms": arms,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native", action="append", type=Path, help="WSB original native Pine Logs CSV, repeat per frame")
    parser.add_argument("--history-bars", type=Path)
    parser.add_argument("--history-s5", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if bool(args.history_bars) != bool(args.history_s5) or not (args.native or args.history_bars):
        parser.error("Supply --native, both historical files, or all three")
    report = {"schema": "qirvanta.wsb.forensic.v1", "public_source": SOURCE_URL,
              "reference_levels": REFERENCE_LEVELS, "settings": asdict(Settings())}
    if args.native:
        native = [native_comparison(path) for path in args.native]
        if len({x["frame_minutes"] for x in native}) != len(native):
            parser.error("Supply one native capture per timeframe")
        report["native"] = {f"M{x['frame_minutes']}": x for x in native}
    if args.history_bars:
        report["archival_research"] = historical_comparison(args.history_bars, args.history_s5)
    args.output.write_bytes((json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode())


if __name__ == "__main__":
    main()
