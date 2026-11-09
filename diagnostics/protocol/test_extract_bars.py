"""Strict export parsing tests; fixtures are synthetic, not market evidence."""
import csv
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import unittest
from extract_bars import BASELINE_SHA, COLUMNS, INTERVAL_MS, archive, canonical_csv, inspect, parse_export
from capture_protocol_record import dom_controls


def export(rows, *, end=True, count=None):
    run = {"schema": "qirvanta.native.v1", "kind": "run", "run_id": "baseline|Всички", "symbol": "OANDA:XAUUSD",
           "timeframe": "15", "source_sha256": BASELINE_SHA, "first_bar_time": rows[0][0], "last_bar_open": rows[-1][0],
           "cutoff": rows[-1][1], "bar_count": len(rows) if count is None else count}
    snap = {"schema": "qirvanta.native.v1", "kind": "snapshot", "run_id": "baseline|Всички", "at_time": rows[-1][1]}
    messages = ["QIRVANTA_DATA_BEGIN\n" + json.dumps(run), json.dumps(snap) + "\nQIRVANTA_DATA_END",
                "QIRVANTA_BARS_BEGIN\n" + json.dumps({"columns": COLUMNS, "price_serialization": "8 decimal places"}),
                "\n".join(json.dumps(row) for row in rows)]
    if end:
        messages.append("QIRVANTA_BARS_END")
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(["Date", "Message"])
    writer.writerows(("2026-11-09T02:30:00-00:00", message) for message in messages)
    return buffer.getvalue().encode("utf-8")


def bar(opening):
    return [opening, opening + INTERVAL_MS, 4000.12345678, 4001, 3999, 4000.5, 10, 4000, 3990, 3980, 3989, None, None]


class ExportIntegrity(unittest.TestCase):
    def test_multiline_csv_preserves_decimal_and_causal_columns(self):
        parsed = parse_export(export([bar(0), bar(INTERVAL_MS)]))
        self.assertEqual(parsed["rows"][0][2], Decimal("4000.12345678"))
        self.assertIn(b"4000.12345678", canonical_csv(parsed["rows"]))
        self.assertEqual(inspect(parsed)["status"], "PASS_OBSERVED_SERIALIZED_EXPORT")

    def test_truncated_export_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "truncated"):
            parse_export(export([bar(0)], end=False))

    def test_duplicates_are_reported_and_never_dropped(self):
        parsed = parse_export(export([bar(0), bar(0)]))
        result = inspect(parsed)
        self.assertEqual(result["bar_count"], 2)
        self.assertEqual(len(result["duplicate_timestamps"]), 1)
        self.assertEqual(result["status"], "INVALID_EXPORT")

    def test_count_disagreement_is_not_passed_as_complete(self):
        self.assertIn("export_count_matches_observed_counter", inspect(parse_export(export([bar(0)], count=2)))["failed_checks"])

    def test_candidate_requires_its_explicit_expected_source_hash(self):
        parsed = parse_export(export([bar(0)]))
        parsed["run"]["source_sha256"] = "a" * 64
        self.assertIn("expected_strategy_source_hash", inspect(parsed)["failed_checks"])
        self.assertEqual(inspect(parsed, "a" * 64)["status"], "PASS_OBSERVED_SERIALIZED_EXPORT")

    def test_observed_gap_remains_gap_not_imputed_bars(self):
        parsed = parse_export(export([bar(0), bar(3 * INTERVAL_MS)]))
        result = inspect(parsed)
        self.assertEqual(result["bar_count"], 2)
        self.assertEqual(result["gaps"][0]["m15_slots_without_observed_bars"], 2)
        self.assertEqual(result["gaps"][0]["classification"], "SHORT_SESSION_BREAK_CANDIDATE")

    def test_invalid_ohlc_is_rejected_before_archiving(self):
        row = bar(0)
        row[3] = 3900
        with self.assertRaisesRegex(ValueError, "OHLC"):
            parse_export(export([row]))

    def test_existing_archive_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            source = base / "actual.csv"
            source.write_bytes(export([bar(0)]))
            archive(source, base / "dataset")
            with self.assertRaisesRegex(ValueError, "overwrite"):
                archive(source, base / "dataset")

    def test_active_and_unfocused_controls_extract_the_same_value(self):
        for state in ("", " [active]", " [active] [expanded]"):
            self.assertEqual(dom_controls([f"  - combobox{state}:", "    - generic: Ретест"]), ["Ретест"])


if __name__ == "__main__":
    unittest.main()
