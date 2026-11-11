from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from research.donchian_20_10.history.lean_sample import (
    Quote, audit_daily_utc, audit_tradingview_overlap, read_archive, session_bars,
)
from research.donchian_20_10.replay import utc_ms


def quotes(open_utc, hours):
    opening = datetime.fromisoformat(open_utc)
    return [
        Quote(opening + timedelta(hours=i), (100 + i, 103 + i, 99 + i, 101 + i),
              (102 + i, 105 + i, 101 + i, 103 + i))
        for i in range(hours)
    ]


class TestLeanSample(unittest.TestCase):
    def test_observed_sunday_reopen_preserves_nominal_ny_dst_boundary(self):
        frames, coverage = session_bars(quotes("2016-03-13T23:00:00+00:00", 22))
        self.assertEqual(coverage["accepted_open_hours_ny"], {"Sunday 19:00": 1})
        self.assertEqual(coverage["omitted_sessions"], [])
        self.assertEqual(len(frames["H4"]), 6)
        day, = frames["D1"]
        self.assertEqual(day.time_ms, utc_ms("2016-03-13T21:00:00Z"))
        self.assertEqual(day.close_ms, utc_ms("2016-03-14T21:00:00Z"))
        self.assertEqual(day.open, 101)
        self.assertEqual(frames["H4"][0].close_ms, utc_ms("2016-03-14T01:00:00Z"))
        self.assertEqual(frames["H4"][0].high, 105)

    def test_missing_hours_exclude_the_same_session_in_both_frames(self):
        session = quotes("2016-01-04T23:00:00+00:00", 23)
        later = quotes("2016-01-06T23:00:00+00:00", 23)
        frames, coverage = session_bars(session[:5] + session[6:] + later)
        self.assertEqual(len(frames["D1"]), 1)
        self.assertEqual(len(frames["H4"]), 6)
        self.assertEqual(
            [x["reason"] for x in coverage["omitted_sessions"]],
            ["MISSING_OR_SHORT_HOURS", "NO_QUOTE_HOURS"],
        )

    def test_weekday_seventeen_with_quotes_is_not_discarded(self):
        frames, coverage = session_bars(quotes("2016-01-04T22:00:00+00:00", 24))
        self.assertEqual(coverage["accepted_open_hours_ny"], {"Monday–Thursday 17:00": 1})
        self.assertEqual(len(frames["H4"]), 6)
        self.assertEqual(frames["D1"][0].close, 125)

    def test_daily_validation_uses_utc_labels_without_splicing_prices(self):
        hour = quotes("2016-03-14T00:00:00+00:00", 3)
        daily = [Quote(hour[0].at, (100, 105, 99, 103), (102, 107, 101, 105))]
        check = audit_daily_utc(hour, daily)
        self.assertEqual(check["matched_utc_days"], 1)
        self.assertEqual(check["different_utc_days"], [])
        self.assertFalse(check["in_replay"])

    def test_rejects_unpinned_download(self):
        with TemporaryDirectory() as scratch:
            file = Path(scratch) / "not-archive.zip"
            file.write_bytes(b"unexpected bytes")
            with self.assertRaisesRegex(ValueError, "Unexpected SHA-256"):
                read_archive(file, "hour")

    def test_native_comparison_uses_raw_open_and_shared_timestamp(self):
        frames, _ = session_bars(quotes("2016-01-04T23:00:00+00:00", 23))
        day, = frames["D1"]
        audit = audit_tradingview_overlap(
            frames["D1"], [{"entry_ms": day.time_ms + 1, "open": 101},
                           {"entry_ms": day.time_ms, "open": 102, "price": 101}],
        )
        self.assertEqual(audit["shared_d1_native_fill_timestamps"], 1)
        self.assertEqual(audit["matching_prices_at_1e_minus_9"], 0)
        self.assertEqual(audit["median_absolute_difference_price"], 1)
