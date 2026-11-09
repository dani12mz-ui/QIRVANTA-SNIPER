"""Compare actual paired H1 runs after reconciliation; no synthetic subset P&L."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path

from analyze import HERE, REPO, analyze_audit, json_default, metrics

BOUNDARY = int(datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
DATA_FIELDS = ("symbol", "timeframe", "first_bar_time", "last_bar_open", "cutoff", "bar_count", "checksum1", "checksum2", "mintick", "pointvalue", "account_currency", "symbol_currency")


def number(v):
    return None if v is None else D(str(v))


def cohort_name(t):
    return "AUG_SEP_DEVELOPMENT" if t < BOUNDARY else "OCT_NOV_ALREADY_OBSERVED_CONFIRMATION"


def closed_cash_dd(rows):
    equity = peak = worst = D(0)
    for row in sorted(rows, key=lambda r: (r["exit_time"], r["trade_index"])):
        equity += number(row["net_cash"])
        peak = max(peak, equity)
        worst = max(worst, peak - equity)
    return worst


def phases(audit, diagnosis):
    campaigns = diagnosis["campaigns"]
    exit_rows = [row for c in audit["campaigns"] for row in c["exit_rows"]]
    result = {}
    for name in ("AUG_SEP_DEVELOPMENT", "OCT_NOV_ALREADY_OBSERVED_CONFIRMATION"):
        selected = [c for c in campaigns if cohort_name(c["entry_time"]) == name]
        realized = [r for r in exit_rows if cohort_name(r["exit_time"]) == name]
        result[name] = {
            "entry_cohort_complete_campaign_metrics": metrics(selected),
            "entry_cohort_plan_ids": [c["plan_id"] for c in selected],
            "realized_exit_period": {
                "partial_rows": len(realized), "quantity": sum((abs(number(r["size"])) for r in realized), D(0)),
                "gross_cash": sum((number(r["gross_cash_from_prices"]) for r in realized), D(0)),
                "commission_cash": sum((number(r["commission"]) for r in realized), D(0)),
                "net_cash": sum((number(r["net_cash"]) for r in realized), D(0)),
                "closed_cash_drawdown_restarted_at_phase_zero": closed_cash_dd(realized),
                "native_equity_drawdown": None,
                "qualification": "Exit-time cash period, distinct from entry cohort. Closed-cash DD is not native equity DD; no per-phase unrealized equity path supplied."
            }
        }
    result["cross_boundary_campaigns"] = [{"plan_id": c["plan_id"], "entry_time": c["entry_time"], "last_exit_time": c["last_exit_time"], "net": c["net"], "net_r": c["net_r"]} for c in campaigns if c["entry_time"] < BOUNDARY <= c["last_exit_time"]]
    return result


def compare(baseline, candidate):
    differing = {key: [baseline["run"][key], candidate["run"][key]] for key in DATA_FIELDS if baseline["run"][key] != candidate["run"][key]}
    if differing:
        raise ValueError("Paired data/contract fields differ: " + str(differing))
    b, h = analyze_audit(baseline), analyze_audit(candidate)
    b_by = {c["plan_id"]: c for c in b["campaigns"]}
    h_by = {c["plan_id"]: c for c in h["campaigns"]}
    common = sorted(b_by.keys() & h_by.keys())
    added = sorted(h_by.keys() - b_by.keys())
    removed = sorted(b_by.keys() - h_by.keys())
    common_delta = sum((h_by[p]["net"] - b_by[p]["net"] for p in common), D(0))
    def execution_signature(c):
        return {"entry": c["entry"], "quantity": c["quantity"], "net_r": c["net_r"], "parts": [{k: v for k, v in part.items() if k != "trade_index"} for part in c["parts"]]}
    common_execution_changes = [p for p in common if execution_signature(b_by[p]) != execution_signature(h_by[p])]
    added_net = sum((h_by[p]["net"] for p in added), D(0))
    removed_net = sum((b_by[p]["net"] for p in removed), D(0))
    delta = h["metrics"]["net"] - b["metrics"]["net"]
    if delta != added_net - removed_net + common_delta:
        raise ValueError("Plan-id delta does not reconcile")
    bdd = baseline["native_equity_max_drawdown"]
    hdd = candidate["native_equity_max_drawdown"]
    bdd_value = number(bdd["value"] if isinstance(bdd, dict) else bdd)
    hdd_value = number(hdd["value"] if isinstance(hdd, dict) else hdd)
    bpf, hpf = b["metrics"]["profit_factor"], h["metrics"]["profit_factor"]
    return {
        "scope": "EXPLORATORY_ACTUAL_PAIRED_NATIVE_RUNS_NOT_UNTOUCHED_HOLDOUT",
        "data_fields_equal": True, "compared_data_fields": list(DATA_FIELDS),
        "native_property_equality": "Must be established by root settings/source-diff evidence; not inferred from netprofit",
        "baseline": {"run": baseline["run"], "metrics": b["metrics"], "campaign_state_counts": baseline["campaign_state_counts"], "all_native_realized_net_cash": baseline["cash_reconciliation"]["derived_net_cash"], "unclosed_campaigns": [c for c in baseline["campaigns"] if c["state"] != "COMPLETE"], "native_equity_max_drawdown": bdd, "phases": phases(baseline, b)},
        "candidate": {"run": candidate["run"], "metrics": h["metrics"], "campaign_state_counts": candidate["campaign_state_counts"], "all_native_realized_net_cash": candidate["cash_reconciliation"]["derived_net_cash"], "unclosed_campaigns": [c for c in candidate["campaigns"] if c["state"] != "COMPLETE"], "native_equity_max_drawdown": hdd, "phases": phases(candidate, h), "family_contributions": h["family_contributions"]},
        "delta_net_cash": delta,
        "historical_criteria": {"net_cash_improved": h["metrics"]["net"] > b["metrics"]["net"], "candidate_net_positive": h["metrics"]["net"] > 0, "net_pf_improved": hpf is not None and bpf is not None and hpf > bpf, "candidate_pf_above_one": hpf is not None and hpf > 1, "native_equity_dd_not_worse": bdd_value is not None and hdd_value is not None and hdd_value <= bdd_value, "candidate_complete_campaigns": h["metrics"]["n"], "historical_count_below_30": h["metrics"]["n"] < 30, "production_adoption": False},
        "portfolio_difference": {"common_count": len(common), "added_count": len(added), "removed_count": len(removed), "common_net_delta": common_delta, "common_execution_changes": common_execution_changes, "added_net_cash": added_net, "removed_baseline_net_cash": removed_net, "delta_identity": "added - removed + common_delta", "common_changed": [{"plan_id": p, "baseline_net": b_by[p]["net"], "candidate_net": h_by[p]["net"]} for p in common if b_by[p]["net"] != h_by[p]["net"]], "added": [h_by[p] for p in added], "removed": [b_by[p] for p in removed], "common_plan_ids": common}
    }


def main():
    prereg = HERE / "H1_PREREGISTRATION.json"
    if not prereg.exists():
        raise SystemExit("H1 must be registered before comparison")
    target = HERE / "H1_COMPARISON.json"
    if target.exists():
        raise SystemExit("Refusing to overwrite comparison")
    evidence = REPO / "diagnostics/protocol/H1_evidence_comparison.json"
    result = {"hypothesis_registration_sha256": hashlib.sha256(prereg.read_bytes()).hexdigest(), "independent_data_and_inputs_evidence": {"path": evidence.relative_to(REPO).as_posix(), "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}, "input_sha256": {}, "comparisons": {}}
    for family in ("FAST", "ALL"):
        objects = []
        for prefix in ("B", "H1"):
            path = REPO / "diagnostics/accounting" / f"{prefix}-{family}.audit.json"
            result["input_sha256"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
            objects.append(json.loads(path.read_text(encoding="utf-8"), parse_float=D))
        result["comparisons"][family] = compare(*objects)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=json_default) + "\n", encoding="utf-8", newline="\n")
    for name, value in result["comparisons"].items():
        print(name, json.dumps({"baseline": value["baseline"]["metrics"], "candidate": value["candidate"]["metrics"], "criteria": value["historical_criteria"], "delta": value["delta_net_cash"], "phase_baseline": value["baseline"]["phases"], "phase_candidate": value["candidate"]["phases"]}, ensure_ascii=False, default=json_default))


if __name__ == "__main__":
    main()
