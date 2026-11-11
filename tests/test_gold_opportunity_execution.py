from types import SimpleNamespace
import unittest

from gold_pro.opportunity import event_audit, scan
from gold_pro.replay import Costs, Outcome, Part, Plan, portfolio, simulate


M5 = 300_000


def candles(quotes):
    return [
        {"time": i * M5, "time_close": (i + 1) * M5, "open": op,
         "high": high, "low": low, "close": close}
        for i, (op, high, low, close) in enumerate(quotes)
    ]


def buy_plan(identifier="buy", bar=0):
    return Plan(identifier, "S5", 1, bar, 100, 96, (104, 108, 112), 4, 48)


def feature(*, ema21=101, trend=0, bos=0, displacement=0, highs=()):
    return SimpleNamespace(
        atr=2, ema21=ema21, trend=trend, bos=bos, displacement=displacement,
        highs=highs, lows=(), mss=False, spike=False, regime="TRENDING_UP",
    )


def opportunity_fixture():
    bars = candles((
        (100.0, 100.2, 99.8, 100.0),
        (100.1, 100.5, 100.0, 100.3),
        (100.3, 100.9, 100.2, 100.7),
        (100.7, 101.2, 100.6, 101.0),
        (101.1, 103.9, 100.9, 103.8),
        (103.8, 104.8, 102.4, 104.6),
        (104.2, 105.0, 103.95, 104.9),
        (104.6, 105.5, 104.4, 105.4),
        (105.4, 105.5, 104.8, 105.1),
    ))
    features = [
        feature(), feature(), feature(), feature(),
        feature(displacement=1),
        feature(ema21=104, trend=1, bos=1, displacement=1, highs=((3, 103.9),)),
        feature(ema21=104.5, trend=1, highs=((3, 103.9),)),
        feature(ema21=104.8, trend=1, displacement=1, highs=((3, 103.9),)),
        feature(ema21=104.8),
    ]
    settings = {
        "warmup_bars": 4, "watch_bars": 4, "model_cooldown_bars": 1,
        "models": ("BREAKOUT", "PULLBACK"),
        "strict": {"max_stop_atr": 2, "max_extension_atr": 1.5, "min_momentum_atr": .3},
    }
    return bars, features, [(1, 1)] * len(bars), settings


class GoldExecutionTests(unittest.TestCase):
    def test_next_open_fill_and_stop_first_on_ambiguous_bar(self):
        bars = candles(((100, 125, 85, 100), (100, 116, 94, 100)))
        result = simulate(buy_plan(), bars, Costs(), 5)
        self.assertEqual(result.entry_bar, 1)
        self.assertEqual(result.state, "CLOSED")
        self.assertEqual([(part.bar, part.reason) for part in result.exits], [(1, "STOP")] * 3)
        self.assertAlmostEqual(result.fill, 100.002)
        self.assertAlmostEqual(result.exits[0].price, 95.998)
        self.assertAlmostEqual(result.net, -14.112)

    def test_limit_touch_does_not_fill_and_prior_high_cannot_earn_a_target(self):
        bars = candles((
            (100, 101, 98, 100),
            (100, 115, 99, 101),       # Exact limit touch supplies no queue penetration.
            (100, 116, 98.95, 100),    # Intrabar entry. The high could predate it.
            (95.5, 97, 95, 96),
        ))
        result = simulate(buy_plan(), bars, Costs(), 5, method="limit", entry_amount=.25)
        self.assertEqual(result.entry_bar, 2)
        self.assertEqual(result.fill, 99)
        self.assertEqual([(part.bar, part.reason) for part in result.exits], [(3, "STOP_GAP")] * 3)
        self.assertLess(result.net, 0)

    def test_unfilled_limit_reserves_slot_until_expiration_close(self):
        bars = candles((
            (100, 101, 98, 100),
            (100, 115, 99, 101), (100, 115, 99, 101), (100, 115, 99, 101),
            (98.8, 101, 98, 100),
        ))
        plans = [buy_plan("pending"), buy_plan("blocked", bar=1), buy_plan("after-expiry", bar=3)]
        outcomes, rejected = portfolio(plans, bars, Costs(), 5, 100_000, method="limit", entry_amount=.25)
        self.assertEqual(outcomes[0].state, "ENTRY_EXPIRED")
        self.assertEqual(outcomes[0].release, 3)
        self.assertIsNone(outcomes[0].entry_bar)
        self.assertEqual(outcomes[0].exits, ())
        self.assertEqual([(p.id, reason) for p, reason in rejected], [("blocked", "POSITION_OCCUPIED")])
        self.assertEqual(outcomes[1].plan.id, "after-expiry")
        self.assertEqual(outcomes[1].entry_bar, 4)

    def test_break_even_stop_cannot_be_applied_before_tp1_bar_close(self):
        bars = candles((
            (100, 101, 99, 100),
            (100, 104.2, 97, 103.8),
            (100.6, 102, 99, 101),
        ))
        result = simulate(buy_plan(), bars, Costs(), 5, exit_policy="be")
        self.assertEqual([(part.bar, part.reason) for part in result.exits],
                         [(1, "TP1"), (2, "STOP_GAP"), (2, "STOP_GAP")])
        self.assertAlmostEqual(result.exits[1].price, 100.598)

    def test_invalid_gap_cannot_be_credited_with_same_bar_targets(self):
        bars = candles((
            (100, 102, 99, 100), (103, 115, 102, 104), (102, 103, 101, 102),
        ))
        result = simulate(buy_plan(), bars, Costs(), 5)
        self.assertEqual(result.entry_bar, 1)
        self.assertEqual([(part.bar, part.reason) for part in result.exits],
                         [(2, "GAP_OR_FILL_INVALID")] * 3)
        self.assertLess(result.net, 0)


class GoldOpportunityTests(unittest.TestCase):
    def scan(self, bars, features, context, settings):
        return scan(bars, features, context, settings, Costs(), 100_000, mode="STRICT")

    def test_early_watch_rejected_break_and_fresh_second_chance(self):
        bars, features, context, settings = opportunity_fixture()
        rows, plans = self.scan(bars, features, context, settings)
        events = {(row["bar"], row["state"]): row for row in rows if row["side"] == 1}
        watch = events[4, "WATCH"]
        rejected = events[5, "RISK_REJECTED"]
        second = events[6, "SECOND_CHANCE"]
        self.assertLess(watch["time_ms"], rejected["time_ms"])
        self.assertEqual(rejected["reason"], "OVERSIZED_STOP")
        self.assertIn((5, "WAIT_FOR_PULLBACK"), events)
        self.assertEqual(second["model"], "PULLBACK")
        self.assertEqual(second["root_watch_ms"], watch["time_ms"])
        self.assertEqual(second["first_structure_ms"], rejected["time_ms"])
        self.assertGreater(second["invalidation"], rejected["invalidation"])
        self.assertLess(second["stop_distance"], rejected["stop_distance"])
        self.assertEqual([(plan.bar, plan.reference, plan.stop) for plan in plans], [(6, 104.9, 102.2)])
        self.assertIn((8, "OPPORTUNITY_EXPIRED"), events)

    def test_model_cooldown_and_prefix_invariance(self):
        bars, features, context, settings = opportunity_fixture()
        rows, plans = self.scan(bars, features, context, settings)
        without_cooldown, _ = self.scan(bars, features, context, {**settings, "model_cooldown_bars": 0})
        def confirms_bar_7(decisions):
            return any(d["bar"] == 7 and d["side"] == 1 and d["model"] == "PULLBACK"
                       and d["state"] == "TREND_CONFIRMED" for d in decisions)

        self.assertFalse(confirms_bar_7(rows))
        self.assertTrue(confirms_bar_7(without_cooldown))
        for end in (5, 6, 7, 8):
            with self.subTest(end=end):
                prefix_rows, prefix_plans = self.scan(bars[:end], features[:end], context[:end], settings)
                self.assertEqual(prefix_rows, [row for row in rows if row["bar"] < end])
                self.assertEqual(prefix_plans, [plan for plan in plans if plan.bar < end])

    def test_unknown_higher_timeframe_vetoes_fresh_second_chance(self):
        bars, features, context, settings = opportunity_fixture()
        context[6] = (1, None)
        rows, plans = self.scan(bars, features, context, settings)
        pullback = [row for row in rows if row["bar"] == 6 and row["model"] == "PULLBACK"]
        self.assertEqual([row["state"] for row in pullback], ["TREND_CONFIRMED", "RISK_REJECTED"])
        self.assertIn("HTF_UNKNOWN", pullback[-1]["all_reasons"])
        self.assertTrue(pullback[-1]["second_chance"])
        self.assertFalse(plans)

    def test_event_capture_excludes_unordered_extremes_and_dates_losses_at_exit(self):
        quiet_hours = [(100, 100.5, 99.5, 100)] * (24 * 12)
        directional_hour = [(100, 100.8, 99.5, 100.5), (100.5, 103, 99.5, 100.5),
                            (100.5, 104, 99.5, 103)] + [(103, 107, 102.5, 106)] * 9
        bars = candles(quiet_hours + directional_hour)
        start_bar = len(quiet_hours)
        start = bars[start_bar]["time"]
        config = {"minimum_absolute_open_to_close_multiple": 1.5,
                  "minimum_absolute_open_to_close_over_hour_range": .6}
        inside = buy_plan("inside", start_bar)
        outside = buy_plan("outside", start_bar - 3)
        first = Outcome(inside, "CLOSED", start_bar + 2, start_bar + 1, 100.5,
                        (Part(start_bar + 2, 99.5, 3, "STOP", 1.05, -4.05),))
        second = Outcome(outside, "CLOSED", start_bar - 1, start_bar - 2, 100,
                         (Part(start_bar - 1, 99.5, 3, "STOP", 1.05, -2.55),))
        decisions = [{"time_ms": start, "side": 1, "state": "WATCH"},
                     {"time_ms": start + M5, "side": 1, "state": "TREND_CONFIRMED"}]
        events, off_event, summary = event_audit(
            bars, 5, config, decisions, [inside, outside], [first, second], [],
        )
        self.assertEqual(summary["event_count"], 1)
        event = events[0]
        self.assertEqual((event["watch_at"], event["confirmed_at"], event["fill_at"]),
                         (start, start + M5, start + M5))
        self.assertTrue(event["filled"])
        self.assertEqual(event["capture_fraction"], 0)
        self.assertLess(event["net_r"], 0)
        self.assertEqual(off_event, [{"time_ms": bars[start_bar - 1]["time"],
                                      "confirmations": 0, "losses": 1}])


if __name__ == "__main__":
    unittest.main()
