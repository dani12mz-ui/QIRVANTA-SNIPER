"""Replay registered H1 evidence from Git, keeping numeric and state tests independent."""

import argparse
from decimal import Decimal
from gzip import compress
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from research.core_htf_readiness import build_probe, compare_native
from research.net_campaign_evidence.audit_cooldown import CORE_FILES, ENTRY_FILES, NOV11
from research.net_campaign_evidence.control_state import compare_state
from research.net_campaign_evidence.cooldown_open_checks import ELIGIBLE, audit_open_checks
from research.net_campaign_evidence.native_raw_metrics import summarize_raw
from research.net_campaign_evidence.verify_readiness import (
    HTF_COMMIT, NATIVE_SHA, ROOT as HTF_PATH, SETTINGS_SHA, VISUAL, pinned, require,
)


REFERENCE = "7fc1f0b71edeb05da804385e0351e9228015dc0a"
RESEARCH = "research/net_campaign_evidence"
ENTRY = "research/entry_decision_audit"
DEEP_M15_SHA = "230c37c21558fcd3dd1790e4689059f2d9808fbbc1efd594688b837071e187a7"
FRAMES = (5, 15, 30)


def materialize(archive, files, folder):
    """Extract only the registered filenames, never arbitrary members."""
    with ZipFile(BytesIO(archive)) as zipped:
        for name in files:
            target = folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zipped.read(name))


def prepare(repo, work):
    core = work / "core"
    entries = work / "entries"
    latest = work / "latest"
    settings = json.loads(pinned(repo, HTF_COMMIT, f"{HTF_PATH}/evidence/native-settings.json", SETTINGS_SHA))
    archive = pinned(repo, HTF_COMMIT, f"{HTF_PATH}/evidence/native-exports.zip", NATIVE_SHA)
    core_names = [f"native/M{frame}-{depth}-add.csv" for frame, depth in CORE_FILES]
    core_names += [f"native/M{frame}-1223-{name}.csv" for frame in FRAMES for name in ("control", "reload")]
    materialize(archive, core_names + ["native/M5-1223-readd.csv"], core)
    materialize(
        pinned(repo, REFERENCE, f"{ENTRY}/evidence/native-exports.zip"),
        [f"{folder}/audit-on-M{frame}.csv" for frame, folder in ENTRY_FILES if (frame, folder) != (30, "native-recent")],
        entries,
    )
    materialize(
        pinned(repo, REFERENCE, f"{RESEARCH}/evidence/supplemental-M30-native.zip"),
        ["native-recent/audit-on-M30.csv"], entries,
    )
    materialize(
        pinned(repo, REFERENCE, f"{RESEARCH}/evidence/M15-5000-native.zip"),
        ["M15-5000-add.csv"], latest,
    )
    return settings, core / "native", entries, latest / "M15-5000-add.csv"


def load_core(path, frame, start, end, cutoff, depth, *, protected_only=False, expected_sha=None):
    if expected_sha:
        require(sha256(path.read_bytes()).hexdigest() == expected_sha, f"{path.name} differs from the registered digest")
    run = compare_native.load_run(path, frame, start, end, protected_only=protected_only)
    require(len(run["market"]) == depth and next(reversed(run["market"])) == cutoff,
            f"{path.name} chart origin/end are incompatible with the registered comparison")
    return run


def depth_pair(left, right, probe):
    analysis = compare_native.compare(left, right, probe, probe, "depth")
    require("htf_price_fields" in analysis and analysis["ema_absolute_tolerance"] == "0.001",
            "Expected a registered depth comparison at the original tolerance")
    state = compare_state(left, right)
    return {
        "history": analysis["common_confirmed_ohlc"],
        "calculation_sensitivity": {
            "ema_absolute_tolerance": analysis["ema_absolute_tolerance"],
            "htf_close_requires_exact_match": analysis["htf_close_requires_exact_match"],
            "prices": analysis["htf_price_fields"],
            "indicator_booleans": {name: analysis["exact_state_fields"][name]
                                   for name in ("ready_HTF", "warmup", "core_trend")},
            "local_recursion": analysis["local_warmup_numeric_sensitivity"],
        },
        "state_sensitivity": state,
        "native_event_summary": analysis["s5"],
        "status": analysis["status"],
    }


def compare_observed_decisions(groups):
    """Compare the one rule on the recorded state. The original state is never advanced by B."""
    rows = []
    for group in groups:
        captures = group["captures"]
        compared = group["opening_and_fill_clock_evidence_by_capture"]
        require(len(captures) == len(compared) and bool(captures), "Missing case-level comparison evidence")
        states = []
        for case, opening in zip(captures, compared):
            require(case["source_id"] == opening["source_id"], "Case-level archives are out of order")
            diagnosis = opening["native_same_side_fill_cooldown_lookback"]["status"]
            disposition = case["disposition"]
            if disposition == ELIGIBLE and diagnosis == "PASS_BY_COMPLETE_LOOKBACK":
                variant = "CLOSE_GATE_CLEARS_ON_OBSERVED_STATE"
            elif disposition.startswith("EXCLUDED") or diagnosis == "FAIL_SAME_SIDE_FILL_WITHIN_COOLDOWN":
                variant = "REMAINS_REJECTED"
            else:
                variant = "UNKNOWN"
            states.append({
                "source_id": case["source_id"],
                "A": {"state": "REJECTED" if case["confirmation"] else "UNKNOWN", "reason": case["current_reason"]},
                "B_on_observed_A_state": variant,
                "known_actual_fill_check": diagnosis,
                "mechanical_next_open": opening["hypothetical_next_open_only"]["status"],
            })
        outcomes = {row["B_on_observed_A_state"] for row in states}
        rows.append({
            "frame": group["frame"], "known_close_ms": group["close_ms"], "side": group["side"],
            "B_on_observed_A_state": next(iter(outcomes)) if len(outcomes) == 1 else "UNKNOWN_CAPTURE_DISAGREEMENT",
            "by_capture": states,
        })
    return {
        "scope": "Local pointwise cooldown comparison on the recorded A state. No altered state, queue, trade, or equity replay.",
        "cases": rows,
    }


def audit_controls(repo, work):
    settings, core, entry, newest = prepare(repo, work)
    start, end = map(compare_native.timestamp, NOV11)
    cutoff = compare_native.timestamp(settings["common_last_historical_close"])
    require(start <= end < cutoff < compare_native.SEALED_START, "Control touched the reserved period")
    base = pinned(repo, compare_native.CONTROL, VISUAL, compare_native.CONTROL_SHA256)
    require((repo / VISUAL).read_bytes() == base, "Verified Visual V2 source changed")
    audit_source = pinned(repo, build_probe.BASE, build_probe.SOURCE, build_probe.DIGEST)
    probe_patch = json.loads(pinned(repo, HTF_COMMIT, f"{HTF_PATH}/probe-patch.json"))
    probe = sha256(build_probe.build(audit_source.decode("utf-8"), probe_patch).encode()).hexdigest()
    require(probe == settings["probe_sha256"], "Probe does not match original native captures")

    controls = {}
    for frame in FRAMES:
        def load(depth, label):
            return load_core(core / f"M{frame}-{depth}-{label}.csv", frame, start, end, cutoff, depth,
                             protected_only=label == "control",
                             expected_sha=CORE_FILES[frame, depth] if label == "add" else None)

        control, shallow, deep = load(1223, "control"), load(1223, "add"), load(3200, "add")
        parity = compare_native.compare(control, shallow, compare_native.CONTROL_SHA256, probe, "protected")
        require(parity["status"] == "ADDITIVE_PROBE_PROTECTED_PARITY", f"M{frame} diagnostic changed protected logs")
        repeat = {}
        for label in (("reload", "readd") if frame == 5 else ("reload",)):
            replay = load(1223, label)
            record = compare_native.compare(shallow, replay, probe, probe, "same")
            require(record["status"] == "SAME_HISTORY_NATIVE_MATCH", f"M{frame} same-history comparison failed")
            repeat[label] = record
        runs = {"1223": shallow, "3200": deep}
        sensitivity = {"1223_vs_3200": depth_pair(shallow, deep, probe)}
        if frame == 15:
            latest = load_core(newest, frame, start, end, cutoff, 5000, expected_sha=DEEP_M15_SHA)
            runs["5000"] = latest
            sensitivity["3200_vs_5000"] = depth_pair(deep, latest, probe)
        controls[f"M{frame}"] = {
            "parity_with_unmodified_visual_v2": parity,
            "repeatability": {"verified_repeats_at_1223": repeat, "deeper_independent_repeats_available": False},
            "depth_sensitivity": sensitivity,
            "native_original_raw_by_depth": {depth: summarize_raw(run) for depth, run in runs.items()},
            "history_registrations": {depth: compare_native.run_details(run, probe) for depth, run in runs.items()},
        }

    case_checks = audit_open_checks(core, entry, newest)
    require(all(source["status"] == "VERIFIED_BOUNDED_NATIVE_INPUT" for source in case_checks["inputs"]),
            "Registered case window missing or invalid")
    return {
        "schema": "qirvanta.h1-cooldown-continuation.v1",
        "source_commit": REFERENCE,
        "pinned_indicator": {"commit": compare_native.CONTROL, "sha256": compare_native.CONTROL_SHA256},
        "study_id": "H1_CANCELLED_ZERO_FILL_COOLDOWN",
        "market": {"symbol": settings["symbol"], "chart": settings["chart"], "timezone": settings["timezone"],
                   "score_first_close_utc": NOV11[0], "score_last_close_utc": NOV11[1],
                   "historical_cutoff_utc": settings["common_last_historical_close"],
                   "native_settings": settings["setup"]},
        "controls": controls,
        "known_at_case_diagnostics": case_checks,
        "decision_ab_fixed_observed_state": compare_observed_decisions(case_checks["unique_observed_cases"]),
        "portfolio_ab": {
            "status": "PRECONDITIONS_NOT_MET",
            "same_history_repeatability_at_1223": True,
            "numeric_stability": "M15_3200_VS_5000_FAILS_AT_0.001",
            "complete_initial_state_and_barwise_execution_envelopes": False,
            "holdout_available": False,
            "normal": {"A": None, "B": None},
            "stress": {"A": None, "B": None},
            "additional_or_displaced_executed_campaigns": None,
            "scope": "Native RAW bounded control observations and mechanical original entry checks are recorded separately. "
                     "Legacy S5 archives cannot supply decisions for this current-version A/B.",
        },
    }


def encode_decimal(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Unsupported evidence type: {type(value).__name__}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with TemporaryDirectory(prefix="qirvanta-h1-") as work:
        result = audit_controls(args.repo.resolve(), Path(work))
    text = json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True, default=encode_decimal) + "\n"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(compress(text.encode(), mtime=0) if args.output.suffix == ".gz" else text.encode())
    print(result["portfolio_ab"]["status"])


if __name__ == "__main__":
    main()
