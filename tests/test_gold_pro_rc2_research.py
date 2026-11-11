import unittest
from dataclasses import replace
from types import SimpleNamespace

from gold_pro.rc1_qa import Context
from gold_pro.rc2_research import Offer, decision_diagnostics, early_observations, replay, report, shared_bar_comparison, simulate


MINUTE = 60_000
START = 1793852400000


def candle(i, opening=100.0, high=101.0, low=99.0, close=100.0):
    return {"time": START + i * 5 * MINUTE, "time_close": START + (i + 1) * 5 * MINUTE,
            "open": opening, "high": high, "low": low, "close": close}


def buy(index=0):
    return Offer(f"test-buy-{index}", 1, candle(index)["time_close"], index, 100.0, 98.0, (102.0, 104.0, 106.0),
                 2.0, 3.5, 0.35, None, 0.0, 48, "TEST")


class ResearchTests(unittest.TestCase):
    def test_stop_precedes_targets_for_unknown_intrabar_order_on_both_sides(self):
        bars = [candle(0), candle(1, high=107.0, low=93.0)]
        short = replace(buy(), id="test-sell", side=-1, stop=102.0, targets=(98.0, 96.0, 94.0))
        for offer in (buy(), short):
            with self.subTest(offer=offer.id):
                campaign, release = simulate(offer, bars, None)
                self.assertEqual(release, 1)
                self.assertTrue(campaign.closed)
                self.assertEqual([x.reason for x in campaign.exits], ["SL", "SL", "SL"])
                self.assertEqual(sum(x.units for x in campaign.exits), 3)
                self.assertAlmostEqual(sum(x.commission for x in campaign.exits), 1.05)
                self.assertLess(campaign.net, 0)

    def test_known_open_target_can_precede_intrabar_stop(self):
        bars = [candle(0), candle(1), candle(2, 104.0, 107.0, 97.0)]
        campaign, _ = simulate(buy(), bars, None)
        self.assertEqual([x.reason for x in campaign.exits], ["TP1_AT_OPEN", "TP2_AT_OPEN", "SL"])

    def test_protection_retries_after_tp1_without_retroactive_fill(self):
        bars = [candle(0), candle(1, high=102.2, low=99.5, close=99.8),
                candle(2, high=101.2, low=99.6, close=101.0),
                candle(3, opening=99.9, high=101.0, low=99.4)]
        protected, release = simulate(buy(), bars, 0.0)
        fixed, _ = simulate(buy(), bars, None)
        self.assertEqual(release, 3)
        self.assertEqual([x.reason for x in protected.exits], ["TP1", "SL_GAP", "SL_GAP"])
        self.assertAlmostEqual(protected.exits[1].price, 99.898)
        self.assertFalse(fixed.closed)

    def test_single_slot_can_reopen_after_a_closed_bar(self):
        bars = [candle(0), candle(1, low=97.0), candle(2, low=97.0)]
        trades, states = replay([buy(), buy(), buy(1)], bars, None)
        self.assertEqual([trade.id for trade in trades], ["test-buy-0", "test-buy-1"])
        self.assertEqual(states, {"closed": 2, "blocked_occupied": 1})

    def test_range_filter_uses_only_signal_candle(self):
        later_wide = [candle(0), candle(1, high=108.0)]
        _, states = replay([buy()], later_wide, None, range_filter=True)
        self.assertNotIn("rejected_signal_range", states)
        earlier_wide = [candle(0, high=104.0), candle(1)]
        self.assertEqual(replay([buy()], earlier_wide, None, range_filter=True)[1], {"rejected_signal_range": 1})

    def test_early_watch_is_causal_edge_and_ignores_gap(self):
        prefix = [candle(i) for i in range(15)]
        trigger = candle(15, opening=100.0, high=103.5, low=99.0, close=103.0)
        continuation = candle(16, opening=103.0, high=107.0, low=102.0, close=106.0)
        expected = [{"known_ms": trigger["time_close"], "side": 1, "price": 103.0}]
        self.assertEqual(early_observations([*prefix, trigger]), expected)
        self.assertEqual(early_observations([*prefix, trigger, continuation]), expected)
        gapped = dict(trigger, time=trigger["time"] + MINUTE, time_close=trigger["time_close"] + MINUTE)
        self.assertEqual(early_observations([*prefix, gapped]), [])

    def test_post_signal_price_observation_censors_missing_intervals(self):
        raw = {"known_time": candle(0)["time_close"], "side": 1, "event": "REJECTED",
               "event_id": "test", "reason": "rejected", "quality": "TEST"}
        export = SimpleNamespace(core=[raw])
        contiguous = [candle(i) for i in range(13)]
        complete = decision_diagnostics(export, contiguous, 5)
        self.assertEqual(complete["summary"][0]["median_favorable_price"], 1.0)
        with_gap = decision_diagnostics(export, [contiguous[0], *contiguous[2:], candle(13)], 5)
        self.assertIsNone(with_gap["observations"][0]["subsequent_60m"])

    def test_pinned_archive_real_metrics_and_empty_frame(self):
        result = report()
        self.assertEqual(result["unchanged_forward_seal"]["holdout_status"], "NOT_AVAILABLE")
        m5, m15, m30 = (result["frames"][frame] for frame in ("M5", "M15", "M30"))
        self.assertEqual([item["native_reference_vs_rc1"]["status"] for item in (m5, m15, m30)], ["MATCH"] * 3)
        for item, n in zip((m5, m15, m30), (8, 2, 0)):
            self.assertEqual(item["native_s5_raw_and_rc1_identical"]["all"]["n"], n)
        self.assertAlmostEqual(m5["native_s5_raw_and_rc1_identical"]["all"]["net_cash"], 24.419)
        self.assertEqual(m30["ohlc_model_lanes"]["FIXED"]["all"]["profit_factor_status"], "NO_TRADES")
        self.assertEqual(m5["ohlc_model_lanes"]["FIXED"]["all"], m5["ohlc_model_lanes"]["FIXED_RANGE_FILTER_175_ATR"]["all"])

    def test_common_window_preserves_price_and_htf_checks_across_shifted_warmup(self):
        context = [Context(START, i, candle(i)["time"], candle(i)["time_close"],
                           (100, 101, 99, 100), (None, None)) for i in range(3)]
        event = {"known_time": context[-1].closing, "event_id": "watch"}
        archive = SimpleNamespace(context=context, core=[event])
        shifted = [replace(row, first_open=context[1].opening, index=i) for i, row in enumerate(context[1:])]
        fresh = SimpleNamespace(context=shifted, core=[event])
        result = shared_bar_comparison(archive, fresh, ("core",))
        self.assertEqual((result["status"], result["common_bars"]), ("MATCH", 2))
        fresh.context[1] = replace(fresh.context[1], htf_closes=(START, None))
        self.assertEqual(shared_bar_comparison(archive, fresh, ("core",))["status"], "NO_EXACT_COMMON_CONTEXT")


if __name__ == "__main__":
    unittest.main()
