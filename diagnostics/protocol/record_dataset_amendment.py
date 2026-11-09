#!/usr/bin/env python3
"""Append actual capture boundaries/fingerprint comparisons without editing registration."""
import csv
from datetime import datetime, timezone
import io
import json
from pathlib import Path
from extract_bars import ROOT, json_ready, read_json, sha, utc
from capture_protocol_record import write_new

OUT = Path(__file__).resolve().parent
START_MS = int(datetime(2026, 8, 3, tzinfo=timezone.utc).timestamp() * 1000)


def native_records(path):
    raw = path.read_bytes()
    records = []
    for row in csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline="")):
        for line in row["Message"].splitlines():
            if line.startswith("{"):
                value = read_json(line)
                if isinstance(value, dict) and value.get("schema") == "qirvanta.native.v1":
                    records.append(value)
    return raw, records


def main():
    manifest_path = OUT / "datasets/B-ALL-FULL/dataset_manifest.json"
    # Use the same Decimal parser as the raw log; Decimal('0.001') != float(0.001).
    manifest = read_json(manifest_path.read_text(encoding="utf-8"))
    keys = ["symbol", "timeframe", "source_sha256", "account_currency", "symbol_currency", "pointvalue",
            "first_bar_time", "last_bar_open", "cutoff", "bar_count", "checksum1", "checksum2", "mintick"]
    reports = []
    for name in ("B-ALL", "B-RETEST", "B-FAST", "B-REVERSAL", "B-ALL-FULL"):
        path = ROOT / "diagnostics/evidence" / f"{name}.pine-logs.csv"
        raw, records = native_records(path)
        runs = [row for row in records if row["kind"] == "run"]
        if len(runs) != 1:
            raise ValueError(f"Expected one run record for {name}")
        run = runs[0]
        submissions = [row for row in records if row["kind"] == "submitted"]
        reports.append({"run_file": name, "source_csv_sha256": sha(raw),
                        "data_fingerprint_fields": {key: run[key] for key in keys},
                        "matches_full_export_metadata": all(run[key] == manifest["run_metadata"][key] for key in keys),
                        "submitted_before_protocol_start": sum(row["submitted_time"] < START_MS for row in submissions),
                        "first_submission_utc": utc(min(row["submitted_time"] for row in submissions)) if submissions else None,
                        "native_records": len(records),
                        "normalized_native_records_sha256": sha(json.dumps(json_ready(records), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode())})
    comparison = {"created_at_utc": datetime.now(timezone.utc).isoformat(), "runs": json_ready(reports),
                  "all_reported_fingerprints_match": all(row["matches_full_export_metadata"] for row in reports),
                  "all_vs_full_native_records_match": reports[0]["normalized_native_records_sha256"] == reports[-1]["normalized_native_records_sha256"],
                  "scope": "Same recorded boundaries/count and dual modular fingerprints across diagnostic runs; SHA-256 of full exported data available for B-ALL-FULL only. Not cryptographic proof every run had identical full backend data, or exact original earlier dataset.",
                  "dataset_manifest_sha256": sha(manifest_path.read_bytes())}
    comparison["supersedes"] = "diagnostic_dataset_comparison.json"
    comparison["extraction_correction"] = "Use Decimal consistently for manifest/log numeric comparisons; initial false mismatch was only binary-float versus Decimal mintick=0.001, not differing source data."
    write_new("diagnostic_dataset_comparison_v2.json", comparison)
    with (OUT / "datasets/B-ALL-FULL/bars.csv").open(encoding="utf-8", newline="") as stream:
        prefix_rows = sum(int(row["time"]) < START_MS for row in csv.DictReader(stream))
    write_new("registration_amendment_03.json", {
        "created_at_utc": datetime.now(timezone.utc).isoformat(), "type": "ACTUAL_CAPTURE_BOUNDARIES_AND_EXECUTION_METHOD_DISCLOSURE",
        "original_registration_sha256": sha((OUT / "preregistration.json").read_bytes()),
        "supersedes_dataset_comparison_in": "registration_amendment_02.json",
        "correction": "Metadata comparison now parses both sides as Decimal; initial binary-float/Decimal mintick comparison was false despite equal exported values. Capture boundaries and trading rules unchanged.",
        "original_protocol_sha256": sha((OUT / "EVALUATION_PROTOCOL_BG.md").read_bytes()),
        "registered_period_start_utc": "2026-08-03T00:00:00Z",
        "observed_first_loaded_open_utc": manifest["first_open_utc"], "observed_first_loaded_open_ms": manifest["first_open_time"],
        "observed_last_closed_bar_close_utc": manifest["last_close_utc"], "observed_bar_count": manifest["bar_count"],
        "rows_before_registered_start_retained_as_original_warmup": prefix_rows,
        "submitted_before_registered_start": {row["run_file"]: row["submitted_before_protocol_start"] for row in reports},
        "planned_ui_date_overrides_applied": False,
        "execution_method": "Original input dates 2020-01-01 through 2035-01-01 were retained in all runs to avoid modifying baseline trading state. Observation-only appendix restricts exported bars/events/snapshot to time_close <= 2026-11-09T02:45Z. Eight original prefix bars before Aug3 00:00Z are retained; no native submission precedes the registered start.",
        "interpretation": "New actual diagnostic rerun with matching combined PnL/journal. The old Aug3–Nov9 UI label is not an exact UTC boundary and is not proof the earlier original dataset is identical.",
        "dataset_precision": manifest["precision"],
        "dataset_manifest": "datasets/B-ALL-FULL/dataset_manifest.json", "dataset_manifest_sha256": sha(manifest_path.read_bytes()),
        "dataset_comparison": "diagnostic_dataset_comparison_v2.json", "dataset_comparison_sha256": sha((OUT / "diagnostic_dataset_comparison_v2.json").read_bytes()),
        "rule_changes": [], "original_protocol_or_registration_overwritten": False,
        "true_future_holdout_available": False,
    })
    print(json.dumps({"first_utc": manifest["first_open_utc"], "prefix_rows": prefix_rows,
                      "all_reported_fingerprints_match": comparison["all_reported_fingerprints_match"],
                      "all_vs_full_native_records_match": comparison["all_vs_full_native_records_match"]}))


if __name__ == "__main__":
    main()
