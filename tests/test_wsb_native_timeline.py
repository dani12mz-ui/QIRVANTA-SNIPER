"""Evidence guardrails for the WSB native timeline join."""

import csv
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from gold_pro.rc1_qa import EvidenceError
from research.wsb_missed_sell import native_timeline as native


FIRST = native.utc_ms("2026-11-11T09:00:00Z")
STEP = 900_000
STAMP = FIRST + 2 * STEP


def original_bar(index, **extra):
    return {"symbol": native.SYMBOL, "timeframe": "15", "first_open_ms": FIRST,
            "open_ms": FIRST + index * STEP, "close_ms": FIRST + (index + 1) * STEP, "index": index,
            "open": 4053, "high": 4055, "low": 4048, "close": 4049, "volume": 500,
            "bear_signal": False, "bull_signal": False, **extra}


class WsbNativeTimelineTest(unittest.TestCase):
    def test_utc_offset_normalization_preserves_instant(self):
        self.assertEqual(native.utc_ms("2026-11-11T10:00:00+01:00", allow_offset=True), FIRST)
        with self.assertRaisesRegex(EvidenceError, "explicit UTC"):
            native.utc_ms("2026-11-11T10:00:00+01:00")
        with self.assertRaisesRegex(EvidenceError, "explicit offset"):
            native.utc_ms("2026-11-11T10:00:00", allow_offset=True)

    def test_original_plan_points_to_known_formation_not_its_visual_start(self):
        first, final = original_bar(0), original_bar(1, bear_signal=True)
        formation = {key: first[key] for key in ("open_ms", "close_ms", "index")}
        formation.update(type="MIXED", left_index=0, top=4058, bottom=4052)
        second_stamp = {key: final[key] for key in ("open_ms", "close_ms", "index")}
        broken = {**second_stamp, "formed_index": 0, "top": 4058, "bottom": 4052,
                  "direction": "SELL", "reason": "SIGNAL"}
        plan = {**second_stamp, "direction": "SELL", "entry": 4049, "sl": 4058,
                "tp1": 4040, "tp2": 4031, "tp3": 4022}
        rows = [("WSB_ORIGINAL_SETTINGS", {"version": 1}), ("WSB_ORIGINAL_CTX", first),
                ("WSB_ORIGINAL_FORMATION", formation), ("WSB_ORIGINAL_CTX", final),
                ("WSB_ORIGINAL_RESOLUTION", broken), ("WSB_ORIGINAL_PLAN", plan)]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "original_M15.csv"
            with path.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(("Date", "Message"))
                for tag, row in rows:
                    writer.writerow((native.utc(row.get("open_ms", FIRST)), f"{tag}|{json.dumps(row)}"))
            run = native.read_lane(path, "original", 15)
            actual = native.original_signal(run, STAMP)
            self.assertEqual(actual["plans"][0]["entry"], 4049)
            self.assertEqual(actual["resolutions"][0]["formation_known_ms"], FIRST + STEP)
            self.assertEqual(actual["formations"][0]["type"], "MIXED")
            report, _ = native.frame_report(Path(directory), 15, FIRST + STEP, STAMP, STAMP)
            self.assertEqual(report["lanes"]["s5"]["meta"]["status"], "MISSING_EVIDENCE")
            self.assertEqual(report["lanes"]["original"]["meta"]["status"], "PARSED")

    def test_ohlcv_match_keeps_loaded_start_discrepancy_visible(self):
        market = {"open_ms": STAMP - STEP, "close_ms": STAMP,
                  "open": 4053, "high": 4055, "low": 4048, "close": Decimal("4049.570"), "volume": 500}
        left = {"bars": {STAMP: market}, "meta": {"first_loaded_open_ms": FIRST}}
        right = {"bars": {STAMP: dict(market)}, "meta": {"first_loaded_open_ms": FIRST - STEP}}
        result = native.compare_lanes(left, right, (STAMP,))
        self.assertEqual(result["status"], "MATCH")
        self.assertTrue(result["complete_window"])
        self.assertFalse(result["same_loaded_start"])
        right["bars"][STAMP]["volume"] = 501
        mismatch = native.compare_lanes(left, right, (STAMP,))
        self.assertEqual(mismatch["differences"][native.utc(STAMP)]["volume"], [500, 501])

    def test_higher_timeframe_data_must_be_known_by_chart_open(self):
        bar = original_bar(1)
        bar.update(schema=1, kind="s5", bar_index=1, h1_close_ms=STAMP)
        rows = {"WSB_FORENSIC": [(FIRST + STEP, json.dumps(bar))]}
        with self.assertRaisesRegex(EvidenceError, "future h1_close_ms"):
            native.context(rows, 15, "s5")


if __name__ == "__main__":
    unittest.main()
