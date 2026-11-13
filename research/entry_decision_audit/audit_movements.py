"""Join frozen movement labels to separately identified native and research evidence."""

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import gzip
import hashlib
import io
import json
from pathlib import Path
import subprocess
from zipfile import ZipFile


BASE = "37b903f46a2d878d9d0757da134620c22f2d067d"
READINESS = "acec05c12dd29827dac43969276e4ee0dd474796"
ENTRY_QUALITY = "587e46228040788fe893dd242769511257557993"
INPUTS = {
    "visual_v2_source": (
        BASE,
        "research/luminous_visual_v2/QIRVANTA_GOLD_PRO_VISUAL_V2.pine",
        "77feedcbef84e77b9270ec43840be5b823d838300d0a09d5ddb4fec7acc089f4",
    ),
    "visual_v2_native": (
        BASE, "research/luminous_visual_v2/evidence/native-pairs.zip",
        "fec83cd615923b210b6815852a69b74ed457f791ac41b3913a7c189f7d58f952",
    ),
    "archived_controls": (
        READINESS, "research/coverage_data_readiness/evidence/archived-s5-controls.json.gz",
        "1bc105475ed08e85aaded26face3b5bcf1737c59f5981ddbe6a947c2d0607f2a",
    ),
    "extension_episodes": (
        READINESS, "research/coverage_data_readiness/evidence/source-audit.json",
        "6badf6bc28d0a49442c2825ca6c42965780fd36df24029182cd23efb3261cd79",
    ),
    "older_entry_quality": (
        ENTRY_QUALITY, "research/market_coverage_entry_quality/evidence/evidence.json.gz",
        "b1f8f4e7fc2e62b9d99b281e114564a134ef3724575bb35e6291d688ef4d1cb4",
    ),
}
MINUTE = 60_000
FRAMES = {"M5": 5, "M15": 15, "M30": 30}
MATCHED_TAGS = (
    "DTV_STATE", "DTV_PAPER", "DTV_LEDGER", "RC1_CORE", "RC1_READY", "RC1_CTX",
    "RC2_EARLY", "LZ_EVENT", "DT_END",
)
CASES = (
    ("NOV11_BUY_03", "2026-11-11T02:45:00Z", "2026-11-11T04:15:00Z", 1),
    ("NOV11_SELL_DAY", "2026-11-11T11:30:00Z", "2026-11-11T16:30:00Z", -1),
)


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ms(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()) * 1000


def git_input(repo, key):
    commit, path, checksum = INPUTS[key]
    try:
        content = subprocess.check_output(["git", "show", f"{commit}:{path}"], cwd=repo)
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"Missing pinned input {commit}:{path}; fetch its history") from exc
    if hashlib.sha256(content).hexdigest() != checksum:
        raise ValueError(f"Input checksum changed for {commit}:{path}")
    return content


def json_input(repo, key):
    content = git_input(repo, key)
    return json.loads(gzip.decompress(content) if content.startswith(b"\x1f\x8b") else content)


def read_tape(archive, name):
    tags = {}
    with archive.open(name) as data:
        for row in csv.DictReader(io.TextIOWrapper(data, encoding="utf-8-sig", newline="")):
            tag, sep, payload = row["Message"].partition("|")
            if sep and tag in (*MATCHED_TAGS, "DT_SHADOW", "DT_MARKET"):
                tags.setdefault(tag, []).append(payload)
    return tags


def native_tape(archive, frame):
    baseline = read_tape(archive, f"baseline-{frame}.csv")
    visual = read_tape(archive, f"candidate-{frame}.csv")
    for tag in MATCHED_TAGS:
        if baseline.get(tag, []) != visual.get(tag, []):
            raise ValueError(f"Native baseline versus Visual V2 mismatch: {frame} {tag}")
    result = {tag: [json.loads(raw) for raw in visual.get(tag, [])] for tag in (
        "DTV_STATE", "DTV_PAPER", "DTV_LEDGER", "RC1_CORE", "RC1_READY", "DT_END",
    )}
    end = result["DT_END"][0]["time_close"]
    for tag, clock in (("DT_SHADOW", "time_ms"), ("DT_MARKET", "time_close")):
        pair = [
            [row for raw in tape[tag] if (row := json.loads(raw))[clock] <= end]
            for tape in (baseline, visual)
        ]
        if pair[0] != pair[1]:
            raise ValueError(f"Native baseline versus Visual V2 mismatch before DT_END: {frame} {tag}")
        result[tag] = pair[1]
    result["RC2_EARLY"] = []
    for raw in visual.get("RC2_EARLY", []):
        _, symbol, tf, close, direction, price = raw.split("|")
        if symbol != "OANDA:XAUUSD" or int(tf) != FRAMES[frame]:
            raise ValueError(f"Unexpected native source {symbol}/{tf}")
        result["RC2_EARLY"].append({
            "known_ms": int(close), "side": 1 if direction == "BUY" else -1, "price": float(price),
        })
    result["LZ_EVENT"] = []
    for raw in visual.get("LZ_EVENT", []):
        symbol, tf, direction, formed, origin, state, known, index, proximal, distal, level, level_known, _ = raw.split("|")
        result["LZ_EVENT"].append({
            "id": "|".join((symbol, tf, direction, formed, origin)), "state": state,
            "side": 1 if direction == "BUY" else -1, "known_ms": int(known), "bar_index": int(index),
            "proximal": float(proximal), "distal": float(distal), "break_level": float(level),
            "level_known_ms": int(level_known),
        })
    result["RC1_CTX"] = {}
    for raw in visual.get("RC1_CTX", []):
        _, symbol, tf, first, index, bar_open, known, op, hi, lo, cl, h1, h4 = raw.split("|")
        result["RC1_CTX"][int(known)] = {
            "symbol": symbol, "frame": tf, "first_chart_open_ms": int(first), "bar_index": int(index),
            "open_ms": int(bar_open), "known_ms": int(known), "ohlc": [float(x) for x in (op, hi, lo, cl)],
            "h1_closed_ms": int(h1) if h1.isdigit() else None,
            "h4_closed_ms": int(h4) if h4.isdigit() else None,
        }
    result["STATE_BY_CLOSE"] = {row["known_ms"]: row for row in result["DTV_STATE"]}
    result["LEDGER_BY_CLOSE"] = {row["known_ms"]: row for row in result["DTV_LEDGER"]}
    if set(result["STATE_BY_CLOSE"]) != set(result["RC1_CTX"]) or set(result["STATE_BY_CLOSE"]) != set(result["LEDGER_BY_CLOSE"]):
        raise ValueError(f"Native clock context does not match decision clocks: {frame}")
    return result


def first_at(rows, key):
    return min(rows, key=lambda row: row[key]) if rows else None


def identity(row):
    return row["start_ms"], row["side"]


def native_observations(tape, start, end, side, extension):
    # Associate the same 15-minute causal lead window used by the extension catalog.
    # Restrict historical bars to the loaded prefix actually present in this tape.
    lead = start - 15 * MINUTE
    shadow = [row for row in tape["DT_SHADOW"] if lead <= row["time_ms"] < end]
    early = [row for row in tape["RC2_EARLY"] if lead <= row["known_ms"] < end]
    zones = [row for row in tape["LZ_EVENT"] if lead <= row["known_ms"] < end]
    directional = [
        {"known_ms": row["time_ms"], "source": row["model"], "stage": row["state"]}
        for row in shadow if row["side"] == side and row["state"] in (
            "WATCH", "RETEST WATCH", "BREAKOUT CONFIRMED",
        )
    ]
    directional.extend(
        {"known_ms": row["known_ms"], "source": "RC2 EARLY", "stage": "WATCH"}
        for row in early if row["side"] == side
    )
    market = [row for row in tape["DT_MARKET"] if start <= row["time"] < end]
    newest_observation = first_at(directional, "known_ms")
    same_candidates = [row for row in shadow if row["side"] == side and row["state"] == "ENTRY CANDIDATE"]
    first_watch_time = newest_observation["known_ms"] if newest_observation else None
    market_slot_by_close = {row["time_close"]: row for row in tape["DT_MARKET"] if lead <= row["time_close"] < end}
    shadow = [annotate_shadow(row, market_slot_by_close.get(row["time_ms"])) for row in shadow]
    first_shadow_confirmation = first_at([
        row for row in shadow if row["side"] == side and row["state"] == "BREAKOUT CONFIRMED"
    ], "time_ms")
    return {
        "scope": "NATIVE_MATCHED_PROVIDER_PREFIX" if market else "NATIVE_MARKET_OUTSIDE_TAPE",
        "association_window_ms": [lead, end],
        "registered_extension_observation": extension.get("first_logged_directional_observation"),
        "first_available_same_side_directional_watch_or_confirmation": newest_observation,
        "first_same_side_shadow_confirmation": first_shadow_confirmation,
        "same_side_shadow_candidate_count": len(same_candidates),
        "later_same_side_shadow_diagnostic_candidate_ms": sorted({row["time_ms"] for row in same_candidates
                                                                 if first_watch_time is not None and row["time_ms"] > first_watch_time}),
        "registered_same_side_independent_continuations": extension.get("same_side_new_continuations"),
        "shadow": shadow,
        "rc2_early": early,
        "luminous_zones": zones,
        "market_bars": market,
        "chart_market_bar_count": len(market),
        "shadow_geometry_is_research_only": True,
        "unlogged_signal_or_rejection_is_unknown_outside_the_recorded_source_scope": True,
    }


def annotate_shadow(row, bar):
    """Report pinned source measurements only for a branch observed in DT_SHADOW."""
    reasons = set(row.get("reasons", ()))
    checks = {}
    atr = row.get("prior_atr")
    if "INSUFFICIENT_DISPLACEMENT" in reasons and bar and atr:
        side = row["side"]
        checks = {
            "body_minimum": {"measured": side * (bar["close"] - bar["open"]), "threshold": 0.50 * atr, "passes_if": ">="},
            "favorable_wick_maximum": {
                "measured": bar["high"] - bar["close"] if side == 1 else bar["close"] - bar["low"],
                "threshold": 0.35 * (bar["high"] - bar["low"]), "passes_if": "<=",
            },
        }
    if row.get("risk") is not None and atr:
        checks.update({
            "stop_minimum": {"measured": row["risk"], "threshold": max(0.001, 0.25 * atr), "passes_if": ">="},
            "stop_maximum": {"measured": row["risk"], "threshold": 2.50 * atr, "passes_if": "<="},
            "maximum_extension_atr": {"measured": row["extension_atr"], "threshold": 1, "passes_if": "<="},
            "maximum_cost_r": {"measured": row["friction_r"], "threshold": 0.30, "passes_if": "<="},
            "minimum_net_room_r": {"measured": row["net_room_r"], "threshold": 0.70, "passes_if": ">="},
            "maximum_cash_risk": {"measured": row["risk_cash"], "threshold": 250, "passes_if": "<="},
        })
    if checks:
        return {**row, "known_at_checks_from_pinned_shadow_source": checks}
    return row


def open_risk_checks(paper):
    if paper["event"] != "CANCELLED" or paper["reason"] != "Риск/разходи при отваряне":
        return None
    open_price, stop, atr = paper["price"], paper["initial_sl"], paper["atr_at_signal"]
    if open_price is None or stop is None or not atr:
        return None
    risk = paper["side"] * (open_price - stop)
    return {
        "event_id": paper["event_id"], "side": paper["side"],
        "effective_ms": paper["effective_time"], "known_ms": paper["known_time"],
        "modeled_open": open_price, "reference": paper["reference"], "initial_stop": stop,
        "stop_risk": risk, "atr_at_signal": atr, "stop_risk_atr": risk / atr,
        "minimum_stop_risk_atr": 0.8, "maximum_stop_risk_atr": paper["max_risk_atr"],
        "recorded_round_trip_cost_price": paper["roundtrip_cost_price"],
        "cost_over_risk": paper["roundtrip_cost_price"] / risk if risk > 0 else None,
        "maximum_cost_over_risk": 0.18,
        "absolute_deviation": abs(open_price - paper["reference"]),
        "maximum_deviation": 0.20 * atr,
        "provenance": "Pinned Visual V2 f_openRejection, native RAW CANCELLED values.",
    }


def native_decision(tape, frame, start, end, side):
    interval = FRAMES[frame] * MINUTE
    expected = list(range((start + interval - 1) // interval * interval, end, interval))
    states = [tape["STATE_BY_CLOSE"][clock] for clock in expected if clock in tape["STATE_BY_CLOSE"]]
    complete = bool(expected) and len(states) == len(expected)
    scope = "COMPLETE_MATCHED_NATIVE_WINDOW" if complete else (
        "PARTIAL_MATCHED_NATIVE_WINDOW" if states else "UNKNOWN_OUTSIDE_NATIVE_DECISION_LOG"
    )
    core = [row for row in tape["RC1_CORE"] if start <= row["known_time"] < end]
    ready = [row for row in tape["RC1_READY"] if start <= row["confirmed_ms"] < end]
    paper = [
        row for row in tape["DTV_PAPER"]
        if start <= row["known_time"] < end
        or (row["effective_time"] is not None and start <= row["effective_time"] < end)
    ]
    same_core = [row for row in core if row["side"] == side]
    same_ready = [row for row in ready if row["direction"] == ("BUY" if side == 1 else "SELL")]
    fills = [row for row in paper if row["event"] == "SIM_ENTRY" and start <= row["effective_time"] < end]
    same_fills = [row for row in fills if row["side"] == side]
    same_rejected = [row for row in same_core if row["event"] == "REJECTED"]
    cancelled = [row for row in paper if row["side"] == side and row["event"] == "CANCELLED"]
    terminals = [row for row in same_core if row["event"] in ("CANCELLED", "EXPIRED")]
    if same_fills:
        decision = "EXECUTED_NATIVE_EMULATOR"
    elif same_ready or cancelled:
        decision = "READY_WITHOUT_RECORDED_FILL_IN_WINDOW" if complete or cancelled else "READY_FILL_UNKNOWN"
    elif same_rejected:
        decision = "REJECTED"
    elif not complete:
        decision = "UNKNOWN"
    elif same_core:
        decision = "OBSERVED_WATCH_NO_READY_IN_WINDOW"
    else:
        decision = "NO_SAME_SIDE_S5_CANDIDATE_LOGGED"
    reasons = [{"known_ms": row["known_time"], "event": row["event"], "reason": row["reason"]}
               for row in same_rejected + terminals + cancelled]
    reasons.sort(key=lambda row: row["known_ms"])
    phases = Counter(row["permission"] for row in states)
    risks = [check for paper_event in paper if (check := open_risk_checks(paper_event)) is not None]
    occupied = [row for row in states if row["permission"] == "POSITION OCCUPIED"]
    tagged = []
    for row in occupied:
        linked = any(event["known_time"] == row["known_ms"] and event["event"] == "CAPACITY"
                     and event["side"] == side for event in paper)
        tagged.append({"known_ms": row["known_ms"], "paper_plan_id": row["paper_plan_id"],
                       "linked_same_side_capacity_event": linked})
    category = "E_INSUFFICIENT_EVIDENCE" if decision == "UNKNOWN" else "B_EXISTING_RULES"
    if decision == "EXECUTED_NATIVE_EMULATOR":
        category = "EXECUTED_NO_MISSED_ENTRY_CLASS"
    elif any(row["reason"] == "Риск/разходи при отваряне" for row in reasons):
        category = "C_UNSUITABLE_RISK"
    elif cancelled or terminals:
        category = "D_CANCELLED_OR_EXPIRED"
    if not reasons and decision == "NO_SAME_SIDE_S5_CANDIDATE_LOGGED":
        reasons = [{"known_ms": None, "event": "NO_EMITTED_S5_CANDIDATE",
                    "reason": "No same-side CORE/READY emitted in covered closes; internal pre-gates unknown."}]
    if not reasons and decision == "UNKNOWN":
        reasons = [{"known_ms": None, "event": "MISSING_PROTECTED_DECISION_LOG",
                    "reason": "No complete current Visual V2 native S5 decision and paper window."}]
    if not reasons and decision == "OBSERVED_WATCH_NO_READY_IN_WINDOW":
        reasons = [
            {"known_ms": row["known_time"], "event": row["event"], "reason": row["reason"]}
            for row in same_core
        ]
    entry_ids = {row["plan_id"] for row in fills}
    entry_outcomes = [
        row for row in tape["DTV_PAPER"] if row["plan_id"] in entry_ids and row["event"] != "QUEUED"
    ]
    return {
        "scope": scope, "decision": decision, "primary_category": category, "primary_reasons": reasons,
        "expected_confirmed_close_count": len(expected), "logged_confirmed_close_count": len(states),
        "missing_confirmed_close_ms": [clock for clock in expected if clock not in tape["STATE_BY_CLOSE"]],
        "permission_counts": dict(phases), "state_by_close": [{
            **row, "id": f'{row["symbol"]}|{row["frame"]}|{row["known_ms"]}|S5_CLOSE',
            "context": tape["RC1_CTX"][row["known_ms"]],
            "raw_emulator_ledger": tape["LEDGER_BY_CLOSE"][row["known_ms"]],
            "h1_direction": "UNKNOWN_NOT_LOGGED", "h4_direction": "UNKNOWN_NOT_LOGGED",
        } for row in states],
        "occupied_closes_ms": [row["known_ms"] for row in occupied],
        "position_occupancy_evidence": tagged,
        "native_open_rejection_measurements": risks,
        "core_events": core, "ready_plans": ready, "paper_events": paper, "paper_outcomes_for_in_window_fills": entry_outcomes,
        "first_same_side_ready": first_at(same_ready, "confirmed_ms"),
        "first_same_side_fill": first_at(same_fills, "effective_time"),
        "same_side_ready_count": len(same_ready), "same_side_fill_count": len(same_fills),
        "opposing_ready_count": len(ready) - len(same_ready),
        "opposing_fill_count": len(fills) - len(same_fills),
        "absence_table_qualifier": "An occupied slot is observed independently. It cannot explain a missing S5 order without a linked candidate.",
        "positions_at_close_are_paper_only": True,
    }


def exact_market_geometry(bars, frame, start, end):
    if not bars:
        return None
    interval = FRAMES[frame] * MINUTE
    if end - start != len(bars) * interval or any(
        row["time"] != start + index * interval or row["time_close"] != row["time"] + interval
        for index, row in enumerate(bars)
    ):
        return None
    return {
        "open": bars[0]["open"], "close": bars[-1]["close"], "high": max(row["high"] for row in bars),
        "low": min(row["low"] for row in bars),
    }


def observed_elapsed_fraction(market, fill, side):
    if market is None or fill is None:
        return None
    directional_move = side * (market["close"] - market["open"])
    if directional_move <= 0:
        return None
    return {
        "fraction": side * (fill - market["open"]) / directional_move,
        "opening_price": market["open"], "closing_price": market["close"],
        "same_side_native_fill_price": fill, "causal_at_entry": False,
    }


def build_row(kind, frame, movement, tape, archival=None, earlier_quality=None, case=None):
    start, side = identity(movement)
    end = movement["end_ms"]
    current = native_decision(tape, frame, start, end, side)
    native_radar = native_observations(tape, start, end, side, movement.get(frame, {}))
    native_market = exact_market_geometry(native_radar["market_bars"], frame, start, end)
    old_market = movement.get("market") if archival else None
    old_fill = (archival or {}).get("native_s5", {}).get("first_same_side_fill")
    native_fill = current["first_same_side_fill"]
    return {
        "id": case or f"{kind}|{utc(start)}|{side}", "episode_kind": kind, "frame": frame,
        "symbol": "OANDA:XAUUSD", "start_ms": start, "start_utc": utc(start),
        "end_ms": end, "end_utc": utc(end), "side": side, "direction": "BUY" if side == 1 else "SELL",
        "independent_market_label_not_an_entry": kind != "SCREENSHOT_DIAGNOSTIC_FIXTURE",
        "retrospective_market_geometry": {
            "older_archived_market": old_market,
            "current_native_exact_market": native_market,
            "observed_current_fill_hindsight_elapsed_fraction": observed_elapsed_fraction(
                native_market, native_fill["price"] if native_fill else None, side),
            "observed_older_native_fill_hindsight_elapsed_fraction": observed_elapsed_fraction(
                old_market, old_fill["entry"] if old_fill else None, side),
            "directional_extreme_distance": movement.get("directional_extreme_distance"),
        },
        "current_visual_v2_native_s5": current, "current_visual_v2_native_radar": native_radar,
        "older_immutable_native_s5_and_distinct_archived_replays": archival,
        "earlier_m5_entry_quality_research_only": earlier_quality,
    }


def clock(value, key):
    return utc(value[key]) if value else ""


def flat(row):
    now = row["current_visual_v2_native_s5"]
    radar = row["current_visual_v2_native_radar"]
    old = row["older_immutable_native_s5_and_distinct_archived_replays"] or {}
    old_s5 = old.get("native_s5", {})
    old_shadow = old.get("shadow", {})
    old_radar = old.get("existing_radar", {})
    earlier = row["earlier_m5_entry_quality_research_only"] or {}
    observation = radar["registered_extension_observation"] or radar["first_available_same_side_directional_watch_or_confirmation"]
    if observation:
        sources = observation.get("sources") or [observation["source"]]
    else:
        sources = []
    prior = old_radar.get("first")
    market = row["retrospective_market_geometry"]
    return {
        **{key: row[key] for key in ("id", "episode_kind", "frame", "symbol", "start_utc", "end_utc", "direction")},
        "visual_v2_s5_native_scope": now["scope"],
        "visual_v2_s5_native_decision": now["decision"],
        "visual_v2_s5_primary_class": now["primary_category"],
        "visual_v2_s5_native_reason": " | ".join(row["reason"] for row in now["primary_reasons"]),
        "visual_v2_s5_native_reason_utc": " | ".join(utc(row["known_ms"]) for row in now["primary_reasons"] if row["known_ms"] is not None),
        "visual_v2_s5_logged_expected_closes": f'{now["logged_confirmed_close_count"]}/{now["expected_confirmed_close_count"]}',
        "visual_v2_s5_occupied_closes": len(now["occupied_closes_ms"]) if now["logged_confirmed_close_count"] else "",
        "visual_v2_s5_first_same_side_ready_utc": clock(now["first_same_side_ready"], "confirmed_ms"),
        "visual_v2_s5_first_same_side_paper_fill_effective_utc": clock(now["first_same_side_fill"], "effective_time"),
        "visual_v2_s5_first_same_side_paper_fill_known_utc": clock(now["first_same_side_fill"], "known_time"),
        "visual_v2_s5_opposing_ready_record_count": now["opposing_ready_count"] if now["logged_confirmed_close_count"] else "",
        "visual_v2_s5_opposing_paper_fill_record_count": now["opposing_fill_count"] if now["logged_confirmed_close_count"] else "",
        "visual_v2_observation_scope": radar["scope"],
        "visual_v2_first_directional_observation_utc": clock(observation, "known_ms"),
        "visual_v2_first_directional_observation_minutes": (observation["known_ms"] - row["start_ms"]) / MINUTE if observation else "",
        "visual_v2_first_directional_observation_sources": " | ".join(sources),
        "visual_v2_first_shadow_confirmation_utc": clock(radar["first_same_side_shadow_confirmation"], "time_ms"),
        "visual_v2_later_same_side_shadow_diagnostic_candidates_utc": " | ".join(
            utc(value) for value in radar["later_same_side_shadow_diagnostic_candidate_ms"]),
        "visual_v2_registered_independent_continuation_count": (
            len(radar["registered_same_side_independent_continuations"])
            if radar["registered_same_side_independent_continuations"] is not None else ""),
        "visual_v2_shadow_same_side_rejections": " | ".join(sorted({
            event["reason"] for event in radar["shadow"] if event["state"] == "REJECTED" and event["side"] == row["side"]
        })),
        "old_immutable_native_s5_same_side_submissions": old_s5.get("same_side_submission_count", ""),
        "old_immutable_native_s5_same_side_fills": old_s5.get("same_side_fill_count", ""),
        "old_immutable_native_s5_opposing_fills": old_s5.get("opposite_side_fill_count", ""),
        "old_immutable_native_s5_non_admission_reason": old_s5.get("no_same_side_admission_reason") or "",
        "current_native_fill_hindsight_elapsed_fraction": (
            (market["observed_current_fill_hindsight_elapsed_fraction"] or {}).get("fraction", "")),
        "older_native_fill_hindsight_elapsed_fraction": (
            (market["observed_older_native_fill_hindsight_elapsed_fraction"] or {}).get("fraction", "")),
        "old_immutable_native_s5_first_fill_utc": (
            (old_s5.get("first_same_side_fill") or {}).get("entry_effective_utc", "")
        ),
        "old_archived_replayed_radar_first_utc": clock(prior, "known_ms"),
        "old_archived_replayed_radar_first_sources": " | ".join(prior["sources"]) if prior else "",
        "old_archived_replayed_radar_latency_min": old_radar.get("lead_minutes", ""),
        "old_archived_shadow_stage": old_shadow.get("stage_reached", ""),
        "old_archived_shadow_independent_continuations_utc": " | ".join(
            item["known_utc"] for item in old_shadow.get("same_side_independent_continuations", ())),
        "old_archived_s5_quote_side_normal_same_side_fills": (
            (old.get("quote_side_s5_replay") or {}).get("normal", {}).get("same_side_fills_in_hour", "")),
        "old_archived_s5_quote_side_stress_same_side_fills": (
            (old.get("quote_side_s5_replay") or {}).get("stress", {}).get("same_side_fills_in_hour", "")),
        "old_archived_shadow_rejections": " | ".join(sorted(old_shadow.get("rejection_reasons", {}))),
        "older_m5_entry_quality_research_diagnosis": earlier.get("entry_diagnosis", ""),
    }


def count_rows(rows):
    return {
        "rows": len(rows), "distinct_episodes": len({row["id"] for row in rows}),
        "native_decision_scope": Counter(row["current_visual_v2_native_s5"]["scope"] for row in rows),
        "native_decision": Counter(row["current_visual_v2_native_s5"]["decision"] for row in rows),
        "native_primary_category": Counter(row["current_visual_v2_native_s5"]["primary_category"] for row in rows),
        "old_native_same_side_fills": sum(
            (row["older_immutable_native_s5_and_distinct_archived_replays"] or {}).get(
                "native_s5", {}).get("same_side_fill_count", 0) for row in rows
        ),
        "old_native_opposing_fills": sum(
            (row["older_immutable_native_s5_and_distinct_archived_replays"] or {}).get(
                "native_s5", {}).get("opposite_side_fill_count", 0) for row in rows
        ),
    }


def serialize(data, indent=None):
    return (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=indent, allow_nan=False) + "\n").encode("utf-8")


def run(repo, output):
    git_input(repo, "visual_v2_source")
    zip_bytes = git_input(repo, "visual_v2_native")
    with ZipFile(io.BytesIO(zip_bytes)) as archive:
        native = {frame: native_tape(archive, frame) for frame in FRAMES}
    old = json_input(repo, "archived_controls")
    extension = json_input(repo, "extension_episodes")["newly_joined_native_development_episodes"]["hours"]
    prior = {identity(row): row for row in json_input(repo, "older_entry_quality")["hours"]}
    paired = {frame: {identity(row): row for row in old["frames"][frame]["observations"]["hours"]}
              for frame in ("M5", "M15")}
    history = old["m15_frame_own_history"]["observations"]["hours"]
    unpaired_m15 = [row for row in history if identity(row) not in paired["M15"]]
    if len(prior) != 33 or prior.keys() != paired["M5"].keys() or prior.keys() != paired["M15"].keys():
        raise ValueError("The fixed original movement identities must match across the two older frames and archived study")
    if len(history) != 82 or len(unpaired_m15) != 49 or len(extension) != 6:
        raise ValueError("Unexpected archive population or extension count")
    rows = []
    for key in sorted(prior):
        for frame in ("M5", "M15"):
            source = paired[frame][key]
            rows.append(build_row("ARCHIVED_PAIRED_33", frame, source, native[frame],
                                  archival=source, earlier_quality=prior[key] if frame == "M5" else None))
    rows.extend(build_row("ARCHIVED_M15_ONLY_49", "M15", row, native["M15"], archival=row)
                for row in unpaired_m15)
    for movement in extension:
        rows.extend(build_row("JOINED_DEVELOPMENT_EXTENSION_6", frame, movement, native[frame])
                    for frame in ("M5", "M15"))
    for label, start, end, side in CASES:
        case = {"start_ms": ms(start), "end_ms": ms(end), "side": side}
        rows.extend(build_row("SCREENSHOT_DIAGNOSTIC_FIXTURE", frame, case, native[frame], case=label)
                    for frame in FRAMES)
    summary = {
        "schema": "qirvanta.entry_decision_movement_audit.v1",
        "base_commit": BASE,
        "provenance": {key: {"commit": data[0], "git_path": data[1], "sha256": data[2]}
                       for key, data in INPUTS.items()},
        "population": {kind: {frame: count_rows([row for row in rows if row["episode_kind"] == kind and row["frame"] == frame])
                              for frame in FRAMES if any(row["episode_kind"] == kind and row["frame"] == frame for row in rows)}
                       for kind in sorted({row["episode_kind"] for row in rows})},
        "total_rows": len(rows),
        "native_log_coverage": {frame: {"decision_closes": len(tape["DTV_STATE"]),
                                       "decision_first_utc": utc(min(tape["STATE_BY_CLOSE"])),
                                       "decision_last_utc": utc(max(tape["STATE_BY_CLOSE"])),
                                       "matched_shadow_through_utc": utc(tape["DT_END"][0]["time_close"])}
                                for frame, tape in native.items()},
        "shadow_entry_quality_33_m5_historical_only": Counter(row["entry_diagnosis"] for row in prior.values()),
        "known_observed_same_side_fill_elapsed_fractions": Counter(
            "current_visual_v2" if row["retrospective_market_geometry"]["observed_current_fill_hindsight_elapsed_fraction"] else "older_native_s5"
            for row in rows if row["retrospective_market_geometry"]["observed_current_fill_hindsight_elapsed_fraction"]
            or row["retrospective_market_geometry"]["observed_older_native_fill_hindsight_elapsed_fraction"]
        ),
        "decision_notes": {
            "A": "No initial field suggests a native compiler, strategy or state-machine defect in these mounted sources. No claim outside their scope.",
            "B": "Existing rules or source visibility; a missing emitted S5 plan still leaves unlogged internal gates unknown.",
            "C": "Only named native S5 open-risk/cost rejections. Shadow geometry belongs exclusively to research.",
            "D": "Linked decline, expiry or cancellation where supplied. Observed slot occupancy is listed separately without inferring causation.",
            "E": "Missing protected decision journal; old immutable S5 submissions/fills and archived causal research are retained separately.",
        },
        "limitations": [
            "All fixed market labels are retrospective and never authorize a signal.",
            "The latest Visual V2 decision time threshold uses confirmed closes in [start,end); fills are selected by effective time and retain known time.",
            "The two diagnostic fixtures overlap fixed labels and are excluded from population claims.",
            "Current Visual V2 is the pinned native Pine and RAW emulator; no live broker/tick fills.",
            "H1/H4 directions and unlogged Pine pre-gate reasons are unknown; HTF close clocks are kept.",
            "Normal/stress quote-side replay is separate old S5 analysis, not the pinned Visual V2 execution.",
            "Earlier M5 experimental Shadow diagnostics are not order decisions or executable current Visual V2 signals.",
            "Elapsed fraction is signed (recorded same-side fill minus hour open) / (hour close minus hour open), unbounded and ex post, only with true native fills and known oriented endpoints. It uses future close for evaluation only.",
        ],
    }
    flat_rows = [flat(row) for row in rows]
    output.mkdir(parents=True, exist_ok=True)
    (output / "movement-journal.json.gz").write_bytes(gzip.compress(serialize(rows), mtime=0))
    (output / "movement-summary.json").write_bytes(serialize(summary, indent=2))
    with (output / "movements.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tuple(flat_rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(flat_rows)
    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, default=Path.cwd())
    args = parser.parse_args()
    print(json.dumps(run(args.repo, args.output)["population"], ensure_ascii=False, sort_keys=True))

