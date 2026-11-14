import unittest

from gold_pro.direct_breakout_research import Execution
from gold_pro.replay import Plan
from research.net_campaign_evidence.replay_archive import exposure_at_cutoff, mark_series


class ArchiveLedgerTests(unittest.TestCase):
    def setUp(self):
        self.bars = [{"time": i * 300_000, "time_close": (i + 1) * 300_000} for i in range(5)]
        self.plan = Plan("test", "S5_RAW", 1, 0, 100., 98., (102., 104., 106.), 2., 20)
        self.outcome = Execution(
            self.plan, "CLOSED", 4, entry_bar=1, entry=100.2, risk_usd=7.2,
            exits=({"bar": 2, "net_usd": 1.45}, {"bar": 4, "net_usd": -2.5}, {"bar": 4, "net_usd": -2.5}),
            marked=((1, -1.), (2, .5), (3, -.5), (4, -3.55)),
        )

    def test_partial_liquidation_has_realized_and_open_value_without_counting_as_closed(self):
        mark = exposure_at_cutoff(self.outcome, self.bars, 900_000)
        self.assertEqual(mark["open_units"], 2)
        self.assertEqual(mark["realized_net_usd"], 1.45)
        self.assertAlmostEqual(mark["unrealized_net_usd"], -.95)
        self.assertIsNone(exposure_at_cutoff(self.outcome, self.bars, 1_500_000))
        self.assertIsNone(exposure_at_cutoff(self.outcome, self.bars, 300_000))

    def test_mark_series_carries_prefix_and_does_not_settle_future_exits(self):
        self.assertEqual(mark_series([self.outcome], self.bars, 900_000, 1_200_000),
                         [(900_000, .5), (1_200_000, -.5)])
        self.assertEqual(mark_series([self.outcome], self.bars, 0, 600_000),
                         [(0, 0.), (300_000, 0.), (600_000, -1.)])


if __name__ == "__main__":
    unittest.main()
