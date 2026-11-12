"""Compare fresh TradingView Visual V2 and 26ad1aa Pine Logs without dropping zone events."""

import argparse
from collections import Counter
import csv
from hashlib import sha256
import json
from pathlib import Path

from research.professional_signal_selector.native_qa import FRAMES, SHADOW_TAGS, frame_checks, log_messages


CONTROL = "26ad1aa2992f9e2e7e28c13fbbd3f321e1de8261"
CONTROL_PINE_SHA256 = "023c9eaf8a0e0085fd187b4bf9b5f76eb48017dd187c5e5b04cb8e542ff852ce"


def tagged(messages, tag):
    return tuple(line for line in messages if line.partition("|")[0] == tag)


def confirmed_history(messages):
    stream = tuple(line for line in messages if line.partition("|")[0] in SHADOW_TAGS)
    markers = [index for index, line in enumerate(stream) if line.startswith("DT_END|")]
    if len(markers) != 1:
        raise ValueError("Expected one DT_END marker in the fresh native journal")
    return stream[:markers[0] + 1]


def verify_frame(directory, frame):
    result = frame_checks(directory, frame)
    if result["status"] == "MISSING_EVIDENCE":
        return result
    files = [directory / f"{role}-M{frame}.csv" for role in ("baseline", "candidate")]
    logs = [log_messages(path) for path in files]
    histories = [confirmed_history(messages) for messages in logs]
    zone_events = [tagged(messages, "LZ_EVENT") for messages in logs]
    history_match = histories[0] == histories[1]
    result["confirmed_historical_ohlc_and_shadow_exact"] = history_match
    result["luminous_zone_events"] = {
        "status": "MATCH" if zone_events[0] and zone_events[0] == zone_events[1]
        else "MISMATCH" if zone_events[0] != zone_events[1] else "NO_EVENT_EXAMPLES",
        "counts": [len(rows) for rows in zone_events],
        "baseline_by_state": dict(Counter(line.split("|")[6] for line in zone_events[0])),
    }
    review_files = [directory / f"{role}-review-M{frame}.csv" for role in ("baseline", "candidate")]
    supplied = [path.is_file() for path in review_files]
    if all(supplied):
        review = [log_messages(path) for path in review_files]
        review_rows = [tagged(messages, "DTV_REVIEW") for messages in review]
        review_match = bool(review_rows[0]) and review_rows[0] == review_rows[1]
        result["paired_historical_review"] = {
            "status": "MATCH" if review_match else "MISMATCH",
            "counts": [len(rows) for rows in review_rows],
            "raw_sha256": [sha256(path.read_bytes()).hexdigest() for path in review_files],
        }
    else:
        result["paired_historical_review"] = {"status": "MISSING_PAIR" if any(supplied) else "NOT_SUPPLIED"}
    if result["status"] == "MATCH":
        if not history_match:
            result["status"] = "NONIDENTICAL_CONTEXT"
        elif result["luminous_zone_events"]["status"] != "MATCH":
            result["status"] = result["luminous_zone_events"]["status"]
        elif result["paired_historical_review"]["status"] in {"MISMATCH", "MISSING_PAIR"}:
            result["status"] = result["paired_historical_review"]["status"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Fresh baseline-M*.csv and candidate-M*.csv")
    parser.add_argument("--frames", nargs="+", choices=FRAMES, default=FRAMES, type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    lanes = {}
    for frame in args.frames:
        try:
            lanes[f"M{frame}"] = verify_frame(args.directory, frame)
        except (OSError, ValueError, KeyError, IndexError, csv.Error) as exc:
            lanes[f"M{frame}"] = {"status": "INVALID_EXPORT", "reason": str(exc)}
    match = all(lane["status"] == "MATCH" for lane in lanes.values())
    payload = {
        "status": "MATCH" if match else "INCOMPLETE_OR_MISMATCH",
        "baseline_commit": CONTROL,
        "baseline_pine_sha256": CONTROL_PINE_SHA256,
        "alert_scope": "Native payload and intent parity",
        "frames": lanes,
    }
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
                           encoding="utf-8", newline="\n")
    return 0 if match else 1


if __name__ == "__main__":
    raise SystemExit(main())
