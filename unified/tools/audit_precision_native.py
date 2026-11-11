"""Check that Precision rendering preserves native context, decisions and fills."""

import argparse
from collections import Counter
import csv
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path

from unified.tools.audit_gold_native import audit

RUNS = (
    ("indicator", "CONFIRMED", 5), ("indicator", "CONFIRMED", 15), ("indicator", "CONFIRMED", 30),
    ("strategy", "CONFIRMED", 5), ("strategy", "CONFIRMED", 15), ("strategy", "CONFIRMED", 30),
    ("strategy", "FAST", 5),
)
EXPECTED_CONTEXT = {5: 349, 15: 117, 30: 59}
EXPECTED_FILLED_M5 = {"CONFIRMED": 2, "FAST": 1}


def hash_file(path):
    return sha256(path.read_bytes()).hexdigest()


def normalized(path, frame, policy):
    rows = []
    counts = Counter()
    start = datetime.fromisoformat("2026-11-10T00:00:00+00:00").timestamp()
    end = datetime.fromisoformat("2026-11-11T06:00:00+00:00").timestamp()
    context_closes = []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            text = row["Message"]
            stamp = datetime.fromisoformat(row["Date"]).timestamp()
            if text.startswith("GOLD_CTX|1|"):
                kind = "context"
                fields = text.split("|")
                if fields[2:4] != ["OANDA:XAUUSD", str(frame)]:
                    raise ValueError(f"Wrong symbol or frame in {path.name}")
                known = int(fields[6]) // 1000
                context_closes.append(known)
            elif text.startswith("GOLD_PLAN|"):
                kind = "plan"
                fields = text.split("|")
                if fields[3] != policy:
                    raise ValueError(f"Unexpected plan policy in {path.name}")
                known = int(fields[2]) // 1000
            elif text.startswith('{"schema":"qirvanta.goldpro.event.v1"'):
                kind = "event"
                event = json.loads(text)
                if event["symbol"] != "OANDA:XAUUSD" or event["timeframe"] != str(frame):
                    raise ValueError(f"Wrong event symbol or frame in {path.name}")
                known = event["known_at"] // 1000
                text = json.dumps(event, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            else:
                continue
            if not start <= known <= end or int(stamp) + frame * 60 != known:
                raise ValueError(f"Wrong closed-bar window in {path.name}")
            counts[kind] += 1
            rows.append((int(stamp), kind, text))
    if not all(counts[k] for k in ("context", "plan", "event")):
        raise ValueError(f"Incomplete native context and events in {path.name}")
    if (len(context_closes) != EXPECTED_CONTEXT[frame] or sorted(set(context_closes)) != context_closes
            or (context_closes[0], context_closes[-1]) != (start, end)):
        raise ValueError(f"Incomplete or duplicated closed-bar context coverage in {path.name}")
    payload = json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode()
    return rows, dict(counts), sha256(payload).hexdigest()


def compare(directory):
    results = []
    for role, policy, frame in RUNS:
        suffix = f"{role}_{'FAST_' if policy == 'FAST' else ''}M{frame}.csv"
        base, candidate = (directory / f"{name}_{suffix}" for name in ("original", "precision"))
        original, counts, digest = normalized(base, frame, policy)
        changed, new_counts, new_digest = normalized(candidate, frame, policy)
        native_original = audit(base, policy.lower(), frame)
        native_new = audit(candidate, policy.lower(), frame)
        expected_fills = EXPECTED_FILLED_M5[policy] if role == "strategy" and frame == 5 else 0
        equal = (original == changed and native_original["matched_native_campaigns"] == native_new["matched_native_campaigns"]
                 and len(native_new["matched_native_campaigns"]) == expected_fills
                 and not native_new["duplicate_alert_eligible_keys"])
        first_difference = next((i for i, (a, b) in enumerate(zip(original, changed)) if a != b), None)
        if first_difference is None and len(original) != len(changed):
            first_difference = min(len(original), len(changed))
        results.append({"role": role, "frame": frame, "policy": policy, "status": "VERIFIED" if equal else "INCONCLUSIVE",
                        "original_sha256": hash_file(base), "precision_sha256": hash_file(candidate),
                        "original_normalized_sha256": digest, "precision_normalized_sha256": new_digest,
                        "counts": counts, "precision_counts": new_counts, "first_difference_index": first_difference,
                        "lifecycle_counts": native_new["states"],
                        "matched_native_campaigns": native_new["matched_native_campaigns"]})
    passed = all(row["status"] == "VERIFIED" for row in results)
    return {"status": "VERIFIED" if passed else "INCONCLUSIVE",
            "scope": "Unchanged original GOLD PRO and Precision on same standard OANDA:XAUUSD chart bars.",
            "window": ["2026-11-10T00:00:00Z", "2026-11-11T06:00:00Z"],
            "alert_scope": "Payload-generating native events, no alert delivery test.",
            "pairs": results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    report = compare(args.logs_dir)
    with args.out.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, indent=2, sort_keys=True, allow_nan=False)
        stream.write("\n")
    print(report["status"], len(report["pairs"]), "native pairs")
    return int(report["status"] != "VERIFIED")


if __name__ == "__main__":
    raise SystemExit(main())
