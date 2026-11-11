import unittest

from research.donchian_20_10.history.compare import (
    DAY_MS, SYMBOL, cash_bridge, common_window, period, rolling_windows,
)
from research.donchian_20_10.replay import BASE, Bar, run, utc_ms


START = utc_ms("2026-01-01T00:00:00Z")


def flat_bar(i, hours):
    step = hours * 3_600_000
    return Bar(START + i * step, START + (i + 1) * step, 100, 102, 98, 100)


class DonchianHistoryComparison(unittest.TestCase):
    def test_later_warmup_and_same_last_close(self):
        daily = [flat_bar(i, 24) for i in range(110)]
        four_hour = [flat_bar(i, 4) for i in range(657)]
        self.assertEqual(common_window({"D1": daily, "H4": four_hour}),
                         (daily[100].close_ms, daily[108].close_ms))

    def test_rolling_windows_use_only_shared_observed_marks(self):
        marks = [START + day * DAY_MS for day in range(130) if day % 7 not in (5, 6)]
        windows = rolling_windows(marks, START, START + 130 * DAY_MS)
        self.assertEqual(len(windows), 2)
        self.assertTrue(all(start in marks and end in marks and end - start >= 90 * DAY_MS
                            for start, end in windows))
        self.assertEqual(windows[-1][1], marks[-1])

    def test_cash_bridge_reconciles_open_position_and_half_spread_mark(self):
        bars = [flat_bar(i, 24) for i in range(105)]
        trigger = bars[-2]
        bars[-2] = Bar(trigger.time_ms, trigger.close_ms, 100, 106, 98, 105)
        fill = bars[-1]
        bars[-1] = Bar(fill.time_ms, fill.close_ms, 105, 107, 104, 106)
        fill = bars[-1]
        result = run(bars, SYMBOL, BASE)
        self.assertIsNotNone(result.open_trade)
        self.assertEqual(len(result.trades), 0)
        output = cash_bridge(result, fill)
        self.assertAlmostEqual(output["spread_including_mark_usd"], BASE.spread * result.open_trade.signal.qty)
        roll = period(result, trigger.close_ms, fill.close_ms)
        self.assertEqual(roll["closed_trades"], 0)
        self.assertAlmostEqual(roll["mark_to_market_pnl_usd"], output["net_mark_to_market_usd"])
