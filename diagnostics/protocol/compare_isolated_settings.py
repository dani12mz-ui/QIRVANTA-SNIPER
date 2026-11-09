#!/usr/bin/env python3
"""Compare actual saved Inputs controls; source defaults never fill missing UI evidence."""
from datetime import datetime, timezone
import argparse
import json
from capture_protocol_record import ROOT, OUT, capture_current_ui, write_new


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="isolated_settings_comparison.json")
    args = parser.parse_args()
    defaults = json.loads((OUT / "source_default_profile.json").read_text(encoding="utf-8"))["inputs"]
    original = capture_current_ui(defaults)
    baseline = {row["name"]: row["normalized_value"] for row in original["inputs"]}
    reports = []
    for run, selected in (("B-RETEST", "Ретест"), ("B-FAST", "Бърз пробив"), ("B-REVERSAL", "Ранно обръщане")):
        capture = capture_current_ui(defaults, ROOT / "diagnostics/evidence" / f"{run}.inputs.txt")
        actual = {row["name"]: row["normalized_value"] for row in capture["inputs"]}
        changes = [{"name": name, "original": baseline[name], "actual": value} for name, value in actual.items() if value != baseline[name]]
        expected = [{"name": "runModel", "original": "Всички", "actual": selected}]
        complete = capture["observed_input_count"] == capture["expected_input_count"] == 108
        reports.append({"run_id": run, "status": "PASS" if complete and changes == expected else "FAIL",
                        "observed_input_count": capture["observed_input_count"], "expected_input_count": 108,
                        "observed_changes": changes, "expected_changes": expected,
                        "all_other_inputs_identical": complete and all(actual[name] == baseline[name] for name in baseline if name != "runModel"),
                        "actual_normalized_inputs": actual, "evidence_sha256": capture["source_files"]})
    report = {"created_at_utc": datetime.now(timezone.utc).isoformat(), "baseline_commit": "48e5cddab17ea831bdc277f17f0406329fdfe39c",
              "comparison_scope": "All actual Inputs controls in three isolated saved dialogs versus original_inputs.txt; original Properties recorded once, not separately recaptured for every isolated run",
              "source_defaults_used_to_fill_missing_values": False,
              "original_observed_input_count": original["observed_input_count"], "original_evidence_sha256": original["source_files"],
              "runs": reports, "status": "PASS" if all(row["status"] == "PASS" for row in reports) else "FAIL"}
    write_new(args.output, report)
    print(json.dumps({"status": report["status"], "runs": [{"run_id": row["run_id"], "observed_input_count": row["observed_input_count"], "changes": row["observed_changes"]} for row in reports]}, ensure_ascii=True))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
