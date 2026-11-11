from dataclasses import replace
import unittest

from gold_pro.replay import Costs, Plan
from gold_wick.wsb_forensic import (
    REFERENCE_LEVELS, Settings, _case_occupancy, candle_flags, displayed, reconstruct, wilder_atr,
)


def bar(i, opening, high, low, close, volume, minutes=5):
    interval = minutes * 60_000
    return {"time": i * interval, "time_close": (i + 1) * interval,
            "open": opening, "high": high, "low": low, "close": close, "volume": volume}


class WsbForensicTests(unittest.TestCase):
    def setUp(self):
        self.settings = replace(Settings(), count=1, window=2, absorption=False,
                                filter_atr_length=2, stop_atr_length=2)
        self.seed = [bar(0, 100, 101, 99, 100.1, 20), bar(1, 99.5, 103, 99, 99.4, 100)]

    def test_inclusive_wick_threshold_uses_pine_comparison_rounding(self):
        # The upper wick evaluates slightly above 25% before comparison rounding.
        sample = bar(0, 100.75 - 1e-12, 101, 100, 100.65, 10)
        self.assertEqual(candle_flags(sample, None, None, Settings()), (True, False, False, False))

    def test_small_high_volume_candle_without_wick_can_supply_direction(self):
        sample = bar(0, 100.70, 101, 100, 100.70, 112)
        self.assertEqual(candle_flags(sample, 2, 100, Settings()), (False, False, True, False))
        self.assertEqual(candle_flags(sample, 2, 100, self.settings), (False, False, False, False))

    def test_breakout_bar_volume_cannot_override_frozen_dominance(self):
        result = reconstruct([*self.seed, bar(2, 98, 99.5, 97, 98.9, 100_000)], self.settings)
        signals = [e for e in result["events"] if e["state"] == "SIGNAL"]
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]["side"], -1)
        self.assertAlmostEqual(signals[0]["buy_weight"], 21)
        self.assertAlmostEqual(signals[0]["sell_weight"], 99)
        self.assertEqual(result["plans"][0].reference, 98.9)
        self.assertEqual(result["plans"][0].bar, 2)

    def test_breakout_precedes_expiry_on_same_bar(self):
        settings = replace(self.settings, wait_bars=1)
        pause = bar(2, 100, 101, 99.5, 100, 10)
        inside = reconstruct([*self.seed, pause, bar(3, 100, 101, 99.5, 100, 10)], settings)
        self.assertTrue(any(e["state"] == "EXPIRED" and e["formation_bar"] == 1 for e in inside["events"]))
        crossed = reconstruct([*self.seed, pause, bar(3, 98, 99.5, 97, 98.9, 10)], settings)
        self.assertTrue(any(e["state"] == "SIGNAL" and e["formation_bar"] == 1 for e in crossed["events"]))

    def test_future_bars_cannot_change_past_signals(self):
        first_breakout = bar(2, 98, 99.5, 97, 98.9, 2000)
        prefix = reconstruct([*self.seed, first_breakout], self.settings)
        full = reconstruct([*self.seed, first_breakout, bar(3, 100, 105, 94, 103, 20)], self.settings)
        self.assertEqual(full["events"][:len(prefix["events"])], prefix["events"])
        self.assertEqual(full["signals"][:len(prefix["signals"])], prefix["signals"])
        self.assertEqual(full["plans"][:len(prefix["plans"])], prefix["plans"])

    def test_wilder_seed_and_m15_timeframe(self):
        bars = [bar(0, 100, 101, 99, 100, 20, 15), bar(1, 104, 105, 103, 104, 20, 15),
                bar(2, 104, 105, 103, 104, 20, 15)]
        self.assertEqual(wilder_atr(bars, 2), [None, 3.5, 2.75])
        self.assertEqual(reconstruct(bars, self.settings)["facts"][2]["atr"], 2.75)

    def test_actual_case_label_rounding(self):
        plan = Plan("case", "WSB", -1, 0, 4049.57, 4058.2307596311,
                    (4040.9092403689, 4032.2484807379, 4023.5877211068), 5.773839754, 72)
        self.assertEqual(displayed(plan), REFERENCE_LEVELS)

    def test_case_can_be_visual_while_the_single_slot_is_occupied(self):
        bars = [bar(i, 100, 102, 98, 100, 20) for i in range(4)]
        earlier = Plan("earlier", "WSB", -1, 0, 100, 110, (90, 80, 70), 10, 72)
        case = replace(earlier, id="case", bar=1)
        outcome = _case_occupancy(case, bars, {"plans": [earlier, case]}, Costs(), 5)
        self.assertFalse(outcome["admitted"])
        self.assertEqual(outcome["rejection"], "POSITION_OCCUPIED")
        self.assertEqual(outcome["held_at_case_confirmation"][0]["remaining_units_at_case_confirmation"], 3)


if __name__ == "__main__":
    unittest.main()
