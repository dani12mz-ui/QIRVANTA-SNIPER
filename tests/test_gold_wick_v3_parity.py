"""Cover behavioral differences that a visual migration must detect."""

import csv
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from gold_wick.v3_parity import PRESENTATION, START, audit, check_source, compare_export, digest
from unified.tools.build_gold_wick import DECLARATIONS, fragment


INTERVAL = 300_000
NEXT = START + INTERVAL
CAMPAIGN = "WICK_BREAKOUT:1:1794268500000:1794268800000"


def context(known):
    return (known, ("WICK_CTX", "1", "OANDA:XAUUSD", "5", str(known - INTERVAL), str(known),
                    "4000", "4006", "3990", "4000", "tick", "100", "10", "NA", "NA", "NA",
                    "NA", "NA", "0.001", "1", "0", "0", "100", "20"))


def execution(known, state, price, units, reason, net="NA"):
    return (known, ("WICK_EXEC", "1", str(known), state, CAMPAIGN, str(START), price, units, reason, net))


def sample_run(net="-32.1"):
    event = ("WICK_EVT", "1", str(START), "ENTRY_VALID", "WF:1:1794268500000",
             "1", "1794268500000", "4000", "3990", "1", "1", "4", "1", "1", str(START),
             "PRICE_AND_RISK_VALID", "1794268500000")
    plan = ("WICK_PLAN", "1", str(START), CAMPAIGN, "WICK_BREAKOUT", "1", "4000", "3990", "4005", "4010",
            "4015", "10", "NA", "1", "1", "0", "0", "OK", "OK")
    return [
        context(START), (START, event), (START, plan), execution(START, "SUBMITTED", "4000", "3", "WICK_BREAKOUT"),
        context(NEXT), execution(NEXT, "FILLED", "4000", "3", "NATIVE_FILL"),
        *(execution(NEXT, "EXIT_PART", "3990", "1", "SL", "-10.7") for _ in range(3)),
        execution(NEXT, "CLOSED", "NA", "0", "CAMPAIGN", net),
    ]


def write_export(path, rows):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("Date", "Message"))
        for known, fields in rows:
            opening = datetime.fromtimestamp((known - INTERVAL) / 1000, tz=timezone.utc)
            writer.writerow((opening.isoformat(), "|".join(fields)))


class NativeExportParityTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        folder = Path(temporary.name)
        self.baseline = folder / "reference.csv"
        self.candidate = folder / "candidate.csv"
        write_export(self.baseline, sample_run())
        self.pin = digest(self.baseline.read_bytes())

    def compare(self, rows):
        write_export(self.candidate, rows)
        return compare_export(self.baseline, self.candidate, 5, self.pin)

    def test_matches_full_native_campaign_with_three_stop_parts(self):
        report = self.compare(sample_run())
        self.assertEqual(report["status"], "VERIFIED")
        self.assertTrue(report["raw_export_bytes_equal"])
        self.assertEqual(report["native_campaigns"], 1)
        self.assertEqual(report["execution_states"]["EXIT_PART"], 3)
        self.assertEqual(report["closed_campaign_net_usd"], "-32.1")

    def test_reports_first_difference_for_one_changed_closed_net(self):
        report = self.compare(sample_run("-32.101"))
        self.assertEqual(report["status"], "INCONCLUSIVE")
        first = report["first_difference"]
        self.assertEqual(first["candidate_tag"], "WICK_EXEC")
        self.assertEqual(first["different_field_indexes"], [9])
        self.assertEqual(first["candidate_close_ms"], NEXT)

    def test_reports_a_missing_last_row(self):
        report = self.compare(sample_run()[:-1])
        self.assertEqual(report["status"], "INCONCLUSIVE")
        self.assertEqual(report["first_difference"]["reference_tag"], "WICK_EXEC")
        self.assertNotIn("candidate_tag", report["first_difference"])

    def test_rejects_an_export_against_a_modified_reference(self):
        write_export(self.candidate, sample_run())
        report = compare_export(self.baseline, self.candidate, 5, "0" * 64)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertIn("checksum", report["problem"])

    def test_reference_folder_cannot_be_used_as_candidate_evidence(self):
        report = audit(self.baseline.parent, self.baseline.parent)
        self.assertEqual(report["status"], "BLOCKED")
        self.assertIn("separate native export directories", report["problem"])


class PineSourceParityTest(unittest.TestCase):
    def setUp(self):
        self.engine = fragment("engine")
        self.execution = fragment("strategy_execution")
        self.declaration = DECLARATIONS["strategy"].replace("Q WICK Risk", "QIRVANTA, V3")

    def source(self, display, declaration=None, engine=None):
        return ("//@version=6\n" + (declaration or self.declaration) + "\n// Generated.\n\n" +
                (engine or self.engine) + "\n\n" + self.execution + "\n\n" +
                PRESENTATION + "\n" + display + "\n").encode("utf-8")

    def check(self, candidate):
        return check_source(candidate, "strategy", self.engine, self.execution)

    def test_accepts_visual_declaration_changes_and_keeps_disabled_alerts(self):
        declaration = self.declaration.replace("max_labels_count=80", "max_labels_count=90")
        report = self.check(self.source('string caption = "strategy.close()" // alert("fake")', declaration))
        self.assertEqual(report["status"], "VERIFIED")
        self.assertEqual(report["order_calls_with_alerts_disabled"], 5)

    def test_rejects_a_single_cost_change_in_the_declaration(self):
        declaration = self.declaration.replace("commission_value=0.35", "commission_value=0.36")
        with self.assertRaisesRegex(ValueError, "behavioral declaration differs"):
            self.check(self.source("int value = 1", declaration))

    def test_rejects_changed_engine_prefix(self):
        engine = self.engine.replace("WF_ATR_BARS = 14", "WF_ATR_BARS = 15")
        with self.assertRaisesRegex(ValueError, "prefix differs"):
            self.check(self.source("int value = 1", engine=engine))

    def test_rejects_new_alerts_orders_or_updates_to_engine_state(self):
        for text in ('alert("changed")', 'strategy.close("changed")', 'wfActive.top := close',
                     'WickFormation view = wfActive\nview.top := close'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.check(self.source(text))


if __name__ == "__main__":
    unittest.main()
