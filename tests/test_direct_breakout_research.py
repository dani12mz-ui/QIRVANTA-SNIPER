import unittest

from gold_pro.direct_breakout_research import BASE, portfolio, simulate
from gold_pro.replay import Plan


MINUTES = 300_000


def bar(index, opening, high, low, close):
    return {
        "time": index * MINUTES, "time_close": (index + 1) * MINUTES,
        "open": opening, "high": high, "low": low, "close": close,
    }


def plan(name="long", model="SHADOW", decision=0, side=1):
    return Plan(name, model, side, decision, 100, 100 - .8 * side,
                tuple(100 + target * side for target in (1, 2, 3)), 2, 12)


class DirectBreakoutExecutionTests(unittest.TestCase):
    def test_ambiguous_later_extremes_resolve_stop_first_in_both_directions(self):
        signal = bar(0, 100, 100.3, 99.7, 100)
        ambiguous = bar(1, 100, 104, 96, 100)
        for side in (1, -1):
            with self.subTest(side=side):
                fill = simulate(plan(side=side), [signal, ambiguous], BASE)
                self.assertEqual(fill.state, "CLOSED")
                self.assertEqual(fill.entry_bar, 1)
                self.assertEqual([part["reason"] for part in fill.exits], ["SL"] * 3)
                self.assertAlmostEqual(fill.net, -fill.risk_usd)

    def test_target_crossed_at_open_precedes_intrabar_stop(self):
        prices = [bar(0, 100, 100.3, 99.7, 100), bar(1, 100, 100.3, 99.7, 100),
                  bar(2, 101.4, 104, 98, 99)]
        fill = simulate(plan(), prices, BASE)
        self.assertEqual([part["reason"] for part in fill.exits], ["TP1_AT_OPEN", "SL", "SL"])
        self.assertAlmostEqual(fill.exits[0]["price"], 101)
        self.assertAlmostEqual(fill.exits[1]["price"], 99.198)

    def test_gap_and_late_repricing_are_different_rejections(self):
        signal = bar(0, 100, 100.3, 99.7, 100)
        self.assertEqual(simulate(plan(), [signal, bar(2, 100, 100.3, 99.7, 100)], BASE).state,
                         "ENTRY_SESSION_GAP")
        bars = [signal, bar(1, 100, 100.2, 99.8, 100.1), bar(2, 100.8, 101, 100, 100.8)]
        self.assertEqual(simulate(plan(), bars, BASE, delay=2).state, "OPEN_MOVE_GT_0_20_ATR")
        self.assertEqual(simulate(plan(), bars, BASE).entry_bar, 1)

    def test_open_crosses_quote_side_stop_before_order(self):
        prices = [bar(0, 100, 100.3, 99.7, 100), bar(1, 99.3, 100, 99.3, 99.8)]
        fill = simulate(plan(), prices, BASE)
        self.assertEqual(fill.state, "OPEN_THROUGH_STOP")
        self.assertIsNone(fill.entry_bar)

    def test_rc2_reserves_slot_and_same_close_after_exit_can_submit(self):
        prices = [bar(0, 100, 100.3, 99.7, 100), bar(1, 100, 104, 96, 100),
                  bar(2, 100, 100.5, 99.5, 100)]
        outcomes = portfolio([plan("shadow"), plan("baseline", "RC2"), plan("after_exit", decision=1)], prices, BASE)
        self.assertEqual([row.plan.id for row in outcomes], ["baseline", "shadow", "after_exit"])
        self.assertEqual(outcomes[1].blocked_by, "baseline")
        self.assertEqual(outcomes[2].entry_bar, 2)


if __name__ == "__main__":
    unittest.main()
