#!/usr/bin/env python3
"""Verify final captured profile/readback against original evidence and frozen files."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re

from capture_baseline import unchanged_report
from capture_protocol_record import capture_current_ui, write_new
from extract_bars import BASELINE_SHA, ROOT, sha

OUT = Path(__file__).resolve().parent


def main() -> int:
    defaults = json.loads((OUT / "source_default_profile.json").read_text(encoding="utf-8"))["inputs"]
    original = capture_current_ui(defaults)
    restored = capture_current_ui(defaults, ROOT / "diagnostics/evidence/final_inputs_restored.txt", ROOT / "diagnostics/evidence/final_properties_restored.txt")
    original_values = {row["name"]: row["normalized_value"] for row in original["inputs"]}
    restored_values = {row["name"]: row["normalized_value"] for row in restored["inputs"]}
    changes = [{"name": key, "original": original_values[key], "restored": value} for key, value in restored_values.items() if value != original_values[key]]
    original_props = {row["label"]: row["raw_ui_values"] for row in original["properties"]}
    restored_props = {row["label"]: row["raw_ui_values"] for row in restored["properties"]}
    property_changes = [{"label": key, "original": original_props[key], "restored": value} for key, value in restored_props.items() if value != original_props[key]]
    editor_path = ROOT / "diagnostics/evidence/final_restored_editor.json"
    editor = json.loads(editor_path.read_text(encoding="utf-8"))
    chart_path = ROOT / "diagnostics/evidence/final_baseline_chart.txt"
    chart = chart_path.read_text(encoding="utf-8")
    patterns = {"net_pnl_display": r"- generic: Total PnL\n- text: ([^\n]+)",
                "profit_factor_display": r'- generic: Profit factor\n- generic: "([^"\n]+)"',
                "drawdown_display": r"- generic: Max drawdown\n- text: ([^\n]+)",
                "closed_parts_display": r'- generic: Trades distribution\n- generic: "([^"\n]+)"'}
    actual_metrics = {}
    for key, pattern in patterns.items():
        match = re.search(pattern, chart)
        actual_metrics[key] = match.group(1) if match else None
    expected_metrics = {"net_pnl_display": "−144.49USD", "profit_factor_display": "0.905", "drawdown_display": "842.26USD", "closed_parts_display": "212"}
    integrity = unchanged_report()
    checks = {"all_108_inputs_observed": original["observed_input_count"] == restored["observed_input_count"] == 108,
              "all_inputs_match_original": not changes, "all_11_property_groups_observed": len(original_props) == len(restored_props) == 11 and all(restored_props.values()),
              "all_properties_match_original": not property_changes,
              "recorded_editor_readback_hash_matches_baseline": editor.get("sha256_lf") == editor.get("expected_sha256") == BASELINE_SHA and editor.get("matches_baseline") is True,
              "production_pine_bytes_match_baseline": sha((ROOT / "unified/QIRVANTA_UNIFIED.pine").read_bytes()) == BASELINE_SHA,
              "visible_rounded_aggregates_match_original": actual_metrics == expected_metrics,
              "all_217_baseline_files_unchanged": integrity["checked_files"] == 217 and integrity["all_baseline_working_tree_bytes_unchanged"]}
    evidence = {**original["source_files"], **restored["source_files"],
                editor_path.relative_to(ROOT).as_posix(): sha(editor_path.read_bytes()),
                chart_path.relative_to(ROOT).as_posix(): sha(chart_path.read_bytes())}
    image = ROOT / "diagnostics/evidence/final_baseline_chart.png"
    if image.is_file():
        evidence[image.relative_to(ROOT).as_posix()] = sha(image.read_bytes())
    report = {"verified_at_utc": datetime.now(timezone.utc).isoformat(), "status": "PASS" if all(checks.values()) else "FAIL",
              "checks": checks, "input_changes": changes, "property_changes": property_changes,
              "actual_restored_inputs": restored_values, "actual_restored_properties": restored_props,
              "editor_readback_record": editor,
              "editor_verification_scope": "Root captured the clipboard readback and LF-normalized hash. This independent validator checks that record against the committed/current baseline artifact; it does not claim an additional UI readback.",
              "actual_visible_metrics": actual_metrics, "expected_visible_metrics": expected_metrics,
              "metric_scope": "Current restored chart's rounded visible aggregates; not a new full-dataset or event-journal equivalence claim.",
              "baseline_integrity": integrity, "evidence_sha256": evidence,
              "candidate_in_production": False}
    write_new("final_restore_verification.json", report)
    print(json.dumps({"status": report["status"], "checks": checks}))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
