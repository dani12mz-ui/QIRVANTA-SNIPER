import unittest
from unittest.mock import patch

from gold_pro.direct_breakout_research import BASE, STRESS, Execution, simulate
from gold_pro.direct_trend_breakout import CONSOLIDATION, RETEST, STRUCTURAL
from gold_pro.precision_entry_research import eligible, precision_portfolio, realized
from gold_pro.replay import Plan


def plan(name, bar):
    return Plan(f"{name}-{bar}", name, 1, bar, 100, 95, (105, 110, 115), 2, 30)


def bar(index, opening, high, low, close):
    return dict(time=index * 300_000, time_close=(index + 1) * 300_000,
                open=opening, high=high, low=low, close=close)


class PrecisionEntryReplayTest(unittest.TestCase):
    def test_rc2_priority_and_no_double_reservation(self):
        offers = [plan(RETEST, 1), plan(CONSOLIDATION, 1), plan("RC2", 1),
                  plan(STRUCTURAL, 3), plan(RETEST, 3), plan(STRUCTURAL, 2)]
        with patch("gold_pro.precision_entry_research.simulate",
                   side_effect=lambda offer, bars, costs, lock_r: Execution(offer, "CLOSED", offer.bar + 2)) as replay:
            results = precision_portfolio(offers, [], BASE)
        self.assertEqual([call.args[0].model for call in replay.call_args_list], ["RC2", RETEST])
        self.assertEqual([row.state for row in results],
                         ["CLOSED", "POSITION_OCCUPIED", "POSITION_OCCUPIED", "POSITION_OCCUPIED",
                          "CLOSED", "POSITION_OCCUPIED"])
        self.assertEqual(results[3].blocked_by, "RC2-1")
        self.assertEqual(results[5].blocked_by, f"{RETEST}-3")

    def test_confirmation_and_exit_use_distinct_window_rules(self):
        bars = [dict(time=i, time_close=i + 1) for i in range(7)]
        old, available, too_late = (plan(STRUCTURAL, index) for index in (1, 2, 4))
        self.assertEqual(eligible([old, available, too_late], bars, 3, 5), [available])
        outcome = Execution(available, "CLOSED", release=4, entry_bar=3)
        self.assertTrue(realized(outcome, bars, 3, 5))
        self.assertFalse(realized(outcome, bars, 3, 4))
        self.assertFalse(realized(outcome, bars, 4, 5))

    def test_same_bar_collision_resolves_stop_first_at_both_costs(self):
        bars = [bar(0, 100, 101, 99, 100), bar(1, 100, 107, 94, 102)]
        base, stress = (simulate(plan(STRUCTURAL, 0), bars, costs) for costs in (BASE, STRESS))
        self.assertEqual([part["reason"] for part in base.exits], ["SL", "SL", "SL"])
        self.assertEqual([part["reason"] for part in stress.exits], ["SL", "SL", "SL"])
        self.assertLess(stress.net, base.net)

    def test_adverse_exit_gap_and_unfavorable_entry_gap_have_different_paths(self):
        first = bar(0, 100, 101, 99, 100)
        bars = [first, bar(1, 100, 103, 98, 99), bar(2, 93, 96, 91, 92)]
        exit_gap = simulate(plan(STRUCTURAL, 0), bars, BASE)
        self.assertEqual([part["reason"] for part in exit_gap.exits], ["SL_GAP", "SL_GAP", "SL_GAP"])
        self.assertLess(exit_gap.net / exit_gap.risk_usd, -1)
        entry_gap = simulate(plan(STRUCTURAL, 0), [first, bar(1, 103, 105, 102, 103)], BASE)
        self.assertEqual(entry_gap.state, "OPEN_MOVE_GT_0_20_ATR")
        self.assertIsNone(entry_gap.entry_bar)


if __name__ == "__main__":
    unittest.main()
