import unittest

from gold_pro.replay import Costs, simulate
from gold_wick.engine import scan


INTERVAL = 5 * 60_000
QUIET = (100.0, 101.0, 99.0, 100.0)
LOWER_ONE = (100.2, 100.6, 98.5, 100.4)
LOWER_TWO = (100.4, 100.9, 98.7, 100.7)
BOTH_PROBE = (100.5, 101.6, 98.2, 100.7)
BREAK = (100.75, 102.8, 100.4, 102.4)
RETEST = (101.1, 103.6, 100.95, 103.5)
RECLAIM = (103.4, 103.6, 100.2, 100.8)


def candles(prices, start=0):
    return [
        {"time": i * INTERVAL, "time_close": (i + 1) * INTERVAL, "open": opening,
         "high": high, "low": low, "close": close, "volume": 100.0}
        for i, (opening, high, low, close) in enumerate(prices, start)
    ]


def initial():
    return candles([QUIET] * 23 + [LOWER_ONE, QUIET, LOWER_TWO])


def event(result, state):
    return next(item for item in result.events if item["state"] == state)


class GoldWickEngineTests(unittest.TestCase):
    def test_frozen_formation_breakout_retest_and_failure_use_confirmed_bars(self):
        data = initial()
        data.extend(candles([BOTH_PROBE, BREAK, RETEST, RECLAIM], start=len(data)))
        data[27]["volume"] = 200
        result = scan(data, minutes=5, context=[(1, 1)] * len(data))

        watch = event(result, "BUY_WATCH")
        self.assertEqual((watch["bar"], watch["top"], watch["bottom"], watch["formation_bars"]),
                         (25, 101.0, 98.5, 4))
        self.assertEqual(watch["first_known_ms"], data[25]["time_close"])
        self.assertEqual(watch["first_rejection_ms"], data[23]["time_close"])
        self.assertEqual(watch["formation_id"], f"WF|1|{data[25]['time_close']}")
        pending = event(result, "BREAKOUT_PENDING")
        self.assertEqual((pending["bar"], pending["reason"]), (26, "BOTH_EDGES_PROBED"))
        self.assertFalse(any(plan.bar == 26 for plan in result.plans))
        confirm = event(result, "BUY_BREAKOUT_CONFIRMED")
        self.assertEqual(confirm["bar"], 27)
        self.assertEqual(confirm["breakout_known_ms"], data[27]["time_close"])
        self.assertEqual(confirm["bottom"], 98.5)
        valid = [item for item in result.events if item["state"] == "ENTRY_VALID"]
        self.assertEqual([item["model"] for item in valid], ["WICK_BREAKOUT", "WICK_RETEST"])
        self.assertEqual([item["bar"] for item in valid], [27, 28])
        self.assertTrue(valid[0]["volume_dominance"])
        self.assertTrue(valid[0]["volume_activity"])
        self.assertTrue(valid[0]["htf_aligned"])
        failure = event(result, "FALSE_BREAKOUT")
        self.assertEqual((failure["bar"], failure["breakout_bar"], failure["formation_id"]),
                         (29, 27, watch["formation_id"]))
        self.assertEqual(result.plans[0].reference, data[27]["close"])
        self.assertLess(result.plans[0].stop, watch["bottom"])
        self.assertLess(result.plans[1].stop, data[28]["low"])
        self.assertEqual(simulate(result.plans[0], data, Costs(), 5).entry_bar, 28)

    def test_scan_prefix_and_short_history_warmup(self):
        data = initial()
        data.extend(candles([BOTH_PROBE, BREAK, RETEST, RECLAIM], start=len(data)))
        complete = scan(data, minutes=5)
        for stop in range(len(data) + 1):
            with self.subTest(stop=stop):
                partial = scan(data[:stop], minutes=5)
                self.assertEqual(partial.features, complete.features[:stop])
                self.assertEqual(partial.events, tuple(x for x in complete.events if x["bar"] < stop))
                self.assertEqual(partial.plans, tuple(x for x in complete.plans if x.bar < stop))
        self.assertIsNone(complete.features[13]["prior_atr"])
        self.assertEqual(complete.features[14]["prior_atr"], 2.0)
        self.assertLess(complete.features[27]["prior_atr"], complete.features[28]["prior_atr"])

    def test_missing_volume_never_passes_the_volume_variants(self):
        data = initial()
        data.extend(candles([BREAK], start=len(data)))
        data[10]["volume"] = 0
        data[-1]["volume"] = 200
        without_mean = event(scan(data, minutes=5), "ENTRY_VALID")
        self.assertTrue(without_mean["volume_available"])
        self.assertTrue(without_mean["volume_dominance"])
        self.assertIsNone(without_mean["volume_activity"])
        data[-1]["volume"] = float("nan")
        unavailable = event(scan(data, minutes=5), "ENTRY_VALID")
        self.assertFalse(unavailable["volume_available"])
        self.assertIsNone(unavailable["volume_dominance"])
        self.assertIsNone(unavailable["volume_activity"])
        self.assertIsNone(unavailable["htf_aligned"])

    def test_ambiguous_cluster_and_sell_mirror(self):
        opposite = (99.9, 102.0, 99.5, 99.7)
        mixed = candles([QUIET] * 23 + [LOWER_ONE, opposite, LOWER_TWO])
        mixed_result = scan(mixed, minutes=5)
        ambiguous = event(mixed_result, "FORMATION_BUILDING")
        self.assertTrue(any(item["reason"] == "OPPOSING_REJECTIONS" for item in mixed_result.events))
        self.assertEqual(ambiguous["reason"], "FIRST_REJECTION")
        self.assertFalse(any(item["state"].endswith("_WATCH") for item in mixed_result.events))

        mirrored = [(200 - opening, 200 - low, 200 - high, 200 - close)
                    for opening, high, low, close in [QUIET] * 23 + [LOWER_ONE, QUIET, LOWER_TWO, BREAK]]
        result = scan(candles(mirrored), minutes=5)
        self.assertEqual(event(result, "SELL_BREAKOUT_CONFIRMED")["side"], -1)
        self.assertGreater(result.plans[0].stop, result.plans[0].reference)
        self.assertTrue(all(target < result.plans[0].reference for target in result.plans[0].targets))

    def test_exact_last_breakout_bar_and_weekend_clock_expiration(self):
        data = initial()
        last_allowed = candles([QUIET] * 7 + [BREAK], start=len(data))
        reached = scan(data + last_allowed, minutes=5)
        self.assertEqual(event(reached, "BUY_BREAKOUT_CONFIRMED")["age_bars"], 8)

        late = candles([QUIET] * 8 + [BREAK], start=len(data))
        expired = scan(data + late, minutes=5)
        self.assertEqual(event(expired, "FORMATION_EXPIRED")["age_bars"], 9)
        self.assertFalse(expired.plans)

        after_gap = candles([BREAK], start=len(data) + 20)
        expired = scan(data + after_gap, minutes=5)
        self.assertEqual(event(expired, "FORMATION_EXPIRED")["age_bars"], 1)
        self.assertFalse(expired.features[-1]["sequence_ready"])
        self.assertFalse(expired.plans)

    def test_opposite_bound_invalidates_and_risk_cap_does_not_erase_confirmation(self):
        data = initial()
        invalidation = candles([(100.0, 101.0, 97.9, 98.0)], start=len(data))
        expired = scan(data + invalidation, minutes=5)
        self.assertEqual(event(expired, "FORMATION_EXPIRED")["reason"], "OPPOSITE_EDGE_CLOSE")

        risk = scan(data + candles([BREAK], start=len(data)), minutes=5, equity=1_000)
        self.assertEqual(event(risk, "BUY_BREAKOUT_CONFIRMED")["bar"], 26)
        self.assertIn("CASH_RISK_CAP", event(risk, "RISK_REJECTED")["risk_reasons"])
        self.assertFalse(risk.plans)

    def test_bad_time_or_context_is_rejected(self):
        data = initial()
        with self.assertRaises(ValueError):
            scan(data, minutes=5, context=[])
        data[3]["time"] -= 1
        with self.assertRaises(ValueError):
            scan(data, minutes=5)


if __name__ == "__main__":
    unittest.main()
