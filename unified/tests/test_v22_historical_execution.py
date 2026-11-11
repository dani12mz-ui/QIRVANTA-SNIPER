import json
import unittest
from decimal import Decimal

from unified.tools.evaluate_v22_execution import Campaign, Plan, replay, simulate
from unified.tools.evaluate_v22_history import REGISTRATION


def bar(i, op, high, low, close):
    return {"time": i * 300_000, "time_close": (i + 1) * 300_000, "open": op, "high": high,
            "low": low, "close": close}


class V22HistoricalExecutionTests(unittest.TestCase):
    def setUp(self):
        self.execution = json.loads(REGISTRATION.read_bytes())["execution"]
        self.plan = Plan("A-BUY-1|1", "A", 1, 0, 300_000, 300_000, 100, 98, (102, 104, 106),
                         2, 2, .25, .75)

    def simulate(self, bars, policy="FIXED"):
        return simulate(self.plan, bars, [None] * len(bars), self.execution, .001, Decimal(1), 5, policy)

    def test_ambiguous_intrabar_protection_first(self):
        closed = self.simulate([bar(0, 100, 100, 100, 100), bar(1, 100, 104, 97, 101)])
        self.assertEqual([e.reason for e in closed.exits], ["STOP"] * 3)
        self.assertEqual([e.price for e in closed.exits], [97.998] * 3)

    def test_cost_be_applies_after_tp1_and_on_later_tick(self):
        bars = [bar(0, 100, 100, 100, 100), bar(1, 100, 102.5, 99.6, 102),
                bar(2, 101.4, 101.5, 100.5, 101)]
        closed = self.simulate(bars, "COST_BE")
        self.assertEqual([(e.reason, e.bar) for e in closed.exits], [("TP1", 1), ("STOP", 2), ("STOP", 2)])
        self.assertEqual(closed.stop_changes[0]["known_close"], 600_000)
        self.assertEqual(closed.stop_changes[0]["earliest_effective_open"], 600_000)
        self.assertEqual(closed.stop_changes[0]["new_stop"], 100.704)

    def test_gap_invalidation_flattens_only_remaining_parts_next_open(self):
        bars = [bar(0, 100, 100, 100, 100), bar(1, 101.5, 102.4, 101.4, 102),
                bar(2, 101, 101.1, 100.6, 100.8)]
        closed = self.simulate(bars)
        self.assertTrue(closed.gap_invalid)
        self.assertEqual([(e.reason, e.bar) for e in closed.exits],
                         [("TP1", 1), ("GAP_INVALID", 2), ("GAP_INVALID", 2)])

    def test_next_open_capital_failure_cannot_release_slot_at_previous_close(self):
        def offer(model, i, fill):
            return Campaign(model, model, 1, i, (i + 1) * 300_000, i + 1, (i + 1) * 300_000,
                            fill, Decimal(3), Decimal(3), 98, 98, (102, 104, 106), (),
                            False, False, ())
        bars = [bar(0, 100, 100, 100, 100), bar(1, 101, 101, 99, 99), bar(2, 99, 99, 99, 99)]
        accepted, rejected = replay([offer("A", 0, 101), offer("B", 0, 99), offer("C", 1, 99)],
                                    bars, 300, Decimal(1))
        self.assertEqual([x.engine for x in accepted], ["A", "C"])
        self.assertTrue(accepted[0].cancelled_at_entry)
        self.assertEqual([(x["model"], x["reason"], x["bar"]) for x in rejected],
                         [("A", "CAPITAL_AT_FILL", 1), ("B", "PORTFOLIO_PRIORITY", 0)])


if __name__ == "__main__":
    unittest.main()
