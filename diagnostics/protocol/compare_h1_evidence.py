#!/usr/bin/env python3
"""Independently compare actual H1 bars/settings against the frozen diagnostic baseline."""
from datetime import datetime, timezone
import json
from pathlib import Path

from capture_protocol_record import capture_current_ui, write_new
from extract_bars import ROOT, archive, sha

OUT = Path(__file__).resolve().parent
REG_SHA = "2b26022eb385d7f4a3574b8792b9ec9eb6d9d844b5953e27147fc891783f92c5"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    registration = ROOT / "diagnostics/diagnosis/H1_PREREGISTRATION.json"
    if sha(registration.read_bytes()) != REG_SHA:
        raise ValueError("H1 preregistration changed")
    generated_path = ROOT / "diagnostics/diagnosis/H1_GENERATED_v2.json"
    generated = read(generated_path)
    if generated["registration_sha256"] != REG_SHA or generated["editor_buffer_sha256"] != sha((ROOT.parent / "h1_probe.txt").read_bytes()):
        raise ValueError("Actual H1 editor buffer or registration does not match the source manifest")
    required = [ROOT / "diagnostics/evidence" / f"{name}.{suffix}" for name in ("H1-ALL", "H1-FAST") for suffix in ("pine-logs.csv", "inputs.txt")]
    absent = [str(path) for path in required if not path.is_file()]
    if absent:
        raise SystemExit("Pending evidence files: " + ", ".join(absent))
    defaults = read(OUT / "source_default_profile.json")["inputs"]
    original = capture_current_ui(defaults)
    original_values = {row["name"]: row["normalized_value"] for row in original["inputs"]}
    baseline_dir = OUT / "datasets/B-ALL-FULL"
    baseline_manifest = read(baseline_dir / "dataset_manifest.json")
    baseline_csv = (baseline_dir / "bars.csv").read_bytes()
    baseline_json = (baseline_dir / "bars.json").read_bytes()
    if sha(baseline_csv) != baseline_manifest["canonical_csv"]["sha256"] or sha(baseline_json) != baseline_manifest["canonical_json"]["sha256"]:
        raise ValueError("Archived baseline bars have changed")
    reports = []
    for name, model in (("H1-ALL", "Всички"), ("H1-FAST", "Бърз пробив")):
        source = ROOT / "diagnostics/evidence" / f"{name}.pine-logs.csv"
        dataset_dir = OUT / "datasets" / name
        if not dataset_dir.exists():
            manifest = archive(source.resolve(), dataset_dir.resolve(), generated["candidate_strategy_sha256"])
        else:
            manifest = read(dataset_dir / "dataset_manifest.json")
            if sha(source.read_bytes()) != manifest["source_log_sha256"]:
                raise ValueError(f"Raw source changed after archiving {name}")
        actual_csv = (dataset_dir / "bars.csv").read_bytes()
        actual_json = (dataset_dir / "bars.json").read_bytes()
        archive_hashes_valid = sha(actual_csv) == manifest["canonical_csv"]["sha256"] and sha(actual_json) == manifest["canonical_json"]["sha256"]
        capture = capture_current_ui(defaults, ROOT / "diagnostics/evidence" / f"{name}.inputs.txt")
        actual = {row["name"]: row["normalized_value"] for row in capture["inputs"]}
        expected = dict(original_values, runModel=model)
        changes = [{"name": key, "original": original_values[key], "actual": value} for key, value in actual.items() if value != original_values[key]]
        unplanned = [{"name": key, "expected": expected[key], "actual": value} for key, value in actual.items() if value != expected[key]]
        data_equal = actual_csv == baseline_csv and actual_json == baseline_json
        inputs_match = capture["observed_input_count"] == 108 and not unplanned
        valid = manifest["status"] == "PASS_OBSERVED_SERIALIZED_EXPORT" and archive_hashes_valid and data_equal and inputs_match
        reports.append({"run_id": name, "status": "PASS" if valid else "FAIL",
                        "source_log_sha256": sha(source.read_bytes()), "dataset_manifest_sha256": sha((dataset_dir / "dataset_manifest.json").read_bytes()),
                        "dataset_manifest": (dataset_dir / "dataset_manifest.json").relative_to(ROOT).as_posix(),
                        "archive_hashes_valid": archive_hashes_valid, "canonical_csv_bytes_equal_baseline": actual_csv == baseline_csv,
                        "canonical_json_bytes_equal_baseline": actual_json == baseline_json,
                        "canonical_csv_sha256": sha(actual_csv), "canonical_json_sha256": sha(actual_json),
                        "observed_bar_count": manifest["bar_count"], "first_open_utc": manifest["first_open_utc"], "last_close_utc": manifest["last_close_utc"],
                        "source_strategy_sha256": manifest["run_metadata"]["source_sha256"],
                        "expected_model": model, "observed_input_count": capture["observed_input_count"],
                        "observed_changes_from_original_inputs": changes, "unplanned_input_changes": unplanned,
                        "actual_normalized_inputs": actual, "settings_evidence_sha256": capture["source_files"]})
    log = OUT / "H1_integrity_tests.log"
    report = {"created_at_utc": datetime.now(timezone.utc).isoformat(), "status": "PASS" if all(row["status"] == "PASS" for row in reports) else "FAIL",
              "hypothesis": "H1_FAST_EFFICIENCY_025", "registration_sha256": REG_SHA,
              "generated_manifest_sha256": sha(generated_path.read_bytes()),
              "candidate_strategy_sha256": generated["candidate_strategy_sha256"], "editor_buffer_sha256": generated["editor_buffer_sha256"],
              "independent_generator_and_parser_tests": {"log": log.relative_to(ROOT).as_posix(), "sha256": sha(log.read_bytes()), "tests": 16, "scope": "Exact single FAST gate/reason change; unchanged remainder/observer contract; mandatory guards under normal and optimized Python; export integrity"},
              "baseline_dataset_manifest_sha256": sha((baseline_dir / "dataset_manifest.json").read_bytes()),
              "runs": reports,
              "data_equality_scope": "Byte-identical canonical serialized OHLC/volume/HTF rows, including row order and missing-session gaps, across B-ALL-FULL/H1-ALL/H1-FAST. Export precision is at most 8 decimals; no claim of original backend full precision or the earlier pre-diagnostic dataset.",
              "properties_scope": "Original Properties captured once; candidate strategy declaration is byte-identical to baseline and all108 observed Inputs were checked. Properties tab was not independently recaptured for every candidate run.",
              "financial_result_scope": "No performance inference in this integrity report; accounting and hypothesis comparison are separate.",
              "production_adopted": False}
    write_new("H1_evidence_comparison.json", report)
    print(json.dumps({"status": report["status"], "runs": [{key: row[key] for key in ("run_id", "status", "observed_bar_count", "observed_input_count", "canonical_csv_bytes_equal_baseline")} for row in reports]}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
