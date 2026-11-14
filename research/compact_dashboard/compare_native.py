"""Compare fresh Visual V2 and Compact dashboard Pine Logs at matched history depth."""

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
from hashlib import sha256
from itertools import zip_longest
import json
from pathlib import Path

from gold_pro import rc1_qa as rc1
from research.direct_trend_breakout import shadow_native
from research.direct_trend_visibility import native_qa as visibility
from research.luminous_visual_v2.compare_native import confirmed_history
from research.professional_signal_selector.native_qa import FRAMES, SHADOW_TAGS, log_messages, pressure_pair


CONTROL_COMMIT = "37b903f46a2d878d9d0757da134620c22f2d067d"
CONTROL_SHA = "77feedcbef84e77b9270ec43840be5b823d838300d0a09d5ddb4fec7acc089f4"
SEALED_START = 1794787200000
ROLES = ("baseline", "candidate")
EVENTS = ("RC1_CORE", "RC1_READY", "RC1_ALERT", "RC2_ALERT", "DTV_ALERT", "DTV_PAPER", "LZ_EVENT")


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def timestamp(text):
    value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if value.tzinfo != timezone.utc:
        raise argparse.ArgumentTypeError("the authorized QA window must use explicit UTC timestamps")
    return int(value.timestamp() * 1000)


def review_case(value):
    spec, separator, path = value.partition("=")
    frame, at, stamp = spec.partition("@")
    if not separator or not at or frame not in map(str, FRAMES) or not path:
        raise argparse.ArgumentTypeError("review case format: FRAME@UTC_CLOSE=DIRECTORY")
    return int(frame), timestamp(stamp), Path(path)


def difference(a, b):
    for index, (left, right) in enumerate(zip_longest(a, b), 1):
        if left != right:
            return {"ordinal": index, "baseline_message": left, "candidate_message": right}
    return None


def read_lane(path, frame, begin, end):
    rows = log_messages(path)
    prefix = confirmed_history(rows)
    shadow = shadow_native.read_native(path)
    last = shadow.end["time_close"]
    # The original DT_END captures loaded history before any subsequent realtime close.
    history = {row["time_close"]: row for row in shadow.bars if row["time_close"] <= last}
    run = rc1.read_export(path, frame, "indicator")
    rc1.require(shadow.timeframe == frame and shadow.first_index == 0 and len(history) >= 1199,
                f"{path.name}: missing expected loaded history or wrong symbol/timeframe")
    rc1.require(shadow.bars[0]["time"] == run.context[0].first_open <= begin <= end <= last < SEALED_START,
                f"{path.name}: unmatched historical origin or out-of-bounds QA window")
    expected = tuple(stamp for stamp in history if begin <= stamp <= end)
    rc1.require(run.meta["end_marker_present"] and expected and expected == tuple(item.closing for item in run.context),
                f"{path.name}: protected log window incomplete or missing RC1_END")
    for ctx in run.context:
        dt = history[ctx.closing]
        dt_ohlc = tuple(rc1.number(str(dt[key]), key) for key in shadow_native.OHLC)
        rc1.require((ctx.opening, ctx.ohlc) == (dt["time"], dt_ohlc),
                    f"{path.name}: RC1_CTX and confirmed DT_MARKET disagree at {ctx.closing}")
    journals = visibility.journal_rows(path)
    core_ready = {(event["confirmed_ms"], 1 if event["direction"] == "BUY" else -1) for event in run.ready}
    rc1.require(tuple(r["known_ms"] for r in journals["DTV_STATE"]) == expected,
                f"{path.name}: incomplete protected dashboard state")
    for item in journals["DTV_STATE"]:
        visibility.verify_permissions(item, item["known_ms"], core_ready)
    review = visibility.review_evidence(journals["DTV_REVIEW"], {r["known_ms"]: r for r in journals["DTV_STATE"]})
    paper = visibility.paper_evidence(journals, run.context, frame)
    rc1.require(paper is not None, f"{path.name}: missing RAW PAPER ledger")
    protected = tuple(line for line in rows if line.partition("|")[0] not in SHADOW_TAGS)
    return {
        "path": path, "prefix": prefix, "protected": protected, "run": run, "paper": paper,
        "last": last, "history_bars": len(history), "journals": journals, "review": review,
    }


def verify(directory, frame, begin, end, selected_close=None):
    paths = [directory / f"{role}-M{frame}.csv" for role in ROLES]
    if missing := [path.name for path in paths if not path.is_file()]:
        return {"status": "MISSING_EXPORTS", "files": missing}
    baseline, candidate = (read_lane(path, frame, begin, end) for path in paths)
    review_cases = {}
    for role, lane in zip(ROLES, (baseline, candidate)):
        found = lane["journals"]["DTV_REVIEW"]
        if selected_close is not None:
            rc1.require(len(found) == 1 and found[0]["known_ms"] == selected_close, f"{role}: wrong or missing selected DTV_REVIEW close")
        plans = {item["confirmed_ms"]: item for item in lane["run"].ready}
        review_cases[role] = [{
            "close_utc": shadow_native.utc(item["known_ms"]), "directional_observation": item["direction"],
            "s5_permission": item["permission"], "plan_visible": item["plan_visible"], "review": item["review"],
            "prices": {key: plans[item["known_ms"]][key] for key in ("entry", "sl", "tp1", "tp2", "tp3")}
            if item["plan_visible"] else None,
        } for item in found]
    streams = visibility.verify_pair(*paths, frame, "indicator")
    pressure = pressure_pair(*paths, frame, (baseline["run"], candidate["run"]))
    history_delta = difference(baseline["prefix"], candidate["prefix"])
    protected_delta = difference(baseline["protected"], candidate["protected"])
    observed = dict(Counter(line.partition("|")[0] for line in baseline["protected"]))
    paper = dict(Counter(row["event"] for row in baseline["paper"][1]))
    native = {
        "exports": {role: {"basename": path.name, "sha256": digest(path)} for role, path in zip(ROLES, paths)},
        "market_and_shadow_prefix_exact": history_delta is None,
        "protected_messages_exact": protected_delta is None,
        "prefix_first_open_utc": [shadow_native.utc(lane["run"].context[0].first_open) for lane in (baseline, candidate)],
        "prefix_end_utc": [shadow_native.utc(lane["last"]) for lane in (baseline, candidate)],
        "prefix_confirmed_bars": [lane["history_bars"] for lane in (baseline, candidate)],
        "chart_closes_audited": [len(lane["run"].context) for lane in (baseline, candidate)],
        "events": {key: observed.get(key, 0) for key in EVENTS},
        "raw_paper_event_types": paper,
        "raw_paper_final": baseline["paper"][0][-1],
        "execution_coverage": "SIM_ENTRY_EXERCISED" if paper.get("SIM_ENTRY") else "NO_SIMULATED_ENTRY_EXAMPLES",
        "zone_coverage": "LZ_EVENT_EXERCISED" if observed.get("LZ_EVENT") else "NO_ZONE_EVENT_EXAMPLES",
        "alert_coverage": "PAYLOADS_EXERCISED" if observed.get("RC2_ALERT") else "NO_TRADE_ALERT_EXAMPLES",
        "context_and_legacy_core_ready_alerts_and_paper": streams,
        "pressure": pressure,
        "review_snapshots": review_cases,
        "first_history_delta": history_delta,
        "first_protected_delta": protected_delta,
    }
    match = history_delta is None and protected_delta is None and streams["status"] == pressure["status"] == "MATCH"
    return {"status": "BOUNDED_EXACT_NATIVE_PARITY" if match else "MISMATCH", **native}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Fresh baseline-M*.csv and candidate-M*.csv")
    parser.add_argument("--frames", nargs="+", type=int, choices=FRAMES, default=FRAMES)
    parser.add_argument("--start", type=timestamp, default="2026-11-11T00:00:00Z")
    parser.add_argument("--end", type=timestamp, default="2026-11-11T17:30:00Z")
    parser.add_argument("--candidate", type=Path, required=True, help="Exact local source submitted to TradingView")
    parser.add_argument("--settings", type=Path, help="Operator-recorded native chart and Pine settings")
    parser.add_argument("--review-case", type=review_case, action="append", default=[],
                        help="Separate paired review captures, FRAME@UTC_CLOSE=DIRECTORY (repeatable)")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.start <= args.end < SEALED_START:
        parser.error("select a chronological pre-seal UTC window")
    frames = {}
    for frame in args.frames:
        try:
            frames[f"M{frame}"] = verify(args.directory, frame, args.start, args.end)
        except (OSError, ValueError, TypeError, KeyError, IndexError, csv.Error) as exc:
            frames[f"M{frame}"] = {"status": "INVALID_OR_INCOMPLETE_EXPORT", "reason": str(exc)}
    reviews = {}
    for frame, stamp, folder in args.review_case:
        key = f"M{frame} {shadow_native.utc(stamp)}"
        try:
            reviews[key] = verify(folder, frame, args.start, args.end, selected_close=stamp)
        except (OSError, ValueError, TypeError, KeyError, IndexError, csv.Error) as exc:
            reviews[key] = {"status": "INVALID_OR_INCOMPLETE_EXPORT", "reason": str(exc)}
    match = all(case["status"] == "BOUNDED_EXACT_NATIVE_PARITY" for case in (*frames.values(), *reviews.values()))
    evidence = {
        "status": "BOUNDED_EXACT_NATIVE_PARITY" if match else "INCOMPLETE_OR_MISMATCH",
        "baseline_commit": CONTROL_COMMIT,
        "baseline_sha256": CONTROL_SHA,
        "candidate_sha256_supplied_by_operator": digest(args.candidate),
        "settings_manifest_sha256": digest(args.settings) if args.settings else None,
        "window_inclusive_utc": [shadow_native.utc(args.start), shadow_native.utc(args.end)],
        "scope": "Native logs compare confirmed chart history, original source/payload/state and in-indicator simulated execution. UI and server delivery require separate evidence.",
        "frames": frames,
        "selected_historical_reviews": reviews,
    }
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, default=str, indent=2) + "\n", encoding="utf-8")
    return 0 if match else 1


if __name__ == "__main__":
    raise SystemExit(main())

