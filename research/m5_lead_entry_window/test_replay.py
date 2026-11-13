import unittest

from gold_pro.direct_breakout_research import BASE, STRESS
from research.m5_lead_entry_window.replay import STEP, Setup, execute, gate, replay


def bar(i, opening=100.0, high=101.0, low=99.0, close=100.0):
    return {"time": i * STEP, "time_close": (i + 1) * STEP,
            "open": opening, "high": high, "low": low, "close": close}


class M5LeadReplayTest(unittest.TestCase):
    def test_observation_prefix_and_next_open_clock(self):
        bars = [bar(i) for i in range(22)]
        bars.extend([
            bar(22, 100.0, 100.9, 99.2, 100.8),
            bar(23, 100.8, 100.98, 99.5, 100.9),
            bar(24, 100.9, 101.9, 100.6, 101.7),
            bar(25, 101.7, 101.8, 100.9, 101.3),
        ])
        events = replay(bars)
        for size in range(22, len(bars)):
            self.assertEqual(replay(bars[:size]), [e for e in events if e["known_ms"] <= size * STEP])
        self.assertIn(("EARLY_SETUP", 24 * STEP), [(e["event"], e["known_ms"]) for e in events])
        breakout, = [e for e in events if e["event"] == "MICRO_BREAK"]
        geometry, = [e for e in events if e["id"] == breakout["id"] and e["event"] == "REJECTED"]
        self.assertEqual(geometry["entry_ms"], breakout["known_ms"])
        self.assertEqual(geometry["known_ms"], breakout["known_ms"] + STEP)
        self.assertEqual(geometry["entry"], 101.7)
        self.assertEqual(geometry["reason"], "OBSTACLE_UNKNOWN")

    def test_both_directions_use_quote_side_stop_before_target(self):
        bars = [bar(0, 100.0, 106.0, 94.0, 100.0)]
        for side in (-1, 1):
            candidate = {"id": str(side), "side": side, "entry_ms": 0, "entry": 100.0,
                         "stop": 100.0 - 4 * side, "obstacle": 100.0 + 4 * side}
            for costs in (BASE, STRESS):
                result = execute(candidate, bars, costs)
                self.assertEqual(result["reason"], "SL_FIRST_BOTH")
                gross = -1.0 + costs.spread / 8
                self.assertAlmostEqual(result["gross_r"], gross)
                friction = costs.spread + 2 * (costs.slip + costs.commission)
                self.assertAlmostEqual(result["net_r"], gross - friction / 4)
                self.assertAlmostEqual(result["cost_r"], friction / 4)

    def test_gap_does_not_invent_an_entry_or_reuse_obstacle(self):
        bars = [bar(0), bar(2)]
        pending = Setup(1, 99.9, STEP, 0, atr=2.0, stop=96.0, obstacle=99.95)
        self.assertEqual(gate(bars, 1, pending, BASE), ("ENTRY_GAP", {}))
        reason, fields = gate([bar(0), bar(1)], 1, pending, BASE)
        self.assertEqual(reason, "OBSTACLE_CROSSED")
        self.assertEqual(fields["entry_ms"], STEP)
        self.assertLess(fields["room_r"], 0)

    def test_priority_exit_happens_at_next_open(self):
        candidate = {"id": "priority", "side": 1, "entry_ms": 0, "entry": 100.0,
                     "stop": 96.0, "obstacle": 104.0}
        bars = [bar(0), bar(1, 101.0, 106.0, 94.0, 100.0)]
        result = execute(candidate, bars, BASE, priority_close=STEP)
        self.assertEqual(result["reason"], "S5_PRIORITY")
        self.assertEqual(result["exit_bar"], 1)
        self.assertAlmostEqual(result["exit_price"], 101 - BASE.spread / 2 - BASE.slip)


if __name__ == "__main__":
    unittest.main()
