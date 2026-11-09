"""Freeze descriptive bins and change-selection limits before calculating them."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]

def main():
    output = HERE / "analysis_plan.json"
    if output.exists():
        raise SystemExit("Analysis plan already registered; do not overwrite")
    paths = sorted((REPO / "diagnostics/evidence").glob("B-*.pine-logs.csv"))
    plan = {
        "registered_at_utc": datetime.now(timezone.utc).isoformat(),
        "baseline_commit": "48e5cddab17ea831bdc277f17f0406329fdfe39c",
        "chronology": "Registered before this agent calculates the descriptive bins. Existing B-ALL aggregate and prior visible contributions were already known. These historical datasets are not unseen holdout data.",
        "known_before_registration": {"B-ALL": {"complete_campaigns": 72, "closed_parts": 212, "net_cash": -144.492}},
        "inputs_sha256": {p.relative_to(REPO).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
        "population": "COMPLETE reconciled native campaigns only; isolated runs separately; combined contributions not added to isolated outcomes",
        "predeclared_descriptive_dimensions": [
            "exit path with quantity/gross/fees/net; SL_FULL, TP1_THEN_LOCKED_SL_CONSISTENT, TP_THEN_SAMEBAR_INITIAL_SL, TP_THEN_OTHER_SL, ALL_TARGETS, TIME, GAP_LABEL, MIXED_OTHER",
            "efficiency <0.25 versus >=0.25 (threshold supplied by parent before analysis); null separate",
            "original local_aligned flag true/false/null",
            "original countertrend flag and independently preserved WITH_HTF/AGAINST_HTF/NEUTRAL_HTF relation",
            "signed entry-reference deviation divided by frozen signal ATR and initial planned R; baseline absolute ATR threshold <=0.20 versus >0.20",
            "predeclared signed planned-R bins <=0, (0,0.10], >0.10; descriptive only"
        ],
        "cost_definition": "Gross cash from actual native entry/exit/qty/pointvalue; subtract observed native commission exactly once. Slippage is already in prices; do not subtract it again. Gross-before-slippage counterfactual unavailable.",
        "lock_consistency_definition": "Requires TP1 on an earlier bar, subsequent SL, and SL price matching rounded entry + side*actual_initial_risk*0.05 minus side*2 ticks. Other TP/SL sequences kept separately; matching is source-policy consistency, not a claim to reconstruct intrabar path.",
        "small_sample_rule": "Every cell prints N. N<20 flagged SMALL_N; even larger cells remain exploratory and correlated.",
        "candidate_limit": 1,
        "candidate_rule": "At most one testable mechanism hypothesis after description, only with coherent evidence in independent isolated-model records plus combined contribution. No optimizing thresholds, disabling losing families wholesale, or treating a filtered retrospective subset as a strategy rerun.",
        "candidate_preference_if_evidence_exists": "Fast-entry confirmation/context quality (original local alignment or fixed efficiency 0.25), not looser filters or blanket removal of the losing model.",
        "change_authorization": "No production changes in this stage. A separate frozen hypothesis/test plan must precede any future edit or rerun.",
        "causality_and_performance_claim": False,
        "genuine_unseen_holdout": False,
    }
    output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"registered_at_utc": plan["registered_at_utc"], "input_count": len(paths)}))

if __name__ == "__main__":
    main()
