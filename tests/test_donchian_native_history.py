"""Regression tests for the full observed Donchian execution ledger."""

from datetime import UTC, datetime
from unittest import TestCase

from research.donchian_20_10.history.native_ledger import _month_offset, run


class DonchianNativeHistoryTest(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frames = run()["frames"]

    def test_complete_native_ledgers_reconcile_equity_and_closed_cash(self):
        d1, h4 = self.frames["D1"], self.frames["H4"]
        self.assertEqual(d1["status_counts"], {"CLOSED": 79, "OPEN_CENSORED": 1})
        self.assertEqual(h4["status_counts"], {"CLOSED": 164})
        self.assertEqual(d1["scoring"]["closed_net_usd"], 2762.16739)
        self.assertEqual(h4["scoring"]["closed_net_usd"], 6469.95547)
        self.assertAlmostEqual(float(d1["last_chart_equity_usd"]), 102754.01595, places=7)
        self.assertEqual(len(d1["decimal_nearest_tick_differences"]), 2)
        self.assertEqual(len(h4["native_entry_bar_stops"]), 4)
        self.assertEqual(h4["scoring"]["mark_to_market_drawdown"], "UNAVAILABLE_FROM_NATIVE_EVENTS")

    def test_annual_cash_balances_carry_while_campaigns_group_by_nominal_exit(self):
        for frame in self.frames.values():
            for previous, current in zip(frame["annual"], frame["annual"][1:]):
                self.assertEqual(previous["end_realized_balance_usd"], current["start_realized_balance_usd"])
            self.assertAlmostEqual(sum(row["closed_net_usd"] for row in frame["annual"]),
                                   frame["scoring"]["closed_net_usd"], places=7)
            self.assertTrue(frame["annual"][0]["partial"])
            self.assertTrue(frame["annual"][-1]["partial"])
            self.assertGreater(len(frame["rolling_12_month"]), 30)
            for row in frame["rolling_12_month"]:
                self.assertLessEqual(row["calendar_exposure_nominal_pct_lower"],
                                     row["calendar_exposure_nominal_pct_upper"])

    def test_calendar_offset_keeps_time_and_clamps_leap_day(self):
        leap = int(datetime(2024, 2, 29, 22, tzinfo=UTC).timestamp() * 1000)
        expected = int(datetime(2023, 2, 28, 22, tzinfo=UTC).timestamp() * 1000)
        self.assertEqual(_month_offset(leap, -12), expected)
