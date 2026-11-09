#!/usr/bin/env python3
"""Catalog exact captured evidence with narrow statuses; no simulated delivery claims."""
from datetime import datetime, timezone
import json
from pathlib import Path
from extract_bars import ROOT, sha
from capture_protocol_record import write_new


def evidence(path):
    target = ROOT / path
    return {"path": path, "sha256": sha(target.read_bytes()), "bytes": target.stat().st_size}


def item(status, claim, paths, limits):
    return {"status": status, "observed_scope": claim, "limitations": limits, "evidence": [evidence(path) for path in paths]}


def main():
    record = {"created_at_utc": datetime.now(timezone.utc).isoformat(), "scope": "Snapshot catalog of actual captured evidence; financial hypothesis results remain in diagnosis/accounting reports",
              "baseline_commit": "48e5cddab17ea831bdc277f17f0406329fdfe39c", "items": {
        "baseline_identity": item("PASS", "Every one of the 217 tracked original files frozen; existing 99+127 tests passed without replacing their old reports.",
            ["diagnostics/protocol/baseline_manifest.json", "diagnostics/protocol/baseline_tests.json"], ["Python/static/reference tests are not Pine compilation or a profitability test."]),
        "baseline_dataset": item("PASS_OBSERVED_SERIALIZED_EXPORT", "6445 actual OHLC/HTF rows from Aug 2 22:00Z through Nov 9 02:45Z; gaps preserved; canonical hashes recorded.",
            ["diagnostics/evidence/B-ALL-FULL.pine-logs.csv", "diagnostics/protocol/datasets/B-ALL-FULL/dataset_manifest.json"],
            ["Pine serialization is at most 8 decimals; original backend full precision and earlier pre-diagnostic exact dataset are not proven."]),
        "candidate_source_and_data": item("PASS", "One preregistered FAST efficiency gate plus diagnostic reason; other production logic exact. H1-ALL/H1-FAST canonical exported bars exactly equal baseline; all 108 Inputs checked.",
            ["diagnostics/diagnosis/H1_PREREGISTRATION.json", "diagnostics/diagnosis/H1_GENERATED_v2.json", "diagnostics/protocol/H1_integrity_tests.log", "diagnostics/protocol/H1_evidence_comparison.json"],
            ["Exploratory historical candidate only; not production adoption. Per-run Properties dialogs were not all recaptured; declaration and Inputs match."]),
        "informational_price_notification": item("PASS_OBSERVED_BROWSER_TOAST_AND_TRIGGERED_LOG", "One-time price>0 informational notification named QIRVANTA — TEST DELIVERY — no trade was observed as Stopped — Triggered at 05:27:22 displayed local time; toast/log message visible.",
            ["diagnostics/evidence/price_delivery_test_configuration.txt", "diagnostics/evidence/price_delivery_test_received.txt", "diagnostics/evidence/price_delivery_test_received.png"],
            ["This is not a strategy signal/native-fill notification. Actual phone push receipt, email and webhook delivery were not observed."]),
        "strategy_alert_delivery": item("NOT_CREATED_PLAN_LIMIT_0_TECHNICAL_ALERTS", "Saved platform error explicitly states Basic plan permits 0 technical alerts; strategy alert creation did not succeed.",
            ["diagnostics/evidence/script_alert_configuration.txt", "diagnostics/evidence/script_alert_notifications.txt", "diagnostics/evidence/script_alert_plan_limit.txt", "diagnostics/evidence/script_alert_plan_limit.png"],
            ["Native fill and alert() server delivery remain unverified. The successful price notification does not satisfy this check."]),
        "mobile_active_plan_replay": item("UNVERIFIED_INTRADAY_REPLAY_PLAN_LIMIT", "Platform displayed intraday Bar Replay upgrade requirement on Basic plan.",
            ["diagnostics/evidence/mobile_replay_plan_limit.txt", "diagnostics/evidence/mobile_replay_plan_limit.png"],
            ["This evidence does not demonstrate an active filled plan on a physical phone. Prior mobile preparation rendering must not be relabeled as an active-plan test."]),
    }, "live_broker_trading": False, "true_future_holdout_available": False, "profitability_claim_from_this_catalog": False}
    write_new("evidence_catalog.json", record)
    print(json.dumps({"catalog": "diagnostics/protocol/evidence_catalog.json", "items": len(record["items"])}))


if __name__ == "__main__":
    main()
