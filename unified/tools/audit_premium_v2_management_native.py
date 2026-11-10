"""Reconcile the four pre-registered native S5 M5 management runs."""

import argparse
from collections import Counter
from pathlib import Path

from audit_premium_parity import compact, comparison
from audit_tv_observations import ALIGNMENT_FIELDS
from build_sniper_premium_v2 import OUTPUT, sha, verify
from premium_native_pair import cutoff_epoch, read_observed, write_report

INPUTS = {
    "M00": ("v2_S5_M5.csv", False, "Буфер след TP1", "Само срок"),
    "M01": ("v2M01_S5_M5.csv", True, "Буфер след TP1", "Само срок"),
    "M02": ("v2M02_S5_M5.csv", True, "BE след TP1", "Само срок"),
    "M04": ("v2M04_S5_M5.csv", True, "Буфер след TP1", "Слаб моментум"),
}


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--logs-dir", required=True, type=Path)
    cli.add_argument("--cutoff", required=True)
    cli.add_argument("--output", required=True, type=Path)
    args = cli.parse_args()
    source = OUTPUT.read_bytes()
    verify(source)
    cutoff = cutoff_epoch(args.cutoff, "5")
    runs = {arm: read_observed([args.logs_dir / name], sha(source), cutoff, ("5",))[("S5", "5")]
            for arm, (name, *_settings) in INPUTS.items()}
    reference = runs["M00"][0]
    same_data = all(all(reference["run"][k] == data["run"][k] for k in ALIGNMENT_FIELDS)
                    for data, _ in runs.values())
    control_diff = comparison(reference, runs["M01"][0])
    rows = {}
    for arm, (data, digest) in runs.items():
        name, master, protection, exit_type = INPUTS[arm]
        parts = Counter(p["exit_comment"] for c in data["campaigns"] for p in c["exit_rows"])
        metrics = compact((data, digest))
        rows[arm] = {"file": name, "management_master": master, "protection": protection,
                     "additional_exit": exit_type, "native": metrics, "exit_part_counts": dict(sorted(parts.items())),
                     "net_cash_delta_from_M00": metrics["net_usd"] - compact(runs["M00"])["net_usd"],
                     "differences_from_M00": comparison(reference, data)}
    accepted = same_data and not control_diff
    write_report(args.output, {
        "status": "MATCHED_NATIVE_EXPLORATORY_CONTROLS" if accepted else "CONTROL_MISMATCH",
        "scope": "S5 M5, previously observed data. Inputs are corroborated by GUI captures, not encoded in Pine Logs. "
                 "No independent performance improvement test.",
        "cutoff_utc": args.cutoff, "common_market_data": same_data, "M00_M01_differences": control_diff,
        "runs": rows,
    })
    print(args.output, "MATCHED" if accepted else "MISMATCH")
    return int(not accepted)


if __name__ == "__main__":
    raise SystemExit(main())
