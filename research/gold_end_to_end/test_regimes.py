"""Tests for completed-hour causality, missing bars and UTC attribution."""

import unittest

from gold_pro.direct_breakout_research import HOUR
from research.gold_end_to_end.regimes import independent_move_labels, regime_tape, utc_session


def hourly_bars(definitions, minutes=15):
    step = minutes * 60_000
    count = HOUR // step
    rows = []
    for hour, (opening, closing, width) in enumerate(definitions):
        # Hold the supplied open until the last chart candle.
        low = min(opening, closing)
        high = low + width
        for i in range(count):
            stamp = hour * HOUR + i * step
            rows.append({"time": stamp, "time_close": stamp + step,
                         "open": opening, "high": high, "low": low,
                         "close": closing if i == count - 1 else opening})
    return rows


class RegimeTapeTests(unittest.TestCase):
    def test_known_only_once_complete_and_prefix_stable(self):
        specs = [(100 + index, 101 + index, 1) for index in range(25)]
        bars = hourly_bars(specs)
        full = regime_tape(bars, 15)
        first = regime_tape(bars[:95], 15)
        self.assertEqual(first, {key: full[key] for key in first})
        self.assertEqual(full[bars[94]["time_close"]]["trend"], "UNKNOWN")
        self.assertEqual(full[bars[95]["time_close"]],
                         {"trend": "UP", "volatility": "NORMAL", "session": "ASIA"})

    def test_missing_first_middle_or_final_chart_bar_discards_hour(self):
        bars = hourly_bars([(100, 100, 1)] * 25)
        for index in (20, 22, 23):
            with self.subTest(index=index):
                tape = regime_tape(bars[:index] + bars[index + 1:], 15)
                self.assertEqual(tape[24 * HOUR]["trend"], "UNKNOWN")
                self.assertEqual(tape[25 * HOUR]["trend"], "SIDEWAYS")
        skipping_full_hour = regime_tape(bars[:20] + bars[24:], 15)
        self.assertEqual(skipping_full_hour[25 * HOUR]["volatility"], "NORMAL")

    def test_rules_for_down_high_and_sideways_low(self):
        stats = regime_tape(hourly_bars([(150 - i, 149 - i, 2 if i < 18 else 3)
                                         for i in range(24)]), 15)
        self.assertEqual(stats[24 * HOUR]["trend"], "DOWN")
        self.assertEqual(stats[24 * HOUR]["volatility"], "HIGH")
        flat = regime_tape(hourly_bars([(100, 100, 3 if i < 18 else 2)
                                        for i in range(24)]), 15)
        self.assertEqual(flat[24 * HOUR]["trend"], "SIDEWAYS")
        self.assertEqual(flat[24 * HOUR]["volatility"], "LOW")

    def test_low_efficiency_fails_directional_threshold(self):
        specs = [(100, 105, 5) if i % 2 == 0 else (105, 100, 5) for i in range(23)]
        specs.append((105, 112, 7))
        stats = regime_tape(hourly_bars(specs), 15)
        self.assertEqual(stats[24 * HOUR]["trend"], "SIDEWAYS")

    def test_session_edges_and_invalid_rows(self):
        self.assertEqual([utc_session(t * HOUR) for t in (0, 6, 7, 12, 13, 20, 21, 24)],
                         ["ASIA", "ASIA", "EUROPE", "EUROPE", "US", "US", "ROLLOVER", "ASIA"])
        bars = hourly_bars([(100, 100, 1)])
        with self.assertRaises(ValueError):
            regime_tape([bars[0], bars[0]], 15)
        with self.assertRaises(ValueError):
            regime_tape([{**bars[0], "low": 105}], 15)

    def test_independent_move_outcomes_keep_registered_rule(self):
        bars = hourly_bars([(100, 100, 1)] * 64 + [(100, 103, 3)])
        events, _, _ = independent_move_labels(bars, 15)
        self.assertEqual([(e["start"], e["side"]) for e in events], [(64 * HOUR, 1)])
        tape = regime_tape(bars, 15)
        self.assertNotIn("DIRECTIONAL_BUY", {item["trend"] for item in tape.values()})


if __name__ == "__main__":
    unittest.main()

