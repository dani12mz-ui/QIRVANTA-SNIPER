#!/usr/bin/env python3
"""Record known evidence, source defaults, and the preregistered document hash."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
BASELINE = "48e5cddab17ea831bdc277f17f0406329fdfe39c"


def blob(path: str) -> bytes:
    return subprocess.check_output(["git", "show", f"{BASELINE}:{path}"], cwd=ROOT)


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_new(name: str, data: dict) -> None:
    target = OUT / name
    if target.exists():
        raise SystemExit(f"Refusing to overwrite {name}")
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def first_argument(text: str) -> str:
    """Extract the default expression without executing Pine expressions."""
    depth = 0
    quoted = False
    escaped = False
    for i, char in enumerate(text):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "," and depth == 0:
            return text[:i].strip()
    raise ValueError("Unterminated input default")


def dom_scalar(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def dom_controls(lines: list[str]) -> list:
    values = []
    for i, line in enumerate(lines):
        match = re.match(r'\s*- (?:textbox(?: "[^"]+")?|combobox)(?: \[[^\]]+\])*:(?: (.*))?$', line)
        if match:
            if match.group(1):
                values.append(dom_scalar(match.group(1)))
            elif i + 1 < len(lines):
                child = re.match(r"    - generic: (.*)$", lines[i + 1])
                if child:
                    values.append(dom_scalar(child.group(1)))
    return values


def capture_current_ui(defaults: list[dict], input_source: Path | None = None, properties_source: Path | None = None) -> dict:
    source = input_source or ROOT / "diagnostics/evidence/original_inputs.txt"
    props = properties_source or ROOT / "diagnostics/evidence/original_properties.txt"
    lines = source.read_text(encoding="utf-8").splitlines()
    labels = {row["title"] for row in defaults}
    starts = []
    for i, line in enumerate(lines):
        match = re.match(r"  - generic: (.*)$", line)
        if match and dom_scalar(match.group(1)) in labels:
            starts.append((i, dom_scalar(match.group(1))))
    ranges = {title: lines[i + 1:(starts[j + 1][0] if j + 1 < len(starts) else len(lines))]
              for j, (i, title) in enumerate(starts)}
    settings = []
    for row in defaults:
        title = row["title"]
        if row["input_type"] == "bool":
            checkbox = next((line for line in lines if f'checkbox "{title}"' in line), None)
            raw = [] if checkbox is None else ["[checked]" in checkbox]
        else:
            raw = dom_controls(ranges.get(title, []))
        value = raw[0] if len(raw) == 1 else raw
        source_default = row["literal_value"]
        if row["input_type"] in ("int", "float") and len(raw) == 1:
            value = float(raw[0])
            if row["input_type"] == "int":
                value = int(value)
        elif row["input_type"] == "session" and len(raw) == 2:
            value = "-".join(str(v).replace(":", "") for v in raw)
        elif row["input_type"] == "timeframe" and raw == ["1 hour"]:
            value = "60"
        elif row["input_type"] == "time" and len(raw) == 2:
            value = f"{raw[0]}T{raw[1]}:00Z"
        if row["input_type"] == "time":
            same = value == {"newsAt": "2020-01-01T00:00:00Z", "testStart": "2020-01-01T00:00:00Z", "testEnd": "2035-01-01T00:00:00Z"}.get(row["name"])
        else:
            same = value == source_default
        settings.append({"name": row["name"], "title": title, "raw_ui_values": raw,
                         "normalized_value": value, "observed": bool(raw), "matches_source_default": same})
    prop_lines = props.read_text(encoding="utf-8").splitlines()
    # The chart toolbar also contains "Script execution"; scope all settings to the actual dialog.
    prop_lines = prop_lines[prop_lines.index('- dialog "QIRV ONE":'):]
    prop_labels = ["Initial capital", "Default order size", "Pyramiding", "Bar detalization", "Script execution",
                   "Commission", "Long leverage", "Short leverage", "Slippage", "Limit order execution", "Order execution delay"]
    property_rows = []
    for title in prop_labels:
        index = prop_lines.index(f"  - generic: {title}")
        next_index = next((i for i in range(index + 1, len(prop_lines)) if prop_lines[i] in [f"  - generic: {t}" for t in prop_labels]), len(prop_lines))
        property_rows.append({"label": title, "raw_ui_values": dom_controls(prop_lines[index + 1:next_index])})
    if any(not row["raw_ui_values"] for row in property_rows):
        raise ValueError("A required Properties control was not extracted")
    return {"scope": "Current original-script Inputs and Properties captured by root before diagnostic runs; not a retroactive proof of every setting in prior snapshots",
            "observed_input_count": sum(row["observed"] for row in settings), "expected_input_count": len(defaults),
            "time_input_normalization": "Date/time strings normalized to UTC using the chart's captured UTC display; raw values retained",
            "all_input_defaults_match": all(row["observed"] and row["matches_source_default"] for row in settings),
            "inputs": settings, "properties": property_rows,
            "source_files": {p.relative_to(ROOT).as_posix(): sha(p.read_bytes()) for p in (source, props)}}


def main() -> None:
    if sys.argv[1:] == ["--correct-ui-extraction"]:
        original_path = OUT / "current_ui_settings.json"
        original = json.loads(original_path.read_text(encoding="utf-8"))
        defaults = json.loads((OUT / "source_default_profile.json").read_text(encoding="utf-8"))["inputs"]
        corrected = capture_current_ui(defaults)
        if original["inputs"] != corrected["inputs"] or original["source_files"] != corrected["source_files"]:
            raise ValueError("Correction unexpectedly changes input values or source evidence")
        write_new("current_ui_settings_v2.json", corrected)
        write_new("registration_amendment_01.json", {
            "created_at_utc": datetime.now(timezone.utc).isoformat(), "type": "EVIDENCE_EXTRACTION_CORRECTION_ONLY",
            "reason": "The first parser found the chart toolbar's Script execution label instead of its Properties-dialog label. The saved evidence itself already contains On bar close.",
            "original_registration_sha256": sha((OUT / "preregistration.json").read_bytes()),
            "superseded_extraction_sha256": sha(original_path.read_bytes()),
            "corrected_extraction": "current_ui_settings_v2.json",
            "corrected_extraction_sha256": sha((OUT / "current_ui_settings_v2.json").read_bytes()),
            "changed_properties": [{"label": "Script execution", "old_values": [], "corrected_values": ["On bar close"]}],
            "input_values_changed": False, "source_evidence_changed": False, "protocol_or_rule_changes": [],
            "new_diagnostic_outcomes_reviewed_for_this_correction": False,
        })
        print(json.dumps({"corrected": "current_ui_settings_v2.json", "all_inputs_observed": corrected["observed_input_count"],
                          "all_inputs_match_defaults": corrected["all_input_defaults_match"], "properties_extracted": len(corrected["properties"])}))
        return
    now = datetime.now(timezone.utc).isoformat()
    runtime_path = "unified/reports/runtime_summary.json"
    runtime = json.loads(blob(runtime_path))
    comparison = runtime["same_chart_m15_visible_comparison"]
    evidence_paths = [runtime_path, "unified/reports/tradingview/baseline_M15.txt",
                      "unified/reports/tradingview/final_M15.txt", "unified/reports/tradingview/baseline_properties.txt",
                      "unified/reports/tradingview/final_properties.txt", "unified/reports/tradingview/execution_settings.png",
                      "unified/reports/tradingview/csv_blocked_and_trade_sample.txt"]
    write_new("baseline_evidence.json", {
        "created_at_utc": now, "baseline_commit": BASELINE,
        "scope": "Previously saved real TradingView observations, not a new runtime rerun",
        "symbol": "OANDA:XAUUSD", "timeframe": "15", "chart_type": "standard Candles",
        "original_loaded_date_label": comparison["loaded_period_display"],
        "baseline_observation_clock_utc": "2026-11-09T02:36:12Z",
        "unified_observation_clock_utc": "2026-11-09T02:45:23Z",
        "clock_qualification": "Visible chart clock in the saved snapshot; not the exact loaded-data boundary",
        "original_baseline_visible_aggregates": comparison["baseline"],
        "saved_unified_visible_aggregates": comparison["final_unified"],
        "metric_units": {"closed_parts": "Native Strategy Tester closed partial trades, not complete 3-part campaigns",
                         "entry_family_net_usd": "Contribution within combined strategy, not independent isolated-model runs"},
        "properties_observed_in_saved_ui": {
            "initial_capital_usd": 100000, "default_order_quantity": 3, "pyramiding": 0,
            "commission_per_contract_per_fill_usd": 0.175, "slippage_ticks": 2,
            "long_leverage": "1x", "short_leverage": "1x", "bar_detail": "Default (4 ticks per bar)",
            "script_execution": "On bar close", "limit_order_execution": "Requested price", "order_execution_delay": "One tick"},
        "configuration_reported_by_prior_runtime_summary": {
            "native_portfolio": runtime["execution_configuration"]["native_portfolio"],
            "model_selection": runtime["execution_configuration"]["model_selection"],
            "units_per_partial": runtime["execution_configuration"]["units_per_partial"]},
        "not_established_by_prior_evidence": [
            "Exact OHLC dataset and SHA-256", "Loaded bar count and exact first loaded timestamp",
            "Complete Inputs-tab snapshot including any saved UI overrides",
            "Exact warmup/HTF history and initialization identical across runs",
            "Complete candidate/event/trade export and event-by-event parity",
            "Independent Retest/Fast/Reversal runs", "Full per-campaign cash/R reconciliation",
            "Future or untouched holdout performance", "Improved profitability"],
        "limitations": comparison["limitations"],
        "performance_improvement_claim": False,
        "evidence_sha256_at_baseline": {path: sha(blob(path)) for path in evidence_paths},
    })
    defaults = []
    source_paths = subprocess.check_output(["git", "ls-tree", "-r", "--name-only", BASELINE, "unified/src"], cwd=ROOT).decode().splitlines()
    for path in source_paths:
        for line_number, line in enumerate(blob(path).decode("utf-8").splitlines(), 1):
            match = re.match(r"^(bool|int|float|string)\s+(\w+)\s*=\s*input\.(\w+)\((.*)", line)
            if not match:
                continue
            expression = first_argument(match.group(4))
            title = json.loads(first_argument(match.group(4)[len(expression):].lstrip(", ")))
            try:
                value = json.loads(expression)
            except json.JSONDecodeError:
                value = None
            group = re.search(r'group="([^"]+)"', line)
            defaults.append({"name": match.group(2), "title": title, "input_type": match.group(3), "source_default_expression": expression,
                             "literal_value": value, "nonliteral_requires_pine_evaluation": value is None,
                             "group": group.group(1) if group else None, "source_path": path, "source_line": line_number})
    write_new("source_default_profile.json", {
        "created_at_utc": now, "baseline_commit": BASELINE,
        "status": "SOURCE_DEFAULTS_ONLY_NOT_PROOF_OF_PRIOR_UI_VALUES",
        "purpose": "Exact default-profile reference. Actual inputs used in each diagnostic runtime must be captured separately.",
        "declared_input_count": len(defaults), "inputs": defaults,
        "new_protocol_common_overrides": {"testStart": "2026-08-03T00:00:00Z", "testEnd": "2026-11-09T02:45:00Z"},
        "isolated_run_only_difference": {"B-ALL": "Всички", "B-RETEST": "Ретест", "B-FAST": "Бърз пробив", "B-REVERSAL": "Ранно обръщане"},
        "native_profile": {"nativePortfolio": "RAW", "nativeUnitQty": 1.0, "flattenInvalidNativeFill": True},
    })
    current_ui = capture_current_ui(defaults)
    write_new("current_ui_settings.json", current_ui)
    file_names = ["EVALUATION_PROTOCOL_BG.md", "baseline_manifest.json", "baseline_evidence.json", "source_default_profile.json", "current_ui_settings.json"]
    write_new("preregistration.json", {
        "registered_at_utc": now, "baseline_commit": BASELINE,
        "baseline_tree": "acfdae17c3414ee7479f1a779c23511fe99d5680",
        "branch": "research/qirvanta-signal-diagnostics", "protocol_version": 1,
        "scope": "Frozen diagnostic protocol before rule edits by this agent; historical aggregate results were already observed",
        "genuine_future_holdout_available": False,
        "rule_changes_in_this_protocol": [], "hash_algorithm": "SHA-256",
        "files": {name: sha((OUT / name).read_bytes()) for name in file_names},
        "amendment_policy": "Do not overwrite this registration or protocol after reviewing new diagnostic results; add a dated amendment stating the reason and prior knowledge",
    })
    print(json.dumps({"registered_at_utc": now, "source_input_defaults": len(defaults), "observed_current_ui_inputs": current_ui["observed_input_count"],
                      "all_current_ui_input_defaults_match": current_ui["all_input_defaults_match"], "protocol_sha256": sha((OUT / file_names[0]).read_bytes())}))


if __name__ == "__main__":
    main()
