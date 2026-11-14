"""Registered diagnostic only: cooldown after a native zero-fill cancellation.

One fixed hypothesis (2026-11-14, before any alternative execution). Keep the
original strict >8-bar same-side cooldown following filled plans. A distinct
fresh native S5 seed, born strictly after a preceding same-side plan's native
next-open RAW CANCELLED was known, is in scope only when that plan was queued
and never filled, and cooldown is the sole veto after checking all other
original confirmation and risk gates. No CAPACITY exception, resubmission,
threshold search, or bypass of another gate. Scope is matching registered
OANDA:XAUUSD standard confirmed-bar logs before the protected period. The
output classifies observations and missing evidence without simulating trades.
"""

import argparse
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

from gold_pro import rc1_qa as rc1
from research.core_htf_readiness.compare_native import historical_candles, load_run
from research.direct_trend_visibility.native_qa import journal_rows, paper_evidence
from research.entry_decision_audit.native_qa import diagnostics, timestamp
from research.professional_signal_selector.native_qa import log_messages


NOV11 = ("2026-11-11T00:00:00Z", "2026-11-11T17:30:00Z")
NOV13 = ("2026-11-13T12:00:00Z", "2026-11-13T19:00:00Z")
COOLDOWN = "Пауза след потвърден модел"
SEALED_START = timestamp("2026-11-16T00:00:00Z")
# Conservative buffer around values serialized by Pine to eight decimal places.
ROUNDING_GUARD = Decimal("0.00000005")
CORE_FILES = {
    (5, 1223): "deb9ad4d31f8e922c6d4fd0bec8c453a98306be89300795db3b0a5e283c5ce38",
    (5, 3200): "85d5626023f5564b64a9fc491647ea11326f0ef6bb074e2d3473259fb38a8493",
    (15, 1223): "5447d32110594419a5effe5a27acc6d4272c4931a8cc86ea1b2e884c5420bdc1",
    (15, 3200): "33055310d814d2cfbf6f39606683de1246eb3bf8058aa21e8e2af17cb15faf8b",
    (30, 1223): "e6e129b52466548fa7af310138d3c39c9e22cb7fa9c53d6003afe2a911c169ff",
    (30, 3200): "3f3bd7fd33dc9442aa7bf953a7a67022d9a3fc683dfac09fc9a33f3a1d1fadd0",
}
ENTRY_FILES = {
    (5, "native"): "953486506b06eda44884097ad3860af8e7e678b18f4fff902b0ad934fd90359a",
    (15, "native"): "3496ac681806935e788bff24a23421992af3cc390046e6cc1b5eebc0a278f854",
    (30, "native"): "f92ad5f59fb97103eef3d305fbb78587928120a7a3ff5b972ad358912d0270c6",
    (5, "native-recent"): "aa303f677fca2c54ab8b7641f58c800dbf24371f7e0a53b87ae037b98da8855d",
    (30, "native-recent"): "447d939e0573387007b526690cd3243e8c42ff178bb0268bb6411c6002a649e4",
}
SOURCE_COMMITS = {
    "core_htf_readiness": "56c9dd25d506341042e8cc3c078fcfb92be650b8",
    "entry_decision_audit": "4875918c17db967b706364c35c7cd074c5fbc14d",
    "visual_v2": "37b903f46a2d878d9d0757da134620c22f2d067d",
}


def pinned_path(directory, name, digest):
    path = directory / name
    rc1.require(rc1.digest(path.read_bytes()) == digest, f"Unexpected source bytes: {path.name}")
    return path


def read_capture(path, frame, window, core):
    start, end = map(timestamp, window)
    messages = log_messages(path)
    if core:
        verified = load_run(path, frame, start, end)
        market = verified["market"]
    else:
        _, market = historical_candles(messages, frame)
    run = rc1.read_export(path, frame, "indicator")
    expected = tuple(close for close in market if start <= close <= end)
    rc1.require(run.meta["end_marker_present"] and expected and end < SEALED_START,
                "Missing audit marker or invalid UTC window")
    rc1.require(tuple(bar.closing for bar in run.context) == expected,
                "Incomplete or unordered confirmed-bar context")
    rc1.require(all((market[ctx.closing]["index"], market[ctx.closing]["time"]) == (ctx.index, ctx.opening)
                    for ctx in run.context), "EDA chart indices are inconsistent with DT_MARKET")
    legacy = journal_rows(path)
    paper = paper_evidence(legacy, run.context, frame)
    rc1.require(paper is not None, "Missing RAW PAPER ledger")
    eda = diagnostics(messages, frame, run, legacy, path)
    return run, eda, paper, market


def confirmed_gates(bar, side, event):
    atr = side["atr"]
    risk = (event["reference"] - event["initial_sl"]) * event["side"]
    cost = event["roundtrip_cost_price"]
    d = event["side"]
    levels = [event[key] for key in ("reference", "tp1", "tp2", "tp3")]
    epsilon = bar["mintick"] * Decimal("0.000001")
    body_min = Decimal("0.25") * atr
    close_fraction_min = Decimal("0.65")
    risk_min = bar["min_stop_atr"] * atr
    risk_max = event["max_risk_atr"] * atr
    friction = cost / risk if risk > 0 else None
    formation_age = bar["index"] - side["before_born_index"]
    validity = {
        "warmup": bar["warmup"],
        "date": bar["date_pass"],
        "no_news_pause": not bar["news_paused"],
        "session": bar["session_pass"],
        "valid_atr": bar["atr_pass"],
        "no_gap": not bar["gap"],
        "bar_range": safe_margin(bar["max_entry_range_atr"] * atr - bar["bar_range"]),
        "directional_body": False if side["signed_body"] <= 0 else safe_margin(side["signed_body"] - body_min),
        "directional_close": safe_margin(side["directional_close_fraction"] - close_fraction_min),
        "watch_within_three_bars": 1 <= formation_age <= 3,
        "anchor_intact": (bar["close"] - side["before_anchor"]) * d >= 0,
        "reclaimed_level": (bar["close"] - side["before_level"]) * d > 0,
        "minimum_risk_tick": risk >= bar["mintick"],
        "min_risk_atr": safe_margin(risk + epsilon - risk_min),
        "max_risk_atr": safe_margin(risk_max - risk),
        "cost_to_risk": None if friction is None else safe_margin(bar["max_cost_risk"] - friction),
        "ordered_targets": all((b - a) * d > 0 for a, b in zip(levels, levels[1:])),
        "original_room_gate_disabled": event["required_room_r"] == 0,
    }
    return validity, {
        "formation_age_bars": formation_age,
        "signed_body": side["signed_body"], "body_min": body_min,
        "directional_close_fraction": side["directional_close_fraction"],
        "directional_close_fraction_min": close_fraction_min,
        "atr": atr, "atr_mean": bar["atr_mean"],
        "atr_ratio_min": bar["min_atr_ratio"], "atr_ratio_max": bar["max_atr_ratio"],
        "bar_range": bar["bar_range"], "bar_range_max": bar["max_entry_range_atr"] * atr,
        "initial_price_risk": risk, "minimum_price_risk_atr": risk_min, "maximum_price_risk_atr": risk_max,
        "required_tick": bar["mintick"], "roundtrip_cost_price": cost, "cost_to_risk": friction,
        "cost_to_risk_max": bar["max_cost_risk"],
        "room_r_requirement": event["required_room_r"],
        "H1_FVG_PD_filters": "DISABLED_IN_PINNED_VISUAL_V2",
        "core_htf_ready": bar["readyHTF"], "core_htf": bar["core_htf"],
        "rc2_filter_active": bar["rc2_filter_active"],
    }


def safe_margin(margin):
    return None if abs(margin) <= ROUNDING_GUARD else margin > 0


def event_identity(event):
    return {k: event[k] for k in (
        "event", "event_id", "plan_id", "known_time", "effective_time", "price", "reason",
        "reference", "initial_sl", "tp1", "tp2", "tp3", "atr_value",
    ) if k in event}


def prior_lifecycle(prior, paper_by_plan, bars, ledgers, indices, current):
    if prior is None:
        return "UNKNOWN_PREDECESSOR_OUTSIDE_COMPLETE_JOURNAL", []
    plan_events = [e for e in paper_by_plan[prior["plan_id"]] if e["known_time"] < current["close_ms"]]
    fills = [e for e in plan_events if e["event"] == "SIM_ENTRY"]
    queued = next((e for e in plan_events if e["event"] == "QUEUED"), None)
    cancelled = next((e for e in plan_events if e["event"] == "CANCELLED"), None)
    if fills:
        return "COVERED_PREDECESSOR_FILLED", plan_events
    if any(e["event"] == "CAPACITY" for e in plan_events):
        return "COVERED_PREDECESSOR_CAPACITY", plan_events
    if not queued or not cancelled:
        return "UNKNOWN_PREDECESSOR_OUTCOME", plan_events
    start, end = queued["known_time"], cancelled["known_time"]
    if (queued["known_time"] != prior["known_time"] or start not in ledgers or end not in ledgers
            or indices.get(bars[end]["index"] - 1) != start
            or cancelled["effective_time"] != bars[end]["open_ms"]
            or bars[end]["before_paper_phase"] != 1 or bars[end]["before_paper_plan_id"] != prior["plan_id"]
            or ledgers[start]["entered"] != ledgers[end]["entered"]
            or ledgers[start]["phase"] != 1 or ledgers[start]["plan_id"] != prior["plan_id"]):
        return "UNKNOWN_CANCELLATION_CHAIN", plan_events
    return "COVERED_PREDECESSOR_CANCELLED_NO_FILL", plan_events


def audit_cases(run, eda, paper):
    bars = {row["close_ms"]: row for row in eda["bars"]}
    ledgers = {row["known_ms"]: row for row in paper[0]}
    indices = {row["index"]: row["close_ms"] for row in eda["bars"]}
    core_at = defaultdict(list)
    paper_by_plan = defaultdict(list)
    for event in run.core:
        core_at[event["known_time"], event["side"]].append(event)
    for event in paper[1]:
        paper_by_plan[event["plan_id"]].append(event)
    cases = []
    for side in eda["sides"]:
        if not side["raw"] or side["cooldown_pass"]:
            continue
        stamp, direction = side["close_ms"], side["side"]
        bar = bars[stamp]
        now = core_at[stamp, direction]
        rejection = next((e for e in now if e["event"] == "REJECTED"), None)
        previous_index = side["last_accepted_index"]
        prior_at = indices.get(previous_index)
        prior = next((e for e in core_at[prior_at, direction] if e["event"] == "CONFIRMED"), None)
        prior_status, previous = prior_lifecycle(prior, paper_by_plan, bars, ledgers, indices, bar)
        cancellation = next((e for e in previous if e["event"] == "CANCELLED"), None)
        age = bar["index"] - previous_index if previous_index is not None else None
        checks, values = {}, {}
        if rejection and side["before_phase"] == 1 and side["before_born_index"] is not None:
            checks, values = confirmed_gates(bar, side, rejection)
        seed = side["before_born_ms"]
        watch = next((e for e in core_at[seed, direction] if e["event"] == "WATCH"), None)
        fresh = bool(cancellation and seed is not None and seed > cancellation["known_time"])
        if not rejection or age is None or not 0 <= age <= bar["min_cooldown"] or not checks:
            disposition = "UNKNOWN_ORIGINAL_COOLDOWN_EVIDENCE"
        elif rejection["reason"] != COOLDOWN or side["reason"] != COOLDOWN:
            disposition = "EXCLUDED_OTHER_NATIVE_REASON"
        elif False in checks.values():
            disposition = "EXCLUDED_ANOTHER_ORIGINAL_GATE_FAILED"
        elif None in checks.values():
            disposition = "UNKNOWN_ROUNDING_AT_ORIGINAL_GATE"
        elif prior_status == "COVERED_PREDECESSOR_FILLED":
            disposition = "EXCLUDED_PREDECESSOR_FILLED"
        elif prior_status == "COVERED_PREDECESSOR_CAPACITY":
            disposition = "EXCLUDED_PREDECESSOR_CAPACITY"
        elif prior_status != "COVERED_PREDECESSOR_CANCELLED_NO_FILL":
            disposition = prior_status
        elif not fresh:
            disposition = "EXCLUDED_SEED_NOT_AFTER_CANCELLATION"
        elif watch is None or rejection["created_time"] != seed or watch["price"] != side["before_level"]:
            disposition = "UNKNOWN_FRESH_SEED_IDENTITY"
        else:
            disposition = "OBSERVED_EXCLUSIVE_COOLDOWN_AFTER_CANCELLED_NO_FILL"
        cases.append({
            "frame": int(bar["frame"]), "close_ms": stamp, "side": direction,
            "disposition": disposition, "current_reason": side["reason"],
            "confirmation": event_identity(rejection) if rejection else None,
            "current_paper_phase_before": bar["before_paper_phase"],
            "current_paper_phase_after": bar["after_paper_phase"],
            "previous_accepted_time_ms": prior_at, "since_previous_accepted_bars": age,
            "original_cooldown_bars": bar["min_cooldown"],
            "previous_lifecycle": prior_status,
            "previous_paper_events": [event_identity(e) for e in previous],
            "current_seed_ms": seed, "current_frozen_level": side["before_level"],
            "current_seed_watch": event_identity(watch) if watch else None,
            "original_raw_slot_empty_after_close": bar["after_paper_phase"] == 0,
            "current_frozen_anchor": side["before_anchor"],
            "seed_strictly_after_cancellation_known": fresh if cancellation else None,
            "known_original_checks_without_cooldown": checks,
            "original_values": values,
        })
    return cases


def audit_inputs(core_dir, entry_dir, new_m15=None):
    registered = [
        ("core_htf_readiness", core_dir, f"M{frame}-{depth}-add.csv", digest, frame, depth, NOV11)
        for (frame, depth), digest in CORE_FILES.items()
    ] + [
        ("entry_decision_audit", entry_dir / folder, f"audit-on-M{frame}.csv", digest, frame, 1223,
         NOV11 if folder == "native" else NOV13)
        for (frame, folder), digest in ENTRY_FILES.items()
    ]
    if new_m15 is not None:
        registered.append((
            "core_htf_readiness", new_m15.parent, new_m15.name,
            "230c37c21558fcd3dd1790e4689059f2d9808fbbc1efd594688b837071e187a7", 15, 5000, NOV11,
        ))
    sources = []
    observed_closes = set()
    groups = defaultdict(list)
    for origin, directory, name, digest, frame, depth, window in registered:
        scope = directory.name if origin == "entry_decision_audit" else "native"
        source_id = f"{origin}/{scope}/{name}"
        summary = {"source_id": source_id, "commit": SOURCE_COMMITS[origin], "sha256": digest,
                   "frame": frame, "calculated_bars_setting": depth, "registered_window_utc": window}
        if depth == 5000:
            summary["commit"] = None
            summary["probe_based_on_commit"] = SOURCE_COMMITS["core_htf_readiness"]
        sources.append(summary)
        try:
            path = pinned_path(directory, name, digest)
            run, eda, paper, market = read_capture(path, frame, window, origin == "core_htf_readiness")
        except (OSError, rc1.EvidenceError) as exc:
            summary.update(status="UNKNOWN", blocker=str(exc))
            continue
        observed_closes.update((frame, row["close_ms"]) for row in eda["bars"])
        cases = audit_cases(run, eda, paper)
        confirmed = sum(e["event"] == "CONFIRMED" for e in run.core)
        summary.update(
            status="VERIFIED_BOUNDED_NATIVE_INPUT", actual_chart_bars=len(market),
            first_loaded_open_ms=run.context[0].first_open, logged_closes=len(eda["bars"]),
            warmed_closes=sum(row["warmup"] for row in eda["bars"]),
            unavailable_core_closes=sum(not row["readyHTF"] for row in eda["bars"]),
            raw_s5_candidates=sum(row["raw"] for row in eda["sides"]),
            core_confirmed=confirmed, ready=len(run.ready),
            core_reject_reasons=Counter(e["reason"] for e in run.core if e["event"] == "REJECTED"),
            raw_paper_kinds=Counter(e["event"] for e in paper[1]),
            cases_with_cooldown_false=len(cases),
        )
        for case in cases:
            groups[frame, case["close_ms"], case["side"]].append({"source_id": source_id, **case})
    unique_cases = []
    for (frame, stamp, side), captures in sorted(groups.items()):
        observed = sorted({case["disposition"] for case in captures})
        unique_cases.append({
            "frame": frame, "close_ms": stamp, "side": side, "capture_count": len(captures),
            "disposition": observed[0] if len(observed) == 1 else "UNKNOWN_DISAGREEMENT_ACROSS_HISTORIES",
            "disposition_by_capture": observed, "captures": captures,
        })
    return {
        "schema": "qirvanta.net-campaign-evidence.cooldown.v1",
        "registered_arms": 1,
        "rule": (__doc__ or "").split("\n\n", 1)[1].strip(),
        "source_commits": SOURCE_COMMITS,
        "inputs": sources,
        "unique_observed_cases": unique_cases,
        "unique_logged_closes_by_frame": Counter(frame for frame, _ in sorted(observed_closes)),
        "unique_case_counts": Counter(case["disposition"] for case in unique_cases),
        "deduplication": "M5/M15/M30 chart frame, close UTC ms and side. Depth captures of the same event are not independent.",
        "scope": "Only native observations in complete and pinned archival windows. Cross-copy parity for supplemental Nov13 M30 remains unverified in the source study. No simulated or realized candidate P&L, fills, opportunity coverage, holdout, or promotion decision.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-dir", type=Path, required=True)
    parser.add_argument("--entry-dir", type=Path, required=True)
    parser.add_argument("--new-m15", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = audit_inputs(args.core_dir, args.entry_dir, args.new_m15)
    args.output.write_text(rc1.encode(results) + "\n", encoding="utf8", newline="\n")


if __name__ == "__main__":
    main()

