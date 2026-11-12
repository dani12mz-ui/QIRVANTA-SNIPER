"""Verify the selector's display-only source and paired TradingView Pine Logs."""

import argparse
from collections import Counter, defaultdict
from dataclasses import replace
import csv
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from gold_pro import pressure_qa, rc1_qa, rc2_qa
from research.direct_trend_breakout import shadow_native
from research.direct_trend_visibility import native_qa as visibility_qa


HERE = Path(__file__).resolve().parent
BASELINE = ROOT / "research/signal_quality_reliability/QIRVANTA_GOLD_PRO_SIGNAL_QUALITY.pine"
CANDIDATE = HERE / "QIRVANTA_GOLD_PRO_PROFESSIONAL_SELECTOR.pine"
FORWARD_SEAL = ROOT / "docs/gold_pro_evidence/forward_seal_m5.json"
PROTOCOL = HERE / "PROTOCOL.json"
BASELINE_COMMIT = "192986d5fd5dddbaead0f7a45a8075b9137082f7"
BASELINE_SHA = "a0ec3f26bd1bf3a6cc7385184501b1ecd6b4aedc6a3a4fe5e52c70201db2c26f"
FORWARD_SHA = "09181ecaf9e30cde62c5203b524cb458d808a07c24029e961e32bb25c474afd1"
FRAMES = (5, 15, 30)
MIN_SHADOW_CLOSED_BARS = 1199  # calc_bars_count=1200 may include the open realtime bar.
SHADOW_TAGS = {"DT_MARKET", "DT_SHADOW", "DT_END"}
VOLUME_ONLY_MATCH = "MATCH_WITH_UNUSED_VOLUME_VARIANCE"
ACCEPTED = {"MATCH", VOLUME_ONLY_MATCH}
SOURCE_EDITS = (
    (
        'bool rcDisplayWickMarks = input.bool(false, "Маркери за Wick наблюдения", group=RC_DISPLAY, display=display.none)\n',
        'bool rcDisplayWickMarks = input.bool(false, "Маркери за Wick наблюдения", group=RC_DISPLAY, display=display.none)\n'
        'bool psDisplay = input.bool(false, "Professional Selector · RESEARCH", group=RC_DISPLAY, display=display.none)\n',
    ),
    (
        '    string directionMeta = dtShow ? "S5 · DT SHADOW" : "S5"\n',
        '    string selectorState = not hasDirection or str.contains(lead.reason, "RESEARCH REJECTED:") ? "REJECT / EXPIRED" :\n'
        '         confirmedSell and confirmedBuy ? "CONFLICT" :\n'
        '         lead.rank < 2 or dvConflict ? "OBSERVE" : "NO DATA"\n'
        '    string selectorCaption = "QUALITY / WHY · " + selectorState\n'
        '    string directionMeta = dtShow ? "S5 · DT SHADOW" : "S5"\n',
    ),
    (
        '        directionMeta += "\\n" + quality\n',
        '        directionMeta += "\\n" + (psDisplay ? selectorCaption : quality)\n',
    ),
    (
        '    color directionInk = dvConflict ? rcViewGold : dvHasSell ? rcViewSell : dvHasBuy ? rcViewBuy : rcViewMuted\n',
        '    if psDisplay and (hasDirection or sqTerminalVisible)\n'
        '        if dvConflict or not hasDirection\n'
        '            directionMeta += "\\n" + selectorCaption\n'
        '        directionTip += "\\n\\n" + selectorCaption + "\\nRESEARCH · Frozen evidence may be unavailable."\n'
        '    color directionInk = dvConflict ? rcViewGold : dvHasSell ? rcViewSell : dvHasBuy ? rcViewBuy : rcViewMuted\n',
    ),
    (
        '    string permissionMeta = (rcReview and permissionReady ? "HISTORICAL · " : "") + dvPermissionSource +\n',
        '    string permissionMeta = (rcReview and permissionReady ? "HISTORICAL · " : "") +\n'
        '         (psDisplay and permissionReady and rcViewHasPlan ? "S5 APPROVED" : dvPermissionSource) +\n',
    ),
)


def digest(data):
    return sha256(data).hexdigest()


def pinned(path, expected):
    data = path.read_bytes()
    rc1_qa.require(digest(data) == expected, f"Unexpected SHA-256 for {path.name}")
    return data


def source_checks():
    baseline = pinned(BASELINE, BASELINE_SHA)
    candidate = CANDIDATE.read_bytes()
    protocol = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    rc1_qa.require(
        (protocol["base_commit"], protocol["baseline_pine_sha256"], protocol["sealed_forward_sha256"])
        == (BASELINE_COMMIT, BASELINE_SHA, FORWARD_SHA),
        "Frozen protocol does not identify the pinned baseline and seal",
    )
    pinned(FORWARD_SEAL, FORWARD_SHA)
    pinned(visibility_qa.AUDIT, visibility_qa.AUDIT_SHA256)

    expected = baseline.decode("utf-8")
    for before, after in SOURCE_EDITS:
        rc1_qa.require(expected.count(before) == 1, f"Non-unique source anchor: {before!r}")
        expected = expected.replace(before, after, 1)
    rc1_qa.require(candidate == expected.encode("utf-8"), "Candidate differs beyond the permitted display edits")
    return {
        "status": "MATCH",
        "base_commit": BASELINE_COMMIT,
        "baseline_sha256": BASELINE_SHA,
        "candidate_sha256": digest(candidate),
        "protocol_sha256": digest(PROTOCOL.read_bytes()),
        "forward_seal_sha256": FORWARD_SHA,
        "protected_rc2_audit_sha256": visibility_qa.AUDIT_SHA256,
        "selector_default": False,
        "display_edits": ["one opt-in input", "directional triage caption and tooltip", "existing READY source caption"],
    }


def native_window(frame):
    window = json.loads(PROTOCOL.read_text(encoding="utf-8"))["data"]["research_windows_utc"]["native_qa"]
    begin = int(datetime.fromisoformat(window["close_first"]).timestamp() * 1000)
    end = int(datetime.fromisoformat(window["close_last"]).timestamp() * 1000)
    return tuple(range(begin, end + 1, frame * 60_000))


def log_messages(path):
    """Keep message order and exact payloads, independently of CSV timestamps."""
    with path.open(encoding="utf-8-sig", newline="") as source:
        rows = csv.DictReader(source, strict=True)
        rc1_qa.require(rows.fieldnames == ["Date", "Message"], f"{path.name}: invalid Pine Logs header")
        return tuple(line for row in rows for line in row["Message"].splitlines() if line)


def normalized_logs(left, right, volume_differences=()):
    a, b = (tuple(line for line in log_messages(path) if line.partition("|")[0] not in SHADOW_TAGS)
            for path in (left, right))
    by_tag = defaultdict(lambda: [[], []])
    for side, rows in enumerate((a, b)):
        for line in rows:
            by_tag[line.partition("|")[0]][side].append(line)
    status = "MATCH" if a == b else "MISMATCH"
    if status == "MISMATCH" and volume_differences:
        closes = {row["close_ms"] for row in volume_differences}

        def allowed_normalization(rows):
            for line in rows:
                tag, _, payload = line.partition("|")
                if tag == "PR_CTX":
                    item = rc1_qa._json_dict(payload)
                    if item.get("close_ms") in closes:
                        yield tag, {key: value for key, value in item.items() if key != "volume"}
                        continue
                yield line

        if tuple(allowed_normalization(a)) == tuple(allowed_normalization(b)):
            status = VOLUME_ONLY_MATCH
    return {
        "status": status,
        "messages": [len(a), len(b)],
        "streams": {tag: {"baseline": len(x), "candidate": len(y), "exact": x == y}
                    for tag, (x, y) in sorted(by_tag.items())},
        "normalized_fields": ["PR_CTX.volume at independently verified OHLC-only closes"]
        if status == VOLUME_ONLY_MATCH else [],
    }


def shadow_pair(left, right, frame, expected_closes, indicator_runs):
    a, b = (shadow_native.read_native(path) for path in (left, right))
    rc1_qa.require(a.timeframe == b.timeframe == frame, "Unexpected Shadow timeframe")
    comparison = shadow_native.compare_repeat(a, b)
    common = comparison.get("common_bars", 0)
    shadow_by_close = [{row["time_close"]: row for row in run.bars} for run in (a, b)]
    covered = all(set(expected_closes) <= bars.keys() for bars in shadow_by_close)
    matched_origins = all(shadow.bars[0]["time"] == indicator.context[0].first_open
                          for shadow, indicator in zip((a, b), indicator_runs))
    cross_checked = []
    for shadow, indicator in zip(shadow_by_close, indicator_runs):
        common_context = [bar for bar in indicator.context if bar.closing in shadow]
        for bar in common_context:
            witness = shadow[bar.closing]
            ohlc = tuple(Decimal(str(witness[key])) for key in shadow_native.OHLC)
            rc1_qa.require((witness["time"], ohlc) == (bar.opening, bar.ohlc),
                           "Shadow OHLC differs from the native indicator context")
        cross_checked.append(len(common_context))
    # The end marker records the original loaded history cutoff even if a later
    # realtime close was logged before the user exported it.
    end_counts = [run.end["index"] - run.first_index + 1 if run.end else 0 for run in (a, b)]
    full = (
        common >= MIN_SHADOW_CLOSED_BARS and min(end_counts) >= MIN_SHADOW_CLOSED_BARS
        and covered and matched_origins
    )
    status = comparison["status"]
    if status == "MATCH" and not full:
        status = "MATCHED_SUBSET"
    return {
        **comparison,
        "status": status,
        "minimum_confirmed_bars_for_1200_bar_run": MIN_SHADOW_CLOSED_BARS,
        "target_closes_covered": covered,
        "warmup_origin_matches_indicator": matched_origins,
        "indicator_ohlc_cross_checked_closes": cross_checked,
        "first_open_ms": [run.bars[0]["time"] for run in (a, b)],
        "last_close_ms": [run.bars[-1]["time_close"] for run in (a, b)],
        "end_markers": [run.end for run in (a, b)],
        "confirmed_bars_at_end_marker": end_counts,
        "closed_bars": [len(a.bars), len(b.bars)],
        "events_total": [len(a.events), len(b.events)],
        "repeated_validated_batches": [len(a.repeated_batches), len(b.repeated_batches)],
        "scope": "Exact OHLC and events on the common loaded prefix. No claim about a non-overlapping tail.",
    }


def unused_volume_differences(original, repeat):
    if not (original.complete and repeat.complete and original.rows and original.rows == repeat.rows):
        return []
    if any(row["weighted"] is not False or row["mode"] in {"TICK", "FALLBACK"} for row in original.rows):
        return []
    if tuple(replace(bar, volume=None) for bar in original.bars) != tuple(
        replace(bar, volume=None) for bar in repeat.bars
    ):
        return []
    return [
        {"close_ms": left.closing, "close_utc": shadow_native.utc(left.closing),
         "baseline": left.volume, "candidate": right.volume, "volume_type": left.volume_type}
        for left, right in zip(original.bars, repeat.bars) if left.volume != right.volume
    ]


def pressure_pair(left, right, frame, runs):
    parsed = [pressure_qa.pressure_source(path, frame) for path in (left, right)]
    if any(problem for _, problem in parsed):
        problems = [problem for _, problem in parsed if problem]
        status = "MISMATCH" if any(item["status"] == "INVALID_EXPORT" for item in problems) else "MISSING_EVIDENCE"
        return {"status": status, "problems": problems}
    replay = []
    sources = [run for run, _ in parsed]
    for source, run in zip(sources, runs):
        settings = {(row["comparison"], row["weighted"]) for row in source.rows}
        if not settings:
            return {"status": "MISSING_EVIDENCE", "reason": f"{source.path.name} lacks Pressure samples"}
        rc1_qa.require(len(settings) == 1, "Pressure settings changed inside the export")
        comparison, weighted = next(iter(settings))
        replay.append(pressure_qa.pressure_checks(
            source, {}, expected_comparison=comparison, expected_weighted=weighted, rc1_context=run.context,
        ))
    repeated = pressure_qa.pressure_repeat(*sources)
    coverage = all(tuple(bar.closing for bar in source.bars) == tuple(bar.closing for bar in run.context)
                   for source, run in zip(sources, runs))
    status = repeated["status"]
    volume_differences = unused_volume_differences(*sources) if coverage else []
    if status == "NONIDENTICAL_CONTEXT" and volume_differences:
        status = VOLUME_ONLY_MATCH
    elif status == "MATCH" and not coverage:
        status = "MATCHED_SUBSET"
    return {
        **repeated,
        "status": status,
        "all_indicator_closes_covered": coverage,
        "sample_rows_exact": sources[0].rows == sources[1].rows,
        "samples": len(sources[0].rows),
        "unused_volume_differences": volume_differences,
        "tick_volume_weighted_parity": (
            "NOT_EXERCISED" if all(not row["weighted"] for row in sources[0].rows)
            else "MATCH" if status == "MATCH" else "UNVERIFIED"
        ),
        "independent_checks": replay,
    }


def frame_checks(directory, frame):
    left = directory / f"baseline-M{frame}.csv"
    right = directory / f"candidate-M{frame}.csv"
    if missing := [path.name for path in (left, right) if not path.is_file()]:
        return {"status": "MISSING_EVIDENCE", "files": missing}

    paths = (left, right)
    runs = [rc1_qa.read_export(path, frame, "indicator") for path in paths]
    expected = native_window(frame)
    full_window = all(tuple(bar.closing for bar in run.context) == expected for run in runs)
    matched_warmup = runs[0].context == runs[1].context
    pressure = pressure_pair(left, right, frame, runs)
    result = {
        "baseline": {"file": left.name, "sha256": digest(left.read_bytes())},
        "candidate": {"file": right.name, "sha256": digest(right.read_bytes())},
        "raw_csv_byte_identical": left.read_bytes() == right.read_bytes(),
        "expected_closes": len(expected),
        "closed_bars": [len(run.context) for run in runs],
        "exact_frozen_window": full_window,
        "indicator_context_and_warmup_match": matched_warmup,
        "indicator": visibility_qa.verify_pair(left, right, frame, "indicator"),
        "shadow_prefix": shadow_pair(left, right, frame, expected, runs),
        "normalized_logs": normalized_logs(left, right, pressure.get("unused_volume_differences", ())),
        "pressure": pressure,
    }
    visibility = {}
    for role, path, run in zip(("baseline", "candidate"), paths, runs):
        review = directory / f"{role}-review-M{frame}.csv"
        checks = visibility_qa.verify_visibility(path, frame, review)
        states = visibility_qa.journal_rows(path)["DTV_STATE"]
        journal = rc2_qa.extras(path)
        batches = journal["RC2_ALERT"]
        events = list(rc2_qa.batches(batches, rc2_qa.INDICATOR_SCHEMA, "confirmed_ms"))
        ready = {(event["confirmed_ms"], event["direction"]): event for event in run.ready}
        approved = []
        for row in states:
            if row["permission"] not in visibility_qa.READY_SIDES:
                continue
            direction, _ = row["permission"].split()
            plan = ready.get((row["known_ms"], direction))
            rc1_qa.require(row["plan_visible"] and plan is not None, "Approved state lacks visible native READY geometry")
            approved.append({key: plan[key] for key in
                             ("event_id", "direction", "confirmation_utc", "entry", "sl", "tp1", "tp2", "tp3")})
        visibility[role] = {
            **checks,
            "permissions": dict(Counter(row["permission"] for row in states)),
            "approved_s5_plans": approved,
            "rc2_alert_batches": len(batches),
            "rc2_unique_alert_ids": len(events),
        }
    result["visibility"] = visibility
    states = [result[name]["status"] for name in ("indicator", "shadow_prefix", "normalized_logs", "pressure")]
    if matched_warmup and "MISMATCH" in states:
        status = "MISMATCH"
    elif matched_warmup and full_window and all(state in ACCEPTED for state in states) and all(
        value["status"] == "VERIFIED" for value in visibility.values()
    ):
        status = VOLUME_ONLY_MATCH if VOLUME_ONLY_MATCH in states else "MATCH"
    else:
        status = "PARTIALLY_VERIFIED" if matched_warmup else "NONIDENTICAL_CONTEXT"
    return {"status": status, **result}


def native_report(directory):
    frames = {}
    for frame in FRAMES:
        try:
            frames[f"M{frame}"] = frame_checks(directory, frame)
        except (OSError, ValueError, KeyError, csv.Error) as exc:
            frames[f"M{frame}"] = {"status": "INVALID_EXPORT", "reason": str(exc)}
    statuses = [lane["status"] for lane in frames.values()]
    if any(status in {"MISMATCH", "INVALID_EXPORT"} for status in statuses):
        status = "MISMATCH"
    elif all(status in ACCEPTED for status in statuses):
        status = VOLUME_ONLY_MATCH if VOLUME_ONLY_MATCH in statuses else "MATCH"
    else:
        status = "PARTIALLY_VERIFIED"
    return {
        "status": status,
        "expected_closes": sum(len(native_window(frame)) for frame in FRAMES),
        "protected_parity_closes": sum(lane["expected_closes"] for lane in frames.values() if lane["status"] in ACCEPTED),
        "frames_with_exact_normalized_logs": [
            frame for frame, lane in frames.items() if lane["status"] == "MATCH"
        ],
        "frames": frames,
        "alert_scope": "Native payload and intent parity. Server alert creation or iPhone delivery is outside the CSV evidence.",
        "execution_scope": "Native in-indicator S5 RAW Paper. The separately published RC2 strategy source is pinned.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-dir", type=Path, help="Folder with baseline-M*.csv and candidate-M*.csv")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = {"source": source_checks()}
    if args.native_dir:
        report["native"] = native_report(args.native_dir)
    content = json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8", newline="\n")
    else:
        print(content, end="")
    status = report.get("native", {}).get("status", "MATCH")
    return 0 if status in ACCEPTED else 1 if status == "MISMATCH" else 2


if __name__ == "__main__":
    raise SystemExit(main())
