"""Freeze the single exploratory H1 before its temporary Pine variant is built."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent

def main():
    target = HERE / "H1_PREREGISTRATION.json"
    if target.exists():
        raise SystemExit("Refusing to overwrite preregistration")
    results_path = HERE / "descriptive_results.json"
    results = json.loads(results_path.read_text(encoding="utf-8"))
    fast = results["runs"]["B-FAST"]
    all_fast = results["runs"]["B-ALL"]["family_contributions"]["2"]
    registration = {
        "registered_at_utc": datetime.now(timezone.utc).isoformat(),
        "hypothesis_id": "H1_FAST_EFFICIENCY_025",
        "baseline_commit": "48e5cddab17ea831bdc277f17f0406329fdfe39c",
        "baseline_standalone_sha256": "c6bfe1af0604c447eddaed1db5195bfe2c07d3826e7fafc70113235e9f25f1a5",
        "status": "EXPLORATORY_TEST_ONLY_NOT_PRODUCTION_ADOPTION",
        "chronology": "The four real baseline journals and all historical descriptive bins were seen before this registration. This registration precedes the parent generating or running the temporary H1 variant. No historical interval is untouched holdout.",
        "mechanism_hypothesis": "Fast breakouts with low directional efficiency may represent noisy continuation attempts that fail before TP1; requiring the existing causal efficiency series >=0.25 may reduce those failures. This is an association to test, not established causality.",
        "single_change": "Require not na(efficiency) and efficiency >=0.25 in the FAST model's existing admission/confirmation gates. Use the existing efficiencyLength=20 global series, computed from present/past chart bars. Retest and Reversal rules remain unchanged.",
        "unchanged": ["all other thresholds/filters", "candidate close-only chronology", "SL/TP/profit-lock/time-limit behavior", "native quantity", "commission/slippage", "OCA/reservations", "UI/alerts", "source production files and baseline", "data feed/history/warmup/date endpoints"],
        "candidate_count_limit": 1,
        "multithreshold_search_allowed": False,
        "artifact_policy": "Generated temporary .txt editor buffer only; no new production .pine and no production-source overwrite",
        "known_descriptive_evidence": {
            "isolated_fast_all": fast["metrics"],
            "isolated_fast_fixed_bins": fast["groups"]["efficiency_bin"],
            "combined_fast_fixed_bins": all_fast["groups"]["efficiency_bin"],
            "qualification": "The six >=0.25 FAST observations are a post-hoc subset of baseline executions, not the H1 strategy result. Removing other entries changes portfolio availability/cooldown/competition. The two runs share these observations and are not independent validation samples. Filtering removes 44/50 observed isolated FAST campaigns (88%)."
        },
        "descriptive_results_sha256": hashlib.sha256(results_path.read_bytes()).hexdigest(),
        "historical_comparison": {
            "symbol": "OANDA:XAUUSD", "timeframe": "15", "bar_count": 6445,
            "cutoff": "2026-11-09T02:45:00Z", "first_bar_open": "2026-08-02T22:00:00Z",
            "checksum1": 52465161, "checksum2": 675559027,
            "ohlc_export_csv_sha256": "7015f6386d2627237c57ff8625d59cbe3c9b852e4742b786cbb4679db3d48c56",
            "runs": ["H1-FAST versus B-FAST", "H1-ALL versus B-ALL"],
            "costs": {"commission_per_unit_per_fill": 0.175, "slippage_ticks": 2, "units_per_leg": 1, "legs": 3},
            "development_interval": "[2026-08-02T22:00:00Z, 2026-10-01T00:00:00Z)",
            "already_observed_confirmation_interval": "[2026-10-01T00:00:00Z, 2026-11-09T02:45:00Z]",
            "subperiod_assignment": "Assign completed campaign by actual entry_time; preserve complete outcomes through common cutoff; separately flag cross-boundary positions. This is descriptive entry-cohort analysis, not contemporaneously frozen out-of-sample testing.",
            "must_report": ["complete campaign and native partial-row counts separately", "net cash and net R", "net profit factor", "native equity maximum drawdown", "completed-campaign cash drawdown separately", "gross/commission/slippage interpretation", "open/partial/incomplete positions", "new/lost/common plan ids and portfolio competition", "both chronological cohorts", "source/data/property hashes and exact cash reconciliation"],
            "failure_signals": ["any accounting/source/data/property mismatch invalidates comparison", "net cash or PF not better than baseline or equity DD worse: no historical support under the registered criteria", "small number of H1 closed campaigns: inconclusive for reliability even if aggregate improves", "contradictory chronological cohorts: instability, not proof of improvement"],
            "historical_results_can_authorize_production": False
        },
        "future_holdout": {
            "interval": "[2026-11-10T00:00:00Z, 2026-12-10T00:00:00Z)",
            "timezone": "UTC", "availability_now": "UNAVAILABLE_FUTURE", "automated_run_or_monitoring_scheduled": False,
            "unchanged_design": "Same OANDA:XAUUSD M15 ordinary candles and declared costs; no retuning after either historical H1 run. Same loaded warmup history for paired baseline/variant.",
            "minimum_sample": "At least 30 complete candidate campaigns in each paired run used for an adoption decision; specifically at least 30 H1-FAST campaigns. Otherwise INCONCLUSIVE, not PASS.",
            "necessary_not_sufficient_pass_criteria": ["complete records reconcile exactly; no hidden open/truncated/loss rows", "candidate net cash >0 and >paired baseline net cash", "candidate net PF is defined, >1 and >paired baseline PF", "candidate native equity max DD <=paired baseline under identical window/properties", "all minimum counts met; report uncertainty and dependence rather than claiming significance from these criteria"],
            "failure_or_inconclusive": "Fail if a necessary performance/risk criterion fails; inconclusive if minimum count, complete data or defined PF is absent. Report still-open positions at the fixed endpoint; do not force-close or omit them to improve completed results.",
            "adoption": "Separate review required; no production adoption or future-profit guarantee follows automatically from this plan"
        }
    }
    target.write_text(json.dumps(registration, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"registered_at_utc": registration["registered_at_utc"], "path": str(target), "sha256": hashlib.sha256(target.read_bytes()).hexdigest()}))

if __name__ == "__main__":
    main()
