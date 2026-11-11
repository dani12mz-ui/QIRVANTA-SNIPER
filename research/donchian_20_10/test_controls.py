"""Regression checks for the isolated dynamic-size control replay."""

import unittest

from gold_pro.rc2_research import Offer
from .controls import normalized_replay, position_size, timestamp
from .replay import BASE, HOUR_MS


T0 = timestamp("2026-11-04T20:00:00Z")


def bar(offset, opening, high, low, closing):
    first = T0 + offset * HOUR_MS
    return {"time": first, "time_close": first + HOUR_MS, "open": opening,
            "high": high, "low": low, "close": closing}


def offer(index, price=100.0, stop=98.0, targets=(102.0, 104.0, 106.0)):
    return Offer(f"test-{index}", 1, T0 + (index + 1) * HOUR_MS, index, price, stop, targets,
                 2.0, 3.0, 0.10, None, 0.0, 48, "MODERATE")


class ControlSizingTests(unittest.TestCase):
    def test_risk_sizing_and_margin_preserve_three_equal_legs(self):
        quantity, budget, risk, margin_capped = position_size(
            offer(0, price=4100.0, stop=4098.0), 100_000.0, BASE
        )
        self.assertEqual(quantity, 24)
        self.assertTrue(margin_capped)
        self.assertEqual(budget, 250.0)
        self.assertLessEqual(quantity * risk, budget)
        self.assertEqual(position_size(offer(0, price=4100.0, stop=4098.0), 10_000, BASE)[0], 0)

    def test_parity_protection_changes_occupancy_and_cost_ledger(self):
        bars = [
            bar(0, 100.0, 101.0, 99.0, 100.0),
            bar(1, 100.0, 102.2, 99.5, 101.0),
            bar(2, 101.0, 102.0, 100.2, 100.5),
            bar(3, 100.5, 101.0, 96.0, 97.0),
        ]
        offers = [offer(0), offer(2, 100.5, 98.5)]
        parity = normalized_replay(offers, bars, "parity", BASE, T0, bars[-1]["time_close"])
        fixed = normalized_replay(offers, bars, "fixed", BASE, T0, bars[-1]["time_close"])
        self.assertEqual(parity["lifecycle"]["CLOSED"], 2)
        self.assertEqual(fixed["lifecycle"]["CLOSED"], 1)
        self.assertEqual(fixed["lifecycle"]["POSITION_OCCUPIED"], 1)
        self.assertGreater(parity["campaigns"][0]["stop_last"], 100.0)
        self.assertEqual(fixed["campaigns"][0]["stop_last"], 98.0)
        for case in (parity, fixed):
            first = case["campaigns"][0]
            self.assertEqual(first["parts"][0]["reason"], "TP1")
            self.assertAlmostEqual(first["realized_gross_mid_usd"] - first["spread_usd"]
                                   - first["slippage_usd"] - first["commission_usd"] - first["funding_usd"],
                                   first["modeled_realized_net_usd"])
            # The open remainder at Wednesday NY rollover is two legs.
            self.assertEqual(first["financing_rows"][0]["multiplier"], 3)
            self.assertAlmostEqual(first["funding_usd"], (2 / 3) * first["qty"] * 101.0 * 0.04 * 3 / 365)


if __name__ == "__main__":
    unittest.main()
