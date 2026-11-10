"""Reconcile the pinned native controls and optionally compare Premium observer exports."""

import argparse
import json
from pathlib import Path

from diagnostics.accounting.audit import audit, json_default, read_jsonl
from unified.tools.audit_tv_observations import ALIGNMENT_FIELDS, validate_execution
from unified.tools.audit_ultimate_native import without_run_id
from unified.tools.build_sniper_premium import BASELINE_SHA, OUTPUT, ROOT, sha, verify_source


GRID = {(mode, tf) for mode in ("S0", "S5") for tf in ("5", "15", "30")}
MATRIX = ROOT / "docs/tradingview_only_evidence/native_matrix.json"


def read_runs(paths, source_sha, *, pinned_matrix=None):
    runs = {}
    for path in paths:
        records, _ = read_jsonl(path, allow_pine_prefix=True, origin_override="REAL_TRADINGVIEW")
        data = audit(records)
        run = data["run"]
        prefix, mode, model = run["run_id"].split("|")
        key = (mode, run["timeframe"])
        if (prefix != "tv_final" or model != "Всички" or key not in GRID or key in runs
                or run["symbol"] != "OANDA:XAUUSD" or run["source_sha256"] != source_sha):
            raise ValueError(f"Unexpected run registration: {path.name}")
        digest = sha(path.read_bytes())
        if pinned_matrix and digest != pinned_matrix["timeframes"][key[1]][mode]["log_sha256"]:
            raise ValueError(f"Control export differs from the published archive: {path.name}")
        if data["status"] != "PASS_OBSERVED_JOURNAL" or not data["cash_reconciliation"]["matches"]:
            raise ValueError(f"Native journal incomplete or unreconciled: {path.name}")
        validate_execution(data)
        runs[key] = data, digest
    if set(runs) != GRID:
        raise ValueError(f"Missing required S0/S5 timeframes: {sorted(GRID - runs.keys())}")
    return runs


def comparison(a, b):
    exact = ("campaigns", "submitted_without_recorded_fill", "campaign_state_counts",
             "complete_campaign_metrics", "observed_closed_row_metrics", "cash_reconciliation",
             "native_equity_max_drawdown")
    differences = [key for key in exact if without_run_id(a[key]) != without_run_id(b[key])]
    differences += ["run." + key for key in ALIGNMENT_FIELDS if a["run"][key] != b["run"][key]]
    return differences


def compact(row):
    data, digest = row
    m = data["complete_campaign_metrics"]
    complete = [c for c in data["campaigns"] if c["state"] == "COMPLETE"]
    returns = [c["net_r"] for c in complete if c["net_r"] is not None]
    return {
        "log_sha256": digest, "data": {k: data["run"][k] for k in ALIGNMENT_FIELDS},
        "campaigns": m["count"], "wins": m["wins"], "losses": m["losses"], "net_usd": m["net_cash"],
        "win_rate": m["win_rate"], "pf": m["profit_factor"], "mean_net_r": sum(returns) / len(returns) if returns else None,
        "native_equity_dd_usd": data["native_equity_max_drawdown"]["value"],
        "parts": data["observed_closed_row_metrics"]["count"],
        "fees_usd": data["cash_reconciliation"]["commission_cash"],
        "tp_campaign_counts": {f"TP{i}": sum(any(p["exit_comment"] == f"TP{i}" for p in c["exit_rows"])
                                               for c in complete) for i in range(1, 4)},
        "sl_before_tp1": sum(any(p["exit_comment"] == "SL" for p in c["exit_rows"])
                             and not any(p["exit_comment"] == "TP1" for p in c["exit_rows"])
                             for c in complete),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-logs", nargs="+", type=Path, required=True)
    parser.add_argument("--candidate-logs", nargs="+", type=Path)
    parser.add_argument("--source", type=Path, default=OUTPUT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = verify_source(args.source.read_bytes()) if args.source.is_file() else None
    if args.candidate_logs and source is None:
        parser.error("Candidate journals require the Premium Pine source")
    matrix = json.loads(MATRIX.read_bytes())
    if matrix["source_sha256"] != BASELINE_SHA:
        raise ValueError("Published matrix is for a different control source")
    baseline = read_runs(args.baseline_logs, BASELINE_SHA, pinned_matrix=matrix)
    candidates = read_runs(args.candidate_logs, source["source_sha256"]) if args.candidate_logs else {}
    comparisons = []
    for mode, tf in sorted(GRID, key=lambda k: (k[0], int(k[1]))):
        key = mode, tf
        diffs = comparison(baseline[key][0], candidates[key][0]) if key in candidates else None
        comparisons.append({"mode": mode, "timeframe": tf, "baseline": compact(baseline[key]),
                            "candidate": compact(candidates[key]) if key in candidates else None,
                            "differences": diffs})
    failed = any(row["differences"] for row in comparisons)
    result = {"schema": "qirvanta.premium.parity.v1", "baseline_sha256": BASELINE_SHA, "source_guard": source,
              "status": ("OBSERVER_MISMATCH" if failed else "MATCHED_OBSERVER_JOURNALS") if candidates else "ARCHIVE_REPRODUCED_PREMIUM_NATIVE_PENDING",
              "qualification": "Observer comparison, when provided, excludes rendering. Full source compile and chart-side native parity require separate TradingView evidence.",
              "comparisons": comparisons}
    with args.output.open("x", encoding="utf-8", newline="\n") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, default=json_default, allow_nan=False)
        handle.write("\n")
    print(result["status"])
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
