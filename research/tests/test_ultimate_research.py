"""Targeted regressions for the new historical research paths."""
import unittest
from dataclasses import replace

from research.data.loader import load_bars
from research.ultimate.expected_r import expected_r_fold
from research.ultimate.fast import delayed_confirmation, drawdown_duration
from research.ultimate.models import evaluate_fold, target_before_stop
from research.ultimate.replay import Setup, replay
from research.ultimate.strategies import discover
from research.ultimate.structure import structural_features


INTERVAL = 900_000


def candle(index, o=100., h=101., l=99., c=100.):
    return {"time": index * INTERVAL, "time_close": (index + 1) * INTERVAL,
            "open": o, "high": h, "low": l, "close": c}


def features(bars):
    return [{"atr": 2., "rsi": 50., "buy_obstacle": 110., "sell_obstacle": 90.} for _ in bars]


class AnchoredReplayTests(unittest.TestCase):
    def setUp(self):
        self.setup = Setup("x", 0, 1, 100., 98., 2., 99.)

    def test_order_fills_next_open_and_conservative_collision(self):
        bars = [candle(0), candle(1, h=107., l=97.)]
        plan = replay(bars, features(bars), [self.setup])["plans"][0]
        self.assertEqual(plan["entry_time"], bars[1]["time"])
        self.assertAlmostEqual(plan["entry"], 100.002)
        self.assertTrue(plan["ambiguous"])
        self.assertEqual([part["reason"] for part in plan["exits"]], ["SL"] * 3)
        self.assertAlmostEqual(plan["net_cash"], -7.062)
        self.assertAlmostEqual(plan["paid_cash"], plan["net_cash"])

    def test_open_carries_paid_fee_and_no_forced_exit(self):
        bars = [candle(0), candle(1)]
        result = replay(bars, features(bars), [self.setup])
        plan = result["plans"][0]
        self.assertEqual(plan["state"], "OPEN")
        self.assertEqual(plan["remaining_quantity"], 3)
        self.assertEqual(plan["net_cash"], 0)
        self.assertAlmostEqual(result["paid_cash"], -.525)
        self.assertEqual(replay(bars[:1], features(bars[:1]), [self.setup])["plans"][0]["state"], "PENDING")

    def test_gap_fill_is_preserved_and_market_exit_is_next_open(self):
        bars = [candle(0), candle(1, o=100.6, h=101., l=100., c=100.7),
                candle(2, o=100.7, h=101., l=100., c=100.7)]
        plan = replay(bars, features(bars), [self.setup])["plans"][0]
        self.assertTrue(plan["invalid_fill_observed"])
        self.assertEqual([part["reason"] for part in plan["exits"]], ["INVALID_FILL"] * 3)
        self.assertEqual(plan["final_exit_time"], bars[2]["time"])

    def test_invalidation_cannot_exit_backdated_or_delete_losses(self):
        bars = [candle(0), candle(1, l=98.8, c=98.9), candle(2, o=99., l=98.5, c=99.)]
        normal = replay(bars, features(bars), [self.setup])["plans"][0]
        changed = replay(bars, features(bars), [self.setup], "INVALIDATION")["plans"][0]
        self.assertEqual(normal["state"], "OPEN")
        self.assertEqual(changed["final_exit_time"], bars[2]["time"])
        self.assertLess(changed["net_cash"], 0)

    def test_partial_tp_lock_does_not_affect_same_bar(self):
        bars = [candle(0), candle(1, h=102.5, l=99., c=101.5),
                candle(2, o=101.5, h=102., l=100., c=101.)]
        plan = replay(bars, features(bars), [self.setup])["plans"][0]
        self.assertEqual([part["reason"] for part in plan["exits"]], ["TP1", "SL", "SL"])
        self.assertEqual(plan["exits"][0]["time"], bars[1]["time_close"])
        self.assertEqual(plan["exits"][1]["time"], bars[2]["time_close"])
        self.assertAlmostEqual(sum(part["fees"] for part in plan["exits"]), 1.05)

    def test_opposite_confirmations_refuse_both(self):
        bars = [candle(0), candle(1)]
        other = replace(self.setup, id="y", side=-1, stop=102., boundary=101.)
        result = replay(bars, features(bars), [self.setup, other])
        self.assertFalse(result["plans"])
        self.assertEqual({r["reason"] for r in result["refusals"]}, {"OPPOSING_CONFIRMATIONS"})
        self.assertEqual(len(result["refusals"]), 2)

    def test_drawdown_duration_counts_recovery_only_after_losses(self):
        self.assertEqual(drawdown_duration([(0, 2), (3_600_000, 3)]), 0)
        self.assertEqual(drawdown_duration([(0, 2), (3_600_000, -1), (7_200_000, 1)]), 2)


class CausalityTests(unittest.TestCase):
    def test_delayed_acceptance_waits_for_close(self):
        seed = Setup("x", 0, 1, 100., 98., 2., 99.)
        bars = [candle(0), candle(1, l=99.1, c=100.5), candle(2, h=120.)]
        new, reason = delayed_confirmation(bars, features(bars), seed, "F2")
        self.assertEqual(reason, "DELAYED_CONFIRMED")
        self.assertEqual(new.index, 1)
        self.assertEqual(new.reference, 100.5)
        self.assertEqual(delayed_confirmation(bars[:1], features(bars[:1]), seed, "F2")[1],
                         "PENDING_AT_DATA_CUTOFF")
        self.assertEqual(delayed_confirmation(bars[:2], features(bars[:2]), seed, "F2")[0], new)

    def test_confirmation_gap_cannot_be_counted_as_acceptance(self):
        seed = Setup("x", 0, 1, 100., 98., 2., 99.)
        bars = [candle(0), candle(2)]
        self.assertEqual(delayed_confirmation(bars, features(bars), seed, "F4"),
                         (None, "GAP_BEFORE_CONFIRMATION"))

    def test_bos_waits_until_swing_was_confirmed(self):
        bars = [candle(i) for i in range(9)]
        bars[3] = candle(3, h=110.)
        bars[7] = candle(7, h=112., c=111.)
        result = structural_features(bars)
        self.assertIsNone(result[5]["pivot_high"])
        self.assertEqual(result[6]["pivot_high"], 110.)
        self.assertIsNone(result[6]["previous_swing_high"])
        self.assertEqual(result[7]["bos"], 1)
        self.assertEqual(result[:8], structural_features(bars[:8]))

    def test_incomplete_asian_range_and_gap_fvg_do_not_publish(self):
        full = [candle(i) for i in range(25)]
        normal = structural_features(full)
        self.assertIsNone(normal[22]["asian_range_utc"])
        self.assertEqual(normal[23]["asian_range_utc"]["available_at"], 24 * INTERVAL)
        incomplete = [bar for j, bar in enumerate(full) if j != 7]
        self.assertFalse(any(row["asian_range_utc"] for row in structural_features(incomplete)))
        gap = [candle(0), candle(2), candle(3, o=105., h=106., l=104., c=105.)]
        self.assertEqual(structural_features(gap)[2]["causal_fvg"], 0)

    def test_same_bar_tp_stop_is_unknown_for_model_label(self):
        campaign = {"exit_rows": [{"exit_bar_index": 5, "exit_comment": "TP1"},
                                  {"exit_bar_index": 5, "exit_comment": "SL"}]}
        self.assertIsNone(target_before_stop(campaign, "TP1"))
        self.assertEqual(target_before_stop(campaign, "TP2"), 0)

    def test_overlapping_label_fit_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "training label overlaps"):
            evaluate_fold([{"id": "train", "end": 1_800_000, "y": 1}],
                          [], [{"id": "test", "start": 2_000_000, "y": 0}])

    def test_scanner_prefix_is_identical_on_observed_market_data(self):
        bars = load_bars()[:600]
        first = bars[:450]
        whole, _ = discover(bars, structural_features(bars))
        prefix, _ = discover(first, structural_features(first))
        self.assertGreater(sum(map(len, prefix.values())), 0)
        self.assertEqual(prefix, {name: [setup for setup in setups if setup.index < len(first)]
                                  for name, setups in whole.items()})

    def test_expected_r_future_labels_do_not_change_predictions(self):
        rows = [{"id": str(i), "start": i * 3 * INTERVAL, "end": (i * 3 + 1) * INTERVAL,
                 "x": [float(i + j) for j in range(9)], "net_r": (i - 8) / 5, "net_cash": i - 8}
                for i in range(15)]
        train, test = rows[:12], rows[12:]
        original = expected_r_fold(train, test)
        revised = expected_r_fold(train, [{**row, "net_r": -20., "net_cash": -200.} for row in test])
        self.assertEqual(original["model"], revised["model"])
        self.assertEqual([p["expected_net_r"] for p in original["predictions"]],
                         [p["expected_net_r"] for p in revised["predictions"]])
        self.assertNotEqual(original["ridge_mse"], revised["ridge_mse"])


if __name__ == "__main__":
    unittest.main()
