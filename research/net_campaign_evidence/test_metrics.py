"""Regression cases for campaign accounting and day-block inference."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import unittest

from research.net_campaign_evidence.metrics import campaign_metrics, marked_drawdown


FIRST_DAY = date(2026, 10, 12)
DAY_MS = 86_400_000


def day_offset(number):
    return FIRST_DAY + timedelta(days=number)


def campaign(name, offset, net_usd, net_r, **other):
    at = datetime.combine(day_offset(offset), datetime.min.time(), timezone.utc)
    return {"id": name, "close_ms": int(at.timestamp() * 1000), "net_usd": net_usd, "net_r": net_r, **other}


class CampaignMetricsTests(unittest.TestCase):
    def test_campaigns_count_once_and_flat_is_narrow(self):
        result = campaign_metrics(
            [campaign("tp1_then_stop", 0, "-4.5", "-.3"),
             campaign("flat_boundary", 0, Decimal("1e-8"), "1e-10"),
             campaign("negative_above_tolerance", 0, "-1.1e-8", "-1.1e-10"),
             campaign("positive_closed", 1, "8", ".5")],
            [day_offset(0), day_offset(1)], block_days=1,
        )
        self.assertEqual((result["closed"], result["wins"], result["losses"], result["flats"]), (4, 1, 2, 1))
        self.assertEqual((result["win_rate"], result["loss_rate"], result["flat_rate"]), (.25, .50, .25))
        self.assertAlmostEqual(result["net_usd"], 3.499999999)
        self.assertAlmostEqual(result["realized_campaign_close_drawdown_usd"], 4.5)

    def test_zero_days_and_day_clusters_are_preserved_and_reproducible(self):
        days = [day_offset(i).isoformat() for i in range(12)]
        rows = [campaign(f"win-{i}", 0, 1, 1) for i in range(3)]
        rows.extend(campaign(f"loss-{i}", 3, -1, -1) for i in range(3))
        rows.extend(campaign(f"win2-{i}", 7, 1, 1) for i in range(3))
        first = campaign_metrics(rows, days, block_days=2, repetitions=500, seed=77)
        second = campaign_metrics(rows, days, block_days=2, repetitions=500, seed=77)
        boot = first["day_block_bootstrap"]
        self.assertEqual(boot, second["day_block_bootstrap"])
        self.assertEqual(boot["status"], "ESTIMATED_DESCRIPTIVE")
        self.assertEqual((boot["day_count"], boot["days_with_campaigns"], boot["zero_trade_days"]), (12, 3, 9))
        self.assertEqual(boot["win_rate_ci95"], [0., 1.])

    def test_one_trading_day_and_no_trades_cannot_support_inference(self):
        days = [day_offset(i) for i in range(12)]
        report = campaign_metrics([campaign("one", 0, "7.45", "0.5")], days, block_days=2)
        self.assertEqual(report["day_block_bootstrap"]["status"], "INSUFFICIENT_DAY_BLOCKS")
        self.assertEqual(report["day_block_bootstrap"]["win_rate_ci95"], [None, None])
        empty = campaign_metrics([], days)
        self.assertEqual(empty["net_usd"], 0.)
        self.assertIsNone(empty["win_rate"])
        self.assertIsNone(empty["profit_factor_usd"])
        self.assertIsNone(empty["realized_campaign_close_drawdown_usd"])

    def test_nonempty_campaign_identity_calendar_and_times_are_checked(self):
        first = campaign("a", 0, 1, 1)
        later = campaign("b", 1, 1, 1)
        cases = (
            ([first, first], [day_offset(0)]),
            ([later], [day_offset(0)]),
            ([later, first], [day_offset(0), day_offset(1)]),
            ([first], [day_offset(1), day_offset(0)]),
            ([{**first, "net_r": -1}], [day_offset(0)]),
        )
        for rows, days in cases:
            with self.subTest(rows=rows, days=days), self.assertRaises(ValueError):
                campaign_metrics(rows, days)

    def test_explicit_session_day_takes_precedence_over_utc_date(self):
        row = campaign("reopening", 0, 5, 1, trading_day=day_offset(1).isoformat())
        result = campaign_metrics([row], [day_offset(1)])
        self.assertEqual((result["closed"], result["wins"]), (1, 1))

    def test_drawdown_can_include_unrealized_losses_and_censored_duration(self):
        report = marked_drawdown([(0, 0), (DAY_MS, 10), (2 * DAY_MS, -2), (5 * DAY_MS, 4)])
        self.assertEqual(report["max_drawdown"], 12.)
        self.assertEqual(report["longest_underwater_ms"], 4 * DAY_MS)
        self.assertTrue(report["still_underwater"])
        self.assertIsNone(report["last_recovery_ms"])
        recovered = marked_drawdown([(0, 5), (3, 2), (5, 5)])
        self.assertEqual(recovered["max_drawdown"], 3.)
        self.assertEqual(recovered["longest_underwater_ms"], 5)
        self.assertFalse(recovered["still_underwater"])
        self.assertEqual(recovered["last_recovery_ms"], 5)


if __name__ == "__main__":
    unittest.main()

