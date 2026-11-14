import unittest

from gold_pro.direct_breakout_research import BASE, STRESS
from gold_pro.replay import Plan
from research.gold_end_to_end.replay import execute, open_gate


def market():
    step = 900_000
    return [{"time": step * k, "time_close": step * (k + 1),
             "open": 4000., "high": 4003., "low": 3998., "close": 4000.} for k in range(266)]


class ReplayTests(unittest.TestCase):
    def test_cost_gates_recalculate_eligibility_and_lot_grid(self):
        plan = Plan("P", "M_CROSS", 1, 250, 2000., 1995., (2005., 2010., 2015.), 4., 48)
        bars = [{**row, **{k: row[k] - 2000. for k in ("open", "high", "low", "close")}} for row in market()]
        reason, normal = open_gate(plan, bars, BASE, 100_000.)
        self.assertIsNone(reason)
        self.assertLessEqual(normal.units * (5. + BASE.spread / 2 + BASE.slip * 2 + BASE.commission * 2), 250.)
        self.assertAlmostEqual(normal.units / 3 / .001, round(normal.units / 3 / .001))
        self.assertEqual(open_gate(plan, bars, STRESS, 100_000.)[0], "SIGNAL_COST_GATE")

    def test_entry_bar_ambiguity_sl_precedes_targets_and_slot_changes(self):
        bars = market()
        bars[251].update(high=4011., low=3989.)
        plan = Plan("P", "M_HOLD", 1, 250, 4000., 3990., (4010., 4020., 4030.), 6., 48)
        out, later = execute([plan, Plan("Q", "M_HOLD", -1, 251, 4000., 4010.,
                                       (3990., 3980., 3970.), 6., 48)], bars, BASE)
        self.assertEqual(out.entry_bar, 251)
        self.assertEqual(out.state, "CLOSED")
        self.assertEqual([row["reason"] for row in out.exits], ["SL"] * 3)
        self.assertLess(out.net, 0.)
        self.assertEqual(later.entry_bar, 252)

    def test_single_slot_and_cutoff_remain_explicit(self):
        plan = Plan("P", "M_HOLD", 1, 250, 4000., 3990., (4010., 4020., 4030.), 6., 48)
        earlier, later = execute([plan, Plan("Q", "M_HOLD", -1, 251, 4000., 4010.,
                                             (3990., 3980., 3970.), 6., 48)], market(), BASE)
        self.assertEqual(earlier.state, "CENSORED_OPEN")
        self.assertEqual(later.state, "POSITION_OCCUPIED")
        self.assertEqual(later.blocked_by, "P")


if __name__ == "__main__":
    unittest.main()
