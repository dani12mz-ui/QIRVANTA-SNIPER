from datetime import datetime, timedelta
import unittest

from research.donchian_20_10.data import NY, aggregate, bounds, ms
from research.donchian_20_10.replay import Bar


def children(start, end):
    first, last = map(ms, (start, end))
    return [Bar(stamp, stamp + 1_800_000, 100, 103, 98, 101)
            for stamp in range(first, last, 1_800_000)]


class DonchianSessionAggregation(unittest.TestCase):
    def test_nominal_anchor_respects_new_york_dst(self):
        cases = (("2026-01-05T23:00:00Z", 22), ("2026-03-09T22:00:00Z", 21))
        for instant, utc_hour in cases:
            for frame in ("H4", "D1"):
                nominal, active, end = bounds(ms(instant), frame)
                self.assertEqual(nominal.hour, 17)
                self.assertEqual(nominal.astimezone(datetime.fromisoformat(instant).tzinfo).hour, utc_hour)
                self.assertEqual(active - nominal, timedelta(hours=1))
                self.assertEqual(end.hour, 21 if frame == "H4" else 17)

    def test_early_close_preserves_nominal_boundary_and_evening_reopens(self):
        early = children("2026-01-19T18:00:00Z", "2026-01-19T19:30:00Z")
        later = children("2026-01-19T23:00:00Z", "2026-01-20T02:00:00Z")
        bars, skipped, shortened = aggregate(early + later, "H4")
        self.assertEqual(len(bars), 2)
        self.assertEqual(skipped, [])
        self.assertEqual(len(shortened), 1)
        self.assertEqual(bars[0].close_ms, ms("2026-01-19T22:00:00Z"))
        self.assertEqual(bars[1].time_ms, ms("2026-01-19T22:00:00Z"))
        self.assertEqual(datetime.fromtimestamp(bars[1].time_ms / 1000, NY).hour, 17)

    def test_missing_subbar_is_excluded_without_synthesis(self):
        observations = children("2026-01-20T18:00:00Z", "2026-01-20T22:00:00Z")
        bars, skipped, shortened = aggregate(observations[:4] + observations[5:], "H4")
        self.assertEqual(bars, [])
        self.assertEqual(shortened, [])
        self.assertEqual(skipped[0]["observed_m30"], 7)
