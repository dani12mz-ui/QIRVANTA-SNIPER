import unittest
from types import SimpleNamespace

from gold_precision.execution import book, capped_plan, policy_simulate
from gold_precision.experiments import recovery_plans
from gold_pro.replay import Costs, Plan, simulate


def candle(i, opening, high, low, close):
    return {"time": i * 300_000, "time_close": (i + 1) * 300_000,
            "open": opening, "high": high, "low": low, "close": close}


def feature(**overrides):
    return SimpleNamespace(**{"bos": 0, "mss": False, "atr": 1., "spike": False,
                              "highs": (), "lows": (), "newest_low": None,
                              "newest_high": None, **overrides})


class PrecisionExecutionTest(unittest.TestCase):
    cost = Costs()
    plan = Plan("plan", "RETEST", 1, 0, 100, 99, (102, 104, 106), 1., 48)

    def test_early_invalidation_requires_next_observed_open(self):
        bars = [candle(0, 100, 100.4, 99.8, 100),
                candle(1, 100, 100.7, 99.7, 99.8),
                candle(2, 99.7, 100.1, 99.6, 99.9)]
        features = [feature(), feature(bos=-1, mss=True), feature()]
        clipped = policy_simulate(self.plan, bars[:2], features[:2], self.cost, 5, "OPPOSITE_MSS")
        self.assertEqual(clipped.state, "CENSORED_OPEN")
        outcome = policy_simulate(self.plan, bars, features, self.cost, 5, "OPPOSITE_MSS")
        self.assertEqual([e.reason for e in outcome.exits], ["OPPOSITE_MSS"] * 3)
        self.assertEqual([e.bar for e in outcome.exits], [2, 2, 2])
        self.assertAlmostEqual(outcome.exits[0].price, 99.698)

    def test_break_even_cannot_affect_a_previous_intrabar_extreme(self):
        bars = [candle(0, 100, 100.2, 99.8, 100),
                candle(1, 100, 102.2, 99.4, 101.8),
                candle(2, 100.4, 101.0, 100.1, 100.6)]
        outcome = policy_simulate(self.plan, bars, [feature()] * 3, self.cost, 5, "COST_BE")
        self.assertEqual([(e.bar, e.reason) for e in outcome.exits],
                         [(1, "TP1"), (2, "STOP_GAP"), (2, "STOP_GAP")])
        self.assertAlmostEqual(outcome.exits[1].price, 100.398)

    def test_stop_precedes_same_bar_partial_targets(self):
        bars = [candle(0, 100, 100.2, 99.8, 100), candle(1, 100, 106.5, 98.9, 102)]
        outcome = policy_simulate(self.plan, bars, [feature()] * 2, self.cost, 5, "ALLOC_503020")
        self.assertEqual([e.reason for e in outcome.exits], ["STOP"] * 3)
        self.assertEqual([round(e.units, 2) for e in outcome.exits], [1.5, .9, .6])
        self.assertAlmostEqual(sum(e.units for e in outcome.exits), 3.)

    def test_swing_trail_uses_the_confirmation_close_and_not_pivot_time(self):
        bars = [candle(0, 100, 100.2, 99.8, 100), candle(1, 100, 102.2, 99.7, 101.7),
                candle(2, 101.7, 101.8, 100.4, 101.3), candle(3, 101.3, 101.6, 100.7, 101.0),
                candle(4, 101, 101.6, 100.5, 101.4), candle(5, 100.2, 101.0, 100.0, 100.5)]
        features = [feature()] * 6
        features[4] = feature(newest_low=100.4)
        out = policy_simulate(self.plan, bars, features, self.cost, 5, "SWING_TRAIL")
        self.assertEqual([(e.bar, e.reason) for e in out.exits],
                         [(1, "TP1"), (5, "STOP_GAP"), (5, "STOP_GAP")])

    def test_risk_cap_rounds_down_and_keeps_original_target_levels(self):
        wide = Plan("wide", "RETEST", 1, 0, 100, 87, (113, 126, 139), 10., 48)
        sized = capped_plan(wide, self.cost)
        self.assertEqual(sized.targets, wide.targets)
        self.assertAlmostEqual(sized.units, 1.44)
        self.assertLessEqual((13 + .704) * sized.units, 20.)
        self.assertGreater((13 + .704) * (sized.units + .03), 20.)

    def test_recovery_uses_prior_anchor_and_fresh_geometry(self):
        first = self.plan
        stale = Plan("stale", "RETEST", 1, 2, 100, 99, (102, 104, 106), 1., 48)
        fresh = Plan("fresh", "RETEST", 1, 3, 101, 99.3, (103, 104, 106), 1., 48)
        bars = [candle(i, 100, 100.5, 99.5, 100) for i in range(4)]
        bars[1]["low"] = 98.9
        decisions = [
            {"state": "ENTRY_AVAILABLE", "plan_id": p.id, "root_watch_ms": 300_000,
             "second_chance": False} for p in (first, stale, fresh)
        ]
        first_exit = simulate(first, bars, self.cost, 5)
        result = recovery_plans([first, stale, fresh], decisions, [first_exit], [], bars, self.cost.tick)
        self.assertNotIn("stale", result)
        self.assertEqual(result["fresh"]["prior_id"], "plan")
        self.assertLess(result["fresh"]["prior_known_at_utc_ms"], result["fresh"]["decision_utc_ms"])

    def test_recovery_preference_does_not_move_s5_priority(self):
        s5 = Plan("s5", "S5", 1, 0, 100, 99, (102, 104, 106), 1., 48)
        bars = [candle(0, 100, 100.2, 99.8, 100), candle(1, 100, 100.2, 98.9, 100)]
        trades = {p.id: simulate(p, bars, self.cost, 5) for p in (s5, self.plan)}
        outcomes, rejected = book([self.plan, s5], self.cost, 100_000,
                                  outcomes=trades, recovery_first=("plan",))
        self.assertEqual([o.plan.id for o in outcomes], ["s5"])
        self.assertEqual([(p.id, reason) for p, reason in rejected], [("plan", "POSITION_OCCUPIED")])


if __name__ == "__main__":
    unittest.main()
