"""Exercise the causal archive comparison where a reclaim changes execution."""

import unittest

from gold_pro.replay import Costs, Outcome, Part, Plan
from gold_wick.research import fail_exit, paired_event_timing


class WickResearchTests(unittest.TestCase):
    def setUp(self):
        self.plan = Plan("WICK|1|1", "WICK_BREAKOUT", 1, 0, 4100, 4090, (4110, 4120, 4130), 8, 72)
        self.bars = [{"open": 4100}, {"open": 4101}, {"open": 4105}, {"open": 4098}, {"open": 4090}]
        self.tp1 = Part(2, 4110, 1, "TP1", .35, 8.3)
        self.stop = Part(4, 4089.998, 1, "STOP", .35, -11.702)
        self.original = Outcome(self.plan, "CLOSED", 4, 1, 4101, (self.tp1, self.stop, self.stop))

    def test_reclaim_preserves_part_already_executed_and_waits_for_open(self):
        actual = fail_exit(self.original, 2, self.bars, Costs())
        self.assertEqual((actual.entry_bar, actual.fill, actual.release), (1, 4101, 3))
        self.assertEqual(actual.exits[0], self.tp1)
        self.assertEqual(sum(p.units for p in actual.exits), self.plan.units)
        for part in actual.exits[1:]:
            self.assertEqual((part.reason, part.bar), ("RECLAIM_NEXT_OPEN", 3))
            self.assertAlmostEqual(part.price, 4097.998)
            self.assertAlmostEqual(part.net, -3.702)

    def test_reclaim_before_fill_after_exit_or_missing_has_no_effect(self):
        for bar in (None, 0, 4, 5):
            self.assertEqual(fail_exit(self.original, bar, self.bars, Costs()), self.original)

    def test_opening_stop_preserves_its_actual_settlement(self):
        gap = Part(3, 4088.998, 1, "STOP_GAP", .35, -12.702)
        outcome = Outcome(self.plan, "CLOSED", 3, 1, 4101, (self.tp1, gap, gap))
        self.assertEqual(fail_exit(outcome, 2, self.bars, Costs()), outcome)

    def test_event_timing_compares_only_a_shared_label_and_known_stage(self):
        fields = {"watch_at": None, "confirmed_at": None, "executable_at": None, "fill_at": None}
        base = [{"id": "a", **fields, "watch_at": 600_000}, {"id": "b", **fields, "watch_at": 0}]
        wick = [{"id": "a", **fields, "watch_at": 300_000}, {"id": "c", **fields, "watch_at": 900_000}]
        result = paired_event_timing(base, wick)
        self.assertEqual(result["watch_at"], {"same_events": 1, "candidate_earlier": 1,
                                             "candidate_later": 0,
                                             "median_candidate_minus_reference_minutes": -5})
        self.assertIsNone(result["executable_at"]["median_candidate_minus_reference_minutes"])


if __name__ == "__main__":
    unittest.main()
