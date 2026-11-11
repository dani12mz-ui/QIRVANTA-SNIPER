from datetime import date, datetime, time, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.donchian_20_10.data import read_bars
from research.donchian_20_10.history import intake


def iso(timestamp):
    return timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def session(day, frame, count=6):
    opening = datetime.combine(day - timedelta(days=1), time(17), intake.NY)
    rows = []
    for hour in range(count if frame == "H4" else 1):
        rows.append({
            "time": iso(opening + timedelta(hours=hour * 4)),
            "complete": True, "volume": 1, "mid": {"o": "100.10", "h": "103", "l": "98", "c": "101"},
        })
    return rows


class OandaIntake(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        self.manifest_path = self.root / "source.json"
        self.output = self.root / "replay"
        self.manifest = {
            "schema": intake.SCHEMA, "environment": "practice", "retrieved_utc": "2026-11-12T12:00:00Z",
            **intake.FIXED_QUERY, "chunks": [],
        }

    def page(self, frame, rows, start="2025-01-01T00:00:00Z", end="2026-11-12T12:00:00Z"):
        raw = json.dumps({"instrument": "XAU_USD", "granularity": frame, "candles": rows}).encode()
        name = f"page-{len(self.manifest['chunks'])}.json"
        (self.root / name).write_bytes(raw)
        self.manifest["chunks"].append({
            "file": name, "granularity": frame, "from_utc": start, "to_utc": end,
            "sha256": sha256(raw).hexdigest(),
        })

    def both(self, days):
        for frame in intake.FRAMES:
            self.page(frame, [row for day in days for row in session(day, frame)])

    def prepare(self):
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        return intake.prepare(self.manifest_path, self.output)

    def test_same_source_cross_check_and_ny_dst(self):
        self.both([date(2026, 3, 6), date(2026, 3, 9)])
        report = self.prepare()
        cross = report["same_source_H4_to_D1"]
        self.assertEqual(cross["status"], "CROSS_CHECK_MATCH")
        self.assertEqual(cross["counts"], {"MATCH": 2})
        self.assertEqual(len(read_bars(self.output / "D1.csv")), 2)
        self.assertEqual(len(read_bars(self.output / "H4.csv")), 12)
        for frame in report["frames"].values():
            change, = frame["utc_offset_transitions_between_observed_opens"]
            self.assertEqual((change["offset_from_hours"], change["offset_to_hours"]), (-5, -4))
            self.assertIsNone(frame["first_replay_eligible_close_utc"])

    def test_keeps_pre_start_warmup_and_trims_final_nominal_close(self):
        days = [date(2015, 12, 31), date(2016, 1, 4), date(2026, 11, 10), date(2026, 11, 11)]
        for frame in intake.FRAMES:
            rows = [row for day in days for row in session(day, frame)]
            self.page(frame, rows, start="2015-01-01T00:00:00Z")
        report = self.prepare()
        self.assertEqual(report["chunks"]["D"][0]["excluded_after_cutoff"], 1)
        self.assertEqual(report["chunks"]["H4"][0]["excluded_after_cutoff"], 6)
        self.assertEqual(report["frames"]["D1"]["bars_closed_before_registered_start"], 1)
        self.assertEqual(report["frames"]["D1"]["last_close_utc"], "2026-11-10T22:00:00Z")

    def test_documented_holiday_closure_and_shortened_session(self):
        wed, thurs, fri = (date(2025, 11, n) for n in (26, 27, 28))
        self.page("H4", session(wed, "H4") + session(fri, "H4", count=5))
        self.page("D", session(wed, "D") + session(fri, "D"))
        source = "https://www.oanda.com/us-en/trading/holiday-trading-hours/"
        self.manifest["session_overrides"] = [
            {"date": thurs.isoformat(), "state": "closed", "source": source},
            {"date": fri.isoformat(), "state": "early_close", "close_local": "12:30", "source": source},
        ]
        cross = self.prepare()["same_source_H4_to_D1"]
        self.assertEqual(cross["counts"], {"MATCH": 2, "DOCUMENTED_CLOSURE": 1})
        shortened = cross["findings"][1]
        self.assertEqual(shortened["calendar"], "SHORTENED")
        self.assertEqual(shortened["expected_h4"], 5)

    def test_unexplained_gap_and_exact_price_mismatch_are_disclosed(self):
        first, third, fourth = (date(2026, 3, n) for n in (2, 4, 5))
        self.page("H4", session(first, "H4") + session(third, "H4") + session(fourth, "H4"))
        mismatch, = session(third, "D")
        mismatch["mid"]["h"] = "103.0001"
        self.page("D", session(first, "D") + [mismatch] + session(fourth, "D"))
        cross = self.prepare()["same_source_H4_to_D1"]
        self.assertEqual(cross["status"], "REVIEW_REQUIRED")
        self.assertEqual(cross["counts"], {"MATCH": 2, "UNEXPLAINED_MISSING_SESSION": 1, "OHLC_MISMATCH": 1})
        self.assertEqual(cross["findings"][1]["different_fields"], ["high"])

    def test_hash_and_request_ranges_are_enforced(self):
        day = date(2026, 3, 6)
        self.both([day])
        self.page("D", session(day, "D"))
        with self.assertRaisesRegex(ValueError, "ranges overlap"):
            self.prepare()
        self.assertFalse(self.output.exists())
        self.manifest["chunks"].pop()
        self.manifest["chunks"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "source hash differs"):
            self.prepare()

    def test_incomplete_terminal_candles_are_excluded(self):
        first, later = date(2026, 11, 10), date(2026, 11, 11)
        self.manifest["retrieved_utc"] = "2026-11-10T23:00:00Z"
        for frame in intake.FRAMES:
            incomplete = session(later, frame)[0]
            incomplete["complete"] = False
            self.page(frame, session(first, frame) + [incomplete])
        report = self.prepare()
        self.assertEqual(report["chunks"]["D"][0]["excluded_incomplete"], 1)
        self.assertEqual(report["chunks"]["H4"][0]["excluded_incomplete"], 1)
        self.assertEqual(report["same_source_H4_to_D1"]["counts"], {"MATCH": 1})

    def test_rejects_non_utc_off_grid_and_midpoint_impostors(self):
        day = date(2026, 3, 6)
        for bad, pattern in (
            ({"time": "2026-03-05T17:00:00-05:00"}, "UTC RFC3339"),
            ({"time": "2026-03-05T22:00:00.000000001Z"}, "UTC RFC3339"),
            ({"time": "2026-03-05T23:00:00Z"}, "off its"),
            ({"ask": {}}, "midpoint-only"),
        ):
            with self.subTest(bad=bad):
                row = {**session(day, "D")[0], **bad}
                with self.assertRaisesRegex(ValueError, pattern):
                    intake.parse_candle(row, "D")
