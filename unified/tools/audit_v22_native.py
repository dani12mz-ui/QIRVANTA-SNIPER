"""Audit V2.2 native campaigns, next-tick execution and matched policy runs."""
import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path
from statistics import mean

from diagnostics.accounting.audit import audit, read_jsonl
from unified.tools.audit_premium_parity import compact, comparison
from unified.tools.audit_tv_observations import ALIGNMENT_FIELDS
from unified.tools.premium_native_pair import write_report

CASE_START = 1794247200000  # 2026-11-09 18:00 UTC
CASE_END = 1794322800000    # 2026-11-10 15:00 UTC
FILL_STATES = {"FILLED", "TP1_FILLED", "TP2_FILLED", "TP3_FILLED", "STOP_FILLED"}


def read_native(path):
    records, _ = read_jsonl(path, allow_pine_prefix=True, origin_override="REAL_TRADINGVIEW")
    value = audit(records)
    run = value["run"]
    if (value["status"] != "PASS_OBSERVED_JOURNAL" or not value["cash_reconciliation"]["matches"]
            or run["symbol"] != "OANDA:XAUUSD" or run["cutoff"] != CASE_END):
        raise ValueError(f"Incomplete or unexpected native export: {path}")
    mode, models, policy, frame = run["run_id"].split("|")
    if mode != "v22" or frame not in ("5", "15", "30"):
        raise ValueError(f"Unexpected run ID: {path}")
    campaigns = sorted(value["campaigns"], key=lambda c: (c["entry_bar_index"], c["entry_id"]))
    seen = set()
    last_exit = None
    complete = []
    for c in campaigns:
        metadata = c["submitted_metadata"]
        if c["entry_id"] in seen or not metadata or c["entry_bar_index"] != metadata["submitted_bar"] + 1:
            raise ValueError(f"Duplicate, unregistered or non-next-bar fill: {c['entry_id']}")
        if c["entry_time"] < metadata["submitted_time"] or (last_exit is not None and c["entry_time"] < last_exit):
            raise ValueError(f"Overlapping campaigns or early fill: {c['entry_id']}")
        if c["submitted_quantity"] != 3:
            raise ValueError(f"Unexpected campaign quantity: {c['entry_id']}")
        if last_exit is None and seen:
            raise ValueError(f"A campaign followed a still-open campaign: {c['entry_id']}")
        seen.add(c["entry_id"])
        if c["state"] == "COMPLETE":
            complete.append(c)
            last_exit = c["last_exit_time"]
        else:
            last_exit = None
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    case = [c for c in campaigns if CASE_START <= c["submitted_metadata"]["submitted_time"] <= CASE_END]
    summary = compact((value, digest))
    summary.update({
        "id": run["run_id"], "models": models, "policy": policy, "frame": frame,
        "source_template_sha256": run["source_sha256"],
        "next_bar_fills": len(campaigns), "mean_holding_bars": mean(c["holding_bars"] for c in complete) if complete else None,
        "close_reasons": dict(Counter(row["exit_comment"] for c in campaigns for row in c["exit_rows"])),
        "by_family": value["complete_campaign_groups"]["family"],
        "case_campaigns": case,
    })
    return value, summary


def lifecycle(path):
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as source:
        for r in csv.DictReader(source):
            msg = r.get("Message", "")
            if not msg.startswith("{"):
                continue
            row = json.loads(msg)
            if row.get("schema") == "qirvanta.v22.lifecycle.v1" and CASE_START <= row["known_at"] <= CASE_END:
                if row["state"] not in FILL_STATES:
                    row["price"] = None
                rows.append(row)
    if not rows or len({r["run_id"] for r in rows}) != 1:
        raise ValueError(f"No valid lifecycle journal: {path}")
    if any(r["effective_at"] is not None and r["state"] in {"WATCH", "CONFIRMED"}
           and r["effective_at"] != r["known_at"] for r in rows):
        raise ValueError(f"Unexpected retrospective observation: {path}")
    first = {}
    for row in sorted(rows, key=lambda r: r["known_at"]):
        key = f"{row['model']}|{row['side']}|{row['state']}"
        first.setdefault(key, row)
    return {
        "id": rows[0]["run_id"], "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "counts": dict(Counter(r["state"] for r in rows)),
        "rejections": dict(Counter(r["reason"] for r in rows if r["state"] == "REJECTED")),
        "first_by_model_side_and_state": first,
        "rows": rows,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--journal", nargs="+", type=Path, required=True)
    p.add_argument("--lifecycle", nargs="*", type=Path, default=[])
    p.add_argument("--pre-display", nargs="*", type=Path, default=[])
    p.add_argument("--out", type=Path, required=True)
    args = p.parse_args()
    runs = dict()
    for path in args.journal:
        raw, summary = read_native(path)
        if summary["id"] in runs:
            raise ValueError(f"Duplicate matrix cell: {summary['id']}")
        runs[summary["id"]] = raw, summary
    fixed_m5 = runs.get("v22|ABC|A|5")
    alignment = {}
    if fixed_m5:
        control = fixed_m5[0]["run"]
        alignment = {k: [f for f in ALIGNMENT_FIELDS if v[0]["run"][f] != control[f]]
                     for k, v in runs.items() if v[1]["frame"] == "5"}
    visual = []
    for path in args.pre_display:
        earlier, info = read_native(path)
        if info["id"] not in runs:
            raise ValueError(f"No corresponding run after the display change: {path}")
        current, summary = runs[info["id"]]
        differences = comparison(earlier, current)
        visual.append({
            "id": info["id"],
            "status": "VERIFIED" if not differences else "INCONCLUSIVE",
            "prior_source_template_sha256": info["source_template_sha256"],
            "prior_log_sha256": info["log_sha256"],
            "current_source_template_sha256": summary["source_template_sha256"],
            "current_log_sha256": summary["log_sha256"],
            "differences": differences,
        })
    write_report(args.out, {
        "status": "VERIFIED_NATIVE_EXPORTS",
        "scope": "TradingView broker emulator. Native drawdown uses its intrabar path. Synthetic cost stress is reported separately.",
        "cutoff_ms": CASE_END,
        "runs": {k: v[1] for k, v in sorted(runs.items())},
        "m5_alignment_differences": alignment,
        "visual_execution_comparison": visual,
        "lifecycle": [lifecycle(path) for path in args.lifecycle],
    })
    print(f"Audited {len(runs)} native run(s)")


if __name__ == "__main__":
    main()
