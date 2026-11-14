"""Reverify the registered TradingView HTF readiness experiment before campaign research."""

import argparse
import csv
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from research.core_htf_readiness import build_probe, compare_native
from research.direct_trend_breakout.shadow_native import OHLC


HTF_COMMIT = "56c9dd25d506341042e8cc3c078fcfb92be650b8"
ROOT = "research/core_htf_readiness"
VISUAL = "research/luminous_visual_v2/QIRVANTA_GOLD_PRO_VISUAL_V2.pine"
NATIVE_SHA = "64383cf0a3ec0ff73e32f222f1d95213f8f9b163138c1f77b910ea8ce9c5c6ee"
SETTINGS_SHA = "d7598cb7210078880f18ca0e58d7d695720d7f5fef6fa14ae5cba5a83d71c1fe"
SOURCE_FILES = {
    5: ("M5_dataset", "44971f52b0008fefc4b61dda51c71043f08e4a2e3bc6d08121380e95232ace1d"),
    15: ("ui_dataset", "6c89015105a406c2a0c1567f0e9a5e445cb31ee7604a985d727f26892d8ba4b7"),
    30: ("M30_dataset", "1b31dc5dad532b9362c5340511211fbc13a33c64983d4a49485c7442a269c5d5"),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def pinned(repo, commit, path, digest=None):
    content = subprocess.check_output(["git", "-C", str(repo), "show", f"{commit}:{path}"])
    if digest is not None:
        require(sha256(content).hexdigest() == digest, f"Unexpected source digest: {commit}:{path}")
    return content


def utc(stamp):
    return datetime.fromtimestamp(stamp / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_development_csv(data_root, frame):
    folder, digest = SOURCE_FILES[frame]
    path = data_root / folder / "bars.csv"
    # Use only registered bytes. Do not parse an unregistered archive.
    raw = path.read_bytes()
    require(sha256(raw).hexdigest() == digest, f"Unexpected registered history digest: {path}")
    reader = csv.DictReader(raw.decode("utf-8-sig").splitlines())
    candles = {}
    htf_present = dict.fromkeys(("htfClose", "htfEMA50", "htfEMA50prev"), 0)
    for row in reader:
        clock = int(row["time_close"])
        require(clock < compare_native.SEALED_START and clock not in candles, "Invalid development bar clock")
        candles[clock] = (int(row["time"]), clock, *(Decimal(row[key]) for key in OHLC))
        for key in htf_present:
            htf_present[key] += row[key] not in ("", "null", "NaN", "na")
    require(candles and list(candles) == sorted(candles), "Disordered registered archive")
    return candles, digest, htf_present


def verify_local_extension(data_root, frame, deep):
    old, digest, htf_present = load_development_csv(data_root, frame)
    new = {clock: compare_native.candle_value(value) for clock, value in deep["market"].items()}
    overlap = sorted(old.keys() & new.keys())
    conflicts = [utc(clock) for clock in overlap if old[clock] != new[clock]]
    added = sorted(new.keys() - old.keys())
    require(all(clock < compare_native.SEALED_START for clock in new), "Native archive reaches sealed dates")
    append_only = bool(added) and added[0] > max(old)
    return {
        "scope": "Development chart OHLC comparison; legacy observer HTF fields are from 48e5cdd and cannot replace the exact current selected core tuple.",
        "dataset_relative_path": f"{SOURCE_FILES[frame][0]}/bars.csv",
        "legacy_sha256": digest,
        "legacy_chart_bars": len(old),
        "legacy_htf_nonnull_by_field": htf_present,
        "native_chart_bars": len(new),
        "overlapping_chart_bars": len(overlap),
        "overlap_ohlc_or_clock_conflicts": conflicts,
        "extension_is_right_edge_only": append_only,
        "added_chart_bars": len(added),
        "exact_ohlc_union_count": len(old.keys() | new.keys()) if overlap and not conflicts and append_only else None,
        "old_first_open_utc": utc(next(iter(old.values()))[0]),
        "old_last_close_utc": utc(max(old)),
        "new_last_close_utc": utc(max(new)),
    }


def available_points(result):
    return {name: row for name, row in result["exact_state_fields"].items()
            if name in ("warmup", "ready_HTF", "core_trend")}


def at_depth(runs, shallow_depth, deep_depth, cutoff):
    for name, run in runs.items():
        market = run["market"]
        expected_count = deep_depth if name == "deep" else shallow_depth
        require(len(market) == expected_count and next(reversed(market)) == cutoff,
                f"M{run['frame']} {name} lacks expected prefix depth and endpoint")
        require(cutoff < compare_native.SEALED_START, "Native history reaches sealed dates")


def compare_archive(repo, data_root):
    archive_path = ROOT + "/evidence/native-exports.zip"
    settings_path = ROOT + "/evidence/native-settings.json"
    archive = pinned(repo, HTF_COMMIT, archive_path, NATIVE_SHA)
    settings = json.loads(pinned(repo, HTF_COMMIT, settings_path, SETTINGS_SHA))
    original = pinned(repo, compare_native.CONTROL, VISUAL, compare_native.CONTROL_SHA256)
    local_source = (repo / VISUAL).read_bytes()
    require(original == local_source, "Current worktree Visual V2 differs from registered baseline")
    parent = pinned(repo, build_probe.BASE, build_probe.SOURCE, build_probe.DIGEST)
    patch = json.loads(pinned(repo, HTF_COMMIT, ROOT + "/probe-patch.json"))
    reconstructed = build_probe.build(parent.decode("utf-8"), patch).encode("utf-8")
    probe_sha = sha256(reconstructed).hexdigest()
    require(probe_sha == settings["probe_sha256"], "Rebuilt diagnostic probe fails native manifest hash")
    shallow, deep = settings["chart_depths"]
    require((shallow, deep) == (1223, 3200), "Unregistered comparison depths")
    start, end = map(compare_native.timestamp, settings["target_close_window"])
    cutoff = compare_native.timestamp(settings["common_last_historical_close"])
    require(start <= end <= cutoff < compare_native.SEALED_START, "Unexpected technical windows")
    results = {}
    with ZipFile(BytesIO(archive)) as zipped, TemporaryDirectory(prefix="qirvanta-htf-") as directory:
        for frame in SOURCE_FILES:
            paths = {
                "control": f"M{frame}-{shallow}-control.csv",
                "shallow": f"M{frame}-{shallow}-add.csv",
                "reload": f"M{frame}-{shallow}-reload.csv",
                "deep": f"M{frame}-{deep}-add.csv",
            }
            if frame == 5:
                paths["readd"] = f"M{frame}-{shallow}-readd.csv"
            runs = {}
            for name, member in paths.items():
                target = Path(directory) / member
                target.write_bytes(zipped.read("native/" + member))
                runs[name] = compare_native.load_run(target, frame, start, end, protected_only=name == "control")
            at_depth(runs, shallow, deep, cutoff)
            attempt = compare_native.compare(
                runs["control"], runs["shallow"], compare_native.CONTROL_SHA256, probe_sha, "protected")
            reload = compare_native.compare(runs["shallow"], runs["reload"], probe_sha, probe_sha, "same")
            changes = compare_native.compare(runs["shallow"], runs["deep"], probe_sha, probe_sha, "depth")
            require(attempt["status"] == "ADDITIVE_PROBE_PROTECTED_PARITY", f"M{frame} failed baseline parity")
            require(reload["status"] == "SAME_HISTORY_NATIVE_MATCH", f"M{frame} failed same-history reload")
            require(changes["status"] == "DEPTH_SENSITIVITY_MISMATCH", f"M{frame} failed to reproduce sensitivity")
            result = {
                "original_source_vs_instrumented_control": attempt,
                "same_history_reload": reload,
                "depth_sensitivity": {
                    "status": changes["status"],
                    "shallow": changes["left"], "deep": changes["right"],
                    "common_confirmed_ohlc": changes["common_confirmed_ohlc"],
                    "ema_absolute_tolerance": changes["ema_absolute_tolerance"],
                    "selected_core_price_field_differences": changes["htf_price_fields"],
                    "readiness_warmup_and_direction_differences": available_points(changes),
                    "s5": changes["s5"],
                    "any_observed_s5_state_drift": changes["any_observed_s5_state_drift"],
                },
            }
            if "readd" in runs:
                readd = compare_native.compare(runs["shallow"], runs["readd"], probe_sha, probe_sha, "same")
                require(readd["status"] == "SAME_HISTORY_NATIVE_MATCH", "M5 re-add failed")
                result["same_history_readd"] = readd
            if data_root is not None:
                result["registered_local_development_chart_extension"] = verify_local_extension(data_root, frame, runs["deep"])
            results[f"M{frame}"] = result
    return {
        "schema": "qirvanta.net_campaign_evidence.htf_readiness.v1",
        "status": "NATIVE_AVAILABILITY_REPRODUCED_NUMERICAL_STABILITY_NOT_ESTABLISHED",
        "provenance": {
            "visual_commit": compare_native.CONTROL, "visual_sha256": compare_native.CONTROL_SHA256,
            "diagnostic_parent_commit": build_probe.BASE, "reconstructed_probe_sha256": probe_sha,
            "native_archive": {"commit": HTF_COMMIT, "path": archive_path, "sha256": NATIVE_SHA},
            "native_settings": {"commit": HTF_COMMIT, "path": settings_path, "sha256": SETTINGS_SHA},
        },
        "native_window": {"first_audited_close_utc": utc(start), "last_audited_close_utc": utc(end),
                          "fixed_historical_last_close_utc": utc(cutoff)},
        "frames": results,
        "independent_untouched_bars_admitted": 0,
        "missing_evidence": [
            "A second sufficiently deep same-end native run has not established numeric and directional stability at the pinned tolerance.",
            "Core tuple source timestamps and the TradingView EMA seed are absent. Exact chart OHLC and chart-close trace alone do not identify them.",
            "The deep native protected ledger covers a Nov 11 case-study window; additional chart bars do not provide a complete current Visual V2 campaign tape.",
            "All work here is archival replay and reproducibility. No new native execution or sealed data was used.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--data-root", type=Path, help="Optional parent of the three registered legacy source folders")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare_archive(args.repo.resolve(), args.data_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes((json.dumps(result, ensure_ascii=False, default=str, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    print(result["status"])


if __name__ == "__main__":
    main()

