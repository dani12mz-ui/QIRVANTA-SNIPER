"""Audit native event parity between Precision and the final UX scripts."""

import argparse
from collections import Counter
import csv
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

from unified.tools.audit_gold_native import audit
from unified.tools.audit_precision_native import EXPECTED_FILLED_M5, RUNS, hash_file, normalized


ROOT = Path(__file__).resolve().parents[2]
BASELINE = ROOT / "docs/precision_evidence/native_parity.json"


def file_suffix(role, policy, frame):
    return f"{role}_{'FAST_' if policy == 'FAST' else ''}M{frame}.csv"


def matches_native(baseline_dir, final_dir):
    pinned = json.loads(BASELINE.read_bytes())
    if pinned["status"] != "VERIFIED":
        raise ValueError("Precision baseline requires its verified native report")
    expected = {(r["role"], r["policy"], r["frame"]): r["precision_sha256"] for r in pinned["pairs"]}
    results = []
    for role, policy, frame in RUNS:
        name = file_suffix(role, policy, frame)
        baseline = baseline_dir / f"precision_{name}"
        final = final_dir / f"final_{name}"
        if hash_file(baseline) != expected[role, policy, frame]:
            raise ValueError(f"Archived Precision source differs for {name}")
        before, counts, before_digest = normalized(baseline, frame, policy)
        after, after_counts, after_digest = normalized(final, frame, policy)
        native_before = audit(baseline, policy.lower(), frame)
        native_after = audit(final, policy.lower(), frame)
        fills = EXPECTED_FILLED_M5[policy] if role == "strategy" and frame == 5 else 0
        status = ("VERIFIED" if before == after
                  and native_before["matched_native_campaigns"] == native_after["matched_native_campaigns"]
                  and len(native_after["matched_native_campaigns"]) == fills
                  and not native_after["duplicate_alert_eligible_keys"] else "INCONCLUSIVE")
        mismatch = next((i for i, (a, b) in enumerate(zip(before, after)) if a != b), None)
        if mismatch is None and len(before) != len(after):
            mismatch = min(len(before), len(after))
        results.append({
            "role": role, "policy": policy, "frame": frame, "status": status,
            "counts": counts, "candidate_counts": after_counts, "first_difference_index": mismatch,
            "baseline_sha256": hash_file(baseline), "candidate_sha256": hash_file(final),
            "baseline_normalized_sha256": before_digest, "candidate_normalized_sha256": after_digest,
            "campaigns": native_after["matched_native_campaigns"],
        })
    return results


def extended_prefix(short_file, extended_file):
    """Compare the same confirmed range after exporting a longer recalculation."""
    short, counts, _ = normalized(short_file, 5, "CONFIRMED")
    first, cutoff = short[0][0] + 300, max(x[0] for x in short) + 300
    earliest = latest = None
    extra_context = 0
    prefix = []
    with extended_file.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            message = row["Message"]
            stamp = int(datetime.fromisoformat(row["Date"]).timestamp())
            if message.startswith("GOLD_CTX|1|"):
                kind = "context"
                fields = message.split("|")
                known = int(fields[6]) // 1000
                if fields[2:4] != ["OANDA:XAUUSD", "5"]:
                    raise ValueError("Unexpected symbol or frame in extended context")
                if latest is not None and known <= latest:
                    raise ValueError("Nonmonotonic or duplicated extended context")
                earliest = known if earliest is None else earliest
                latest = known
                extra_context += known > cutoff
            elif message.startswith("GOLD_PLAN|"):
                kind = "plan"
                fields = message.split("|")
                if fields[3] != "CONFIRMED":
                    raise ValueError("Unexpected policy in extended plan")
                known = int(fields[2]) // 1000
            elif message.startswith('{"schema":"qirvanta.goldpro.event.v1"'):
                kind = "event"
                event = json.loads(message)
                if event["symbol"] != "OANDA:XAUUSD" or event["timeframe"] != "5":
                    raise ValueError("Unexpected symbol or frame in extended event")
                known = event["known_at"] // 1000
                message = json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            else:
                continue
            if known != stamp + 300 or known < first:
                raise ValueError("Extended export has an invalid availability time")
            if known <= cutoff:
                prefix.append((stamp, kind, message))
    passed = prefix == short and earliest == first and extra_context > 0
    return {
        "status": "VERIFIED" if passed else "INCONCLUSIVE",
        "scope": "Native confirmed-prefix parity after a separate recalculation with a later export bound. Not a tick-by-tick replay.",
        "fixed_window_counts": counts, "prefix_equal": prefix == short,
        "first_close_utc_seconds": earliest, "prefix_last_close_utc_seconds": cutoff,
        "extended_last_close_utc_seconds": latest, "additional_confirmed_bars": extra_context,
        "short_sha256": hash_file(short_file), "extended_sha256": hash_file(extended_file),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-dir", required=True, type=Path)
    parser.add_argument("--final-dir", required=True, type=Path)
    parser.add_argument("--extended-indicator", type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    pairs = matches_native(args.baseline_dir, args.final_dir)
    counts = Counter()
    for item in pairs:
        counts.update(item["counts"])
    prefix = (extended_prefix(args.final_dir / "final_indicator_M5.csv", args.extended_indicator)
              if args.extended_indicator else {"status": "INCONCLUSIVE", "reason": "No extended native export"})
    passed = all(row["status"] == "VERIFIED" for row in pairs)
    report = {"status": "VERIFIED" if passed else "INCONCLUSIVE",
              "window_utc": ["2026-11-10T00:00:00Z", "2026-11-11T06:00:00Z"],
              "baseline_report_sha256": sha256(BASELINE.read_bytes()).hexdigest(),
              "counts": dict(counts), "pairs": pairs, "confirmed_prefix": prefix,
              "alert_scope": "Native payload-generating events. No alert delivery test."}
    with args.out.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    print(report["status"], len(pairs), "native pairs", prefix["status"], "confirmed prefix")
    return int(not passed or prefix["status"] != "VERIFIED")


if __name__ == "__main__":
    raise SystemExit(main())
