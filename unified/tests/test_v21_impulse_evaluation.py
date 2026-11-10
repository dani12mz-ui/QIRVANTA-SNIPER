import unittest
from decimal import Decimal

from unified.tools.evaluate_v21_impulse import (
    HOUR, Decision, Exit, Offer, Watch, hourly_events, model_offer, occupy,
)


def bar(i, op=100, high=101, low=99.5, close=100, interval=300_000):
    return {"time": i * interval, "time_close": (i + 1) * interval,
            "open": op, "high": high, "low": low, "close": close}


class V21ExecutionBoundaries(unittest.TestCase):
    def setUp(self):
        self.execution = {"position_units": 3, "market_or_stop_adverse_slippage_ticks": 2,
                          "commission_usd_per_unit_per_fill": 0.175}
        self.intent = Decision(0, 300_000, 1, "RISK_VALID", "CONTINUATION",
                               Watch(1, -1, 100, 99.9), 100, 1, 99, 102)

    def test_same_bar_stop_and_target_resolve_stop_first(self):
        bars = [bar(0), bar(1, high=103, low=98)]
        trade = model_offer(self.intent, bars, self.execution, 0.001, Decimal(1), 24)
        self.assertEqual(trade.entry_price, 100.002)
        self.assertEqual((trade.exits[0].reason, trade.exits[0].price, trade.exits[0].net),
                         ("STOP", 98.998, Decimal("-4.062")))

    def test_gap_beyond_stop_does_not_create_a_valid_realized_r_denominator(self):
        bars = [bar(0), bar(1, 98, 99, 97, 98)]
        trade = model_offer(self.intent, bars, self.execution, 0.001, Decimal(1), 24)
        self.assertIsNone(trade.initial_risk)
        self.assertEqual((trade.exits[0].reason, trade.exits[0].price, trade.exits[0].net),
                         ("STOP_GAP", 97.998, Decimal("-1.062")))

    def test_time_exit_at_next_open_after_last_holding_bar(self):
        bars = [bar(0), bar(1), bar(2), bar(3, 103, 104, 102, 103)]
        trade = model_offer(self.intent, bars, self.execution, 0.001, Decimal(1), 2)
        self.assertEqual((trade.exits[0].reason, trade.exits[0].bar, trade.exits[0].price),
                         ("TIME", 3, 102.998))

    def test_equal_time_priority_and_exit_bar_close_release(self):
        def order(name, engine, decision, release):
            return Offer(name, engine, 1, decision, decision, decision + 1, decision + 1,
                         100, 99, 102, Decimal(3), Decimal(3),
                         (Exit(release, release, 102, Decimal(3), Decimal("1.05"), Decimal("4.95"), "TARGET"),))
        tape = [order("late", "IMPULSE", 4, 7), order("first_impulse", "IMPULSE", 1, 6),
                order("first_s5", "S5", 1, 4)]
        admitted, blocked = occupy(tape, "S5")
        self.assertEqual([t.id for t in admitted], ["first_s5", "late"])
        self.assertEqual([(x["id"], x["occupant"]) for x in blocked], [("first_impulse", "first_s5")])
        admitted, blocked = occupy(tape, "IMPULSE")
        self.assertEqual([t.id for t in admitted], ["first_impulse"])
        self.assertEqual([x["id"] for x in blocked], ["first_s5", "late"])

    def test_partial_hour_is_excluded_and_outcome_does_not_set_threshold(self):
        half = HOUR // 2
        bars = [bar(i, high=101, low=99, interval=half) for i in range(250)]
        bars.append(bar(250, high=102, low=98, interval=half))
        bars.extend([bar(252, 100, 101, 99.9, 101, half), bar(253, 101, 105, 100.9, 105, half)])
        settings = {"minimum_absolute_open_to_close_multiple": 1.5,
                    "minimum_absolute_open_to_close_over_hour_range": 0.6}
        events, coverage = hourly_events(bars, 30, settings)
        self.assertEqual(coverage["partial_observed_hours"], 1)
        self.assertEqual([(e["start"], e["side"], e["scale"]) for e in events], [(126 * HOUR, 1, 2)])


if __name__ == "__main__":
    unittest.main()
