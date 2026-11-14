"""Known-at checks for registered H1. No alternative orders, exits, or returns."""

import argparse
from collections import defaultdict
from decimal import Decimal
import json
from pathlib import Path

from gold_pro import rc1_qa as rc1
from research.core_htf_readiness.compare_native import utc
from research.net_campaign_evidence import audit_cooldown


ELIGIBLE = "OBSERVED_EXCLUSIVE_COOLDOWN_AFTER_CANCELLED_NO_FILL"
PROTOCOL_SHA256 = "0853701432c588b1ff95b58c4cb1b8ff45225634d86ce7350c558cbc6755aaa8"
ORIGINAL = "37b903f46a2d878d9d0757da134620c22f2d067d"
RISK_CAP = Decimal("250")
CAPITAL = Decimal("100000")
REFERENCE_UNITS = 3


def cost_scenarios():
    path = Path(__file__).with_name("PROTOCOL.json")
    data = path.read_bytes()
    rc1.require(rc1.digest(data) == PROTOCOL_SHA256, "The original H1 comparison protocol has changed")
    comparison = json.loads(data, parse_float=Decimal)["comparison"]
    return {name: comparison[name] for name in ("normal", "stress")}


def fill_lookback(eda, paper, clock, side, market):
    """Reconcile native entries with the cumulative counter over the decision's eight prior bars."""
    bars = {row["index"]: row for row in eda["bars"]}
    bar_at = {row["close_ms"]: row for row in eda["bars"]}
    current = bar_at.get(clock)
    if current is None:
        return {"status": "UNKNOWN_MISSING_DECISION_BAR"}
    index = current["index"]
    cooldown = current["min_cooldown"]
    first = index - cooldown
    ledger = {row["known_ms"]: row for row in paper[0]}
    fills = defaultdict(list)
    for event in paper[1]:
        if event["event"] == "SIM_ENTRY" and event["known_time"] < clock:
            fills[event["known_time"]].append(event)

    missing = [i for i in range(first - 1, index) if i not in bars or bars[i]["close_ms"] not in ledger]
    if missing:
        return {"status": "UNKNOWN_INCOMPLETE_INDEXED_LOOKBACK", "missing_chart_indices": missing,
                "decision_index": index, "cooldown_bars": cooldown}

    observed = []
    for i in range(first, index):
        stamp = bars[i]["close_ms"]
        prior = bars[i - 1]["close_ms"]
        found = fills[stamp]
        if (ledger[stamp]["entered"] - ledger[prior]["entered"] != len(found)
                or any(e["side"] not in (-1, 1) or e["effective_time"] != bars[i]["open_ms"] for e in found)):
            return {"status": "UNKNOWN_RAW_FILL_LEDGER_MISMATCH", "chart_index": i,
                    "decision_index": index, "cooldown_bars": cooldown}
        for event in found:
            observed.append({
                "event_id": event["event_id"], "plan_id": event["plan_id"], "side": event["side"],
                "effective_open_ms": event["effective_time"], "known_close_ms": stamp,
                "fill_bar_index": i, "bars_before_decision": index - i,
            })
    nearest_same_side = next((e for e in reversed(observed) if e["side"] == side), None)
    latest_logged = next((e for e in reversed(paper[1])
                          if e["event"] == "SIM_ENTRY" and e["side"] == side and e["known_time"] < clock), None)
    if latest_logged is None:
        old_basis = "NONE_SINCE_FIRST_AUDIT_CLOSE" if ledger[min(ledger)]["entered"] == 0 else "UNKNOWN_PREWINDOW_IDENTITY"
    else:
        old_basis = "LATEST_LOGGED_SAME_SIDE_FILL"
    return {
        "status": "FAIL_SAME_SIDE_FILL_WITHIN_COOLDOWN" if nearest_same_side else "PASS_BY_COMPLETE_LOOKBACK",
        "decision_index": index, "cooldown_bars": cooldown,
        "first_checked_close_ms": bars[first]["close_ms"], "last_checked_close_ms": bars[index - 1]["close_ms"],
        "prior_ledger_snapshot_ms": bars[first - 1]["close_ms"],
        "raw_fill_events": observed, "latest_same_side_in_window": nearest_same_side,
        "previous_fill_identity_scope": old_basis,
        "latest_logged_same_side": audit_cooldown.event_identity(latest_logged) if latest_logged else None,
        "initial_audit_raw_entered": ledger[min(ledger)]["entered"],
        "initial_audit_raw_phase": ledger[min(ledger)]["phase"],
        "loaded_chart_first_open_ms": next(iter(market.values()))["time"],
        "alignment": "Pine f_tvCandidate() runs before RAW f_step() at a close. A same-close fill is unavailable to this clock.",
    }


def quote_sensitivity(open_price, plan, tick, scenario, midpoint_risk, maximum_cost_ratio):
    """Measure open-only thresholds at scenario prices, without submitting an entry."""
    direction = plan["side"]
    spread = Decimal(scenario["spread_usd"])
    slippage = Decimal(scenario["slippage_ticks"]) * tick
    fee_per_side = Decimal(scenario["commission_per_side_per_unit_usd"])
    entry_quote = open_price + direction * (spread / 2 + slippage)
    exit_quote = open_price - direction * spread / 2
    quote_risk = direction * (entry_quote - plan["initial_sl"])
    cost = spread + 2 * slippage + 2 * fee_per_side
    cash_risk = REFERENCE_UNITS * (quote_risk + slippage + 2 * fee_per_side)
    notional = REFERENCE_UNITS * entry_quote
    mid_ratio, quote_ratio = cost / midpoint_risk, cost / quote_risk
    return {
        "spread": spread, "slippage_price_per_side": slippage, "commission_per_side_per_unit": fee_per_side,
        "modeled_roundtrip_cost_per_unit": cost,
        "modeled_entry_quote_including_slippage": entry_quote,
        "modeled_liquidation_side_quote": exit_quote,
        "quote_distance_to_initial_sl": quote_risk,
        "quote_distance_to_tp1": direction * (plan["tp1"] - entry_quote),
        "modeled_sl_open_quote_pass": direction * (exit_quote - plan["initial_sl"]) > 0,
        "modeled_tp1_entry_quote_pass": direction * (plan["tp1"] - entry_quote) > 0,
        "reference_units_only": REFERENCE_UNITS, "reference_cash_risk_including_cost": cash_risk,
        "reference_notional": notional, "reference_cash_risk_within_prior_250_cap": cash_risk <= RISK_CAP,
        "reference_notional_within_prior_100000_capital": notional <= CAPITAL,
        "all_in_cost_over_midpoint_risk": mid_ratio,
        "all_in_cost_over_entry_quote_risk": quote_ratio,
        "if_all_in_cost_substituted_for_fixed_pine_cost_midpoint_ratio_pass": mid_ratio <= maximum_cost_ratio,
        "if_all_in_cost_substituted_for_fixed_pine_cost_quote_ratio_pass": quote_ratio <= maximum_cost_ratio,
        "scope": "Sensitivity only. Modeled quotes and three reference units are not fills, sizing, or a change to the fixed Pine cost gate.",
    }


def native_open_check(run, eda, market, case, scenarios):
    clock, side = case["close_ms"], case["side"]
    bars = {b["close_ms"]: b for b in eda["bars"]}
    current = bars[clock]
    core = {event["event_id"]: event for event in run.core}
    plan = core.get(case["confirmation"]["event_id"])
    next_bar = next((b for b in market.values() if b["index"] == current["index"] + 1), None)
    if plan is None or next_bar is None or next_bar["time_close"] not in bars:
        return {"status": "UNKNOWN_MISSING_PLAN_OR_NEXT_OPEN", "candidate_execution_performed": False}
    if next_bar["time"] != clock:
        return {
            "status": "ENTRY_SESSION_GAP_UNFILLED_IN_REGISTERED_COMPARISON",
            "next_available_open_ms": next_bar["time"], "candidate_execution_performed": False,
            "scope": "The comparison needs a contiguous next open. Pine's actual RAW behavior is a separate source predicate.",
        }

    opening = rc1.json_number(next_bar["open"], "next observed market open")
    atr = plan["atr_at_signal"]
    signed_risk = side * (opening - plan["initial_sl"])
    drift = abs(opening - plan["reference"])
    target_distance = side * (plan["tp1"] - opening)
    cost_ratio = plan["roundtrip_cost_price"] / signed_risk if signed_risk > 0 else None
    min_risk, max_risk = current["min_stop_atr"] * atr, plan["max_risk_atr"] * atr
    max_drift = current["max_open_dev_atr"] * atr
    reasons = {
        "WRONG_SIDE_OF_SL_OR_TP1": signed_risk < current["mintick"] or target_distance <= 0,
        # Both news switches are constant false in the pinned Visual V2.
        "NEWS_AT_OPEN": False,
        "DEVIATION_ABOVE_LIMIT": drift > max_drift,
        "RISK_OR_COST_AT_OPEN": not (signed_risk > 0 and min_risk <= signed_risk <= max_risk
                                      and cost_ratio <= current["max_cost_risk"]),
        # The pinned source disables the room gate. Preserve it for this diagnosis.
        "INSUFFICIENT_ROOM_AT_OPEN": (
            plan["required_room_r"] > 0
            and (plan["obstacle"] is None or side * (plan["obstacle"] - opening) + current["mintick"] * Decimal("0.000001")
                 < max(signed_risk * plan["required_room_r"], target_distance))
        ),
    }
    first = next((k for k, failed in reasons.items() if failed), None)
    result = {
        "status": "ORIGINAL_PINE_GATE_FAILS" if first else "ORIGINAL_PINE_GATE_PASSES_DIAGNOSTICALLY",
        "next_open_effective_ms": next_bar["time"], "next_open_effective_utc": utc(next_bar["time"]),
        "confirmed_tape_bar_close_ms": next_bar["time_close"],
        "baseline_executed_candidate": False, "candidate_execution_performed": False,
        "observed_midpoint_open": opening, "plan_id": plan["plan_id"], "reference": plan["reference"],
        "initial_stop": plan["initial_sl"], "targets": [plan["tp1"], plan["tp2"], plan["tp3"]],
        "atr_at_confirmation": atr, "midpoint_stop_distance": signed_risk,
        "midpoint_stop_distance_min": min_risk, "midpoint_stop_distance_max": max_risk,
        "midpoint_distance_to_tp1": target_distance, "midpoint_abs_deviation": drift,
        "midpoint_abs_deviation_max": max_drift, "source_roundtrip_friction": plan["roundtrip_cost_price"],
        "source_cost_over_midpoint_risk": cost_ratio, "source_cost_over_risk_max": current["max_cost_risk"],
        "room_requirement": plan["required_room_r"], "reference_obstacle": plan["obstacle"],
        "original_first_failure": first, "all_observed_predicate_failures": reasons,
        "scope": "Only next bar time and open were used. No highs, lows, closes, modeled fill, exits, P&L, or new admission.",
    }
    if not first:
        result["quote_sensitivity"] = {k: quote_sensitivity(
            opening, plan, current["mintick"], assumptions, signed_risk, current["max_cost_risk"]
        ) for k, assumptions in scenarios.items()}
    return result


def diagnose_case(run, eda, paper, market, case, scenarios=None):
    """Supplement one existing audit_cooldown case without changing its disposition."""
    lookback = fill_lookback(eda, paper, case["close_ms"], case["side"], market)
    opposite_or_same_ready = [row["source_id"] for row in run.ready if row["confirmed_ms"] == case["close_ms"]]
    candidate_id = case["confirmation"]["plan_id"] if case["confirmation"] else None
    baseline_events = [e for e in paper[1] if e["plan_id"] == candidate_id]
    baseline_fill = any(e["event"] == "SIM_ENTRY" for e in baseline_events)
    original_core_at_close = [row for row in run.core if row["known_time"] == case["close_ms"]]
    had_competing_confirmation = any(row["event"] == "CONFIRMED" for row in original_core_at_close)
    empty_slot = case["original_raw_slot_empty_after_close"]

    opening = {"status": "NOT_ASSESSED_OUTSIDE_REGISTERED_CASE", "candidate_execution_performed": False}
    if case["disposition"] == ELIGIBLE:
        if lookback["status"] != "PASS_BY_COMPLETE_LOOKBACK":
            opening["status"] = "NOT_ASSESSED_ACTUAL_FILL_COOLDOWN_UNRESOLVED_OR_ACTIVE"
        elif not empty_slot or had_competing_confirmation or baseline_events or opposite_or_same_ready:
            opening["status"] = "NOT_ASSESSED_BASELINE_OCCUPANCY_OR_EVIDENCE_CONFLICT"
        else:
            opening = native_open_check(run, eda, market, case, scenarios if scenarios is not None else cost_scenarios())
    return {
        "original_disposition": case["disposition"],
        "frame": case["frame"], "close_ms": case["close_ms"], "side": case["side"],
        "baseline_ready_source_ids_at_close": opposite_or_same_ready,
        "baseline_raw_events_for_candidate": [audit_cooldown.event_identity(e) for e in baseline_events],
        "baseline_candidate_has_sim_entry": baseline_fill,
        "original_raw_slot_empty_after_close": empty_slot,
        "native_same_side_fill_cooldown_lookback": lookback,
        "hypothetical_next_open_only": opening,
    }


def capture_path(item, core_dir, entry_dir, new_m15):
    origin, folder, name = item["source_id"].split("/")
    if item["calculated_bars_setting"] == 5000:
        return audit_cooldown.pinned_path(new_m15.parent, new_m15.name, item["sha256"])
    directory = core_dir if origin == "core_htf_readiness" else entry_dir / folder
    return audit_cooldown.pinned_path(directory, name, item["sha256"])


def audit_open_checks(core_dir, entry_dir, new_m15=None):
    scenarios = cost_scenarios()
    existing = audit_cooldown.audit_inputs(core_dir, entry_dir, new_m15)
    loaded = {}
    inputs = {item["source_id"]: item for item in existing["inputs"]}
    for group in existing["unique_observed_cases"]:
        extensions = []
        for case in group["captures"]:
            source = case["source_id"]
            item = inputs[source]
            if source not in loaded:
                path = capture_path(item, core_dir, entry_dir, new_m15)
                loaded[source] = audit_cooldown.read_capture(
                    path, item["frame"], item["registered_window_utc"], source.startswith("core_htf_readiness/")
                )
            extensions.append({"source_id": source, **diagnose_case(*loaded[source], case, scenarios)})
        group["opening_and_fill_clock_evidence_by_capture"] = extensions
    return {
        **existing, "schema": "qirvanta.net-campaign-evidence.cooldown-open.v1",
        "source_visual_v2": ORIGINAL, "registered_protocol_sha256": PROTOCOL_SHA256,
        "candidate_orders_executed": 0, "candidate_exits_evaluated": 0,
        "evaluation_state": "DIAGNOSTIC_ONLY_PRECONDITIONS_NOT_MET",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-dir", type=Path, required=True)
    parser.add_argument("--entry-dir", type=Path, required=True)
    parser.add_argument("--new-m15", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit_open_checks(args.core_dir, args.entry_dir, args.new_m15)
    args.output.write_text(rc1.encode(result) + "\n", encoding="utf8", newline="\n")


if __name__ == "__main__":
    main()

