"""Causality boundaries and economics of final BREAKOUT research."""

from types import SimpleNamespace
import unittest

from gold_precision.breakout_research import fixed_gate_effect, recent_reclaim


class BreakoutResearchTests(unittest.TestCase):
    def setUp(self):
        self.settings = {"lookback_bars": 6, "reclaim_atr": 0.10, "anchor_proximity_atr": 0.25}

    def test_reclaim_must_precede_candidate_close(self):
        bars = [{"close": close} for close in (101.0, 101.2, 99.0, 101.5)]
        attempts = {(0, 1): (100.0, 2.0)}
        self.assertIsNone(recent_reclaim(2, 1, 100.0, 2.0, attempts, bars, self.settings))
        self.assertEqual(recent_reclaim(3, 1, 100.0, 2.0, attempts, bars, self.settings), (0, 2))

    def test_reclaim_respects_side_anchor_and_expiry(self):
        bars = [{"close": close} for close in (99.0, 100.4, 98.6, 99.0, 98.5, 98.9, 98.6, 98.7)]
        attempts = {(0, -1): (100.0, 2.0)}
        self.assertEqual(recent_reclaim(2, -1, 100.0, 2.0, attempts, bars, self.settings), (0, 1))
        self.assertIsNone(recent_reclaim(2, -1, 100.6, 2.0, attempts, bars, self.settings))
        self.assertIsNone(recent_reclaim(7, -1, 100.0, 2.0, attempts, bars, self.settings))

    def test_excluding_winners_and_losers_keeps_signed_r(self):
        winning = SimpleNamespace(id="win", net_cash=15, stressed_net_cash=13, risk_cash=10)
        losing = SimpleNamespace(id="loss", net_cash=-6, stressed_net_cash=-8, risk_cash=3)
        other = SimpleNamespace(id="other", net_cash=100, stressed_net_cash=80, risk_cash=10)
        result = fixed_gate_effect([winning, losing, other], {"win", "loss"})
        self.assertEqual(result["winners_excluded"], 1)
        self.assertEqual(result["losers_excluded"], 1)
        self.assertAlmostEqual(result["winning_r_forgone"], 1.5)
        self.assertAlmostEqual(result["losing_r_avoided"], 2.0)
        self.assertAlmostEqual(result["net_r_delta"], 0.5)


if __name__ == "__main__":
    unittest.main()
