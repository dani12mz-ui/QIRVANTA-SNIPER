"""Regression checks for bounded native RAW campaign accounting."""

from decimal import Decimal
from types import SimpleNamespace
import unittest

from research.net_campaign_evidence.native_raw_metrics import summarize_raw


BAR = 300_000
START = 1_794_355_200_000
PLAN = "S5|OANDA:XAUUSD|5|2|1|1794354900000|1794355200000"
NONE = ""


def event(kind, bar, *, plan=PLAN, fraction=None, gross=None, cost=None):
    at = START + bar * BAR
    return {
        "event": kind, "event_id": f"{kind}|{plan}|{bar}", "book": "RAW",
        "known_time": at, "effective_time": at - BAR if kind == "SIM_ENTRY" else None,
        "plan_id": plan, "side": 1, "price": Decimal(100),
        "quantity_fraction": None if fraction is None else Decimal(fraction),
        "gross_r": None if gross is None else Decimal(gross),
        "cost_r": None if cost is None else Decimal(cost),
        "net_r": None if gross is None else Decimal(gross) - Decimal(cost),
        "reference": Decimal(100), "initial_sl": Decimal(99),
        "tp1": Decimal(101), "tp2": Decimal(102), "tp3": Decimal(103),
        "roundtrip_cost_price": Decimal(".354"),
    }


def snapshot(bar, phase, plan, entries, closed, *, gross="0", costs="0", capacity=0):
    return {
        "known_ms": START + bar * BAR, "phase": phase, "plan_id": plan,
        "entered": entries, "closed": closed, "capacity_skipped": capacity,
        "gross": Decimal(gross), "costs": Decimal(costs), "net": Decimal(gross) - Decimal(costs),
    }


def capture(events, ledger):
    ctx = {row["known_ms"]: SimpleNamespace(opening=row["known_ms"] - BAR) for row in ledger}
    return {"context": ctx, "s5": {"RAW": events, "LEDGER": ledger}}


class NativeRawMetricsTests(unittest.TestCase):
    def test_tp1_followed_by_stop_is_one_cost_net_loss(self):
        rows = [
            event("QUEUED", 0), event("SIM_ENTRY", 1, fraction="1"),
            event("SIM_TP1", 1, fraction=".33333333", gross=".3", cost=".1"),
            event("SIM_SL", 2, fraction=".66666667", gross="-.3", cost=".2"),
            event("CLOSED", 2, fraction="1", gross="0", cost=".3"),
        ]
        ledger = [
            snapshot(0, 1, PLAN, 0, 0), snapshot(1, 2, PLAN, 1, 0),
            snapshot(2, 0, NONE, 1, 1, costs=".3"),
        ]
        result = summarize_raw(capture(rows, ledger))
        self.assertEqual(result["metrics"]["losses"], 1)
        self.assertEqual(result["metrics"]["closed"], 1)
        self.assertEqual(result["metrics"]["net_r"], Decimal("-.3"))
        self.assertEqual(result["campaign_roster"][0]["exit_parts_reconciled"], "MATCH")
        self.assertEqual(result["marked_equity"]["status"], "UNAVAILABLE")

    def test_carry_in_closed_is_not_a_fully_observed_campaign(self):
        rows = [event("SIM_SL", 1, fraction=".66666667", gross="-.3", cost=".2"),
                event("CLOSED", 1, fraction="1", gross="0", cost=".3")]
        ledger = [snapshot(0, 2, PLAN, 5, 4), snapshot(1, 0, NONE, 5, 5, costs=".3")]
        result = summarize_raw(capture(rows, ledger))
        self.assertEqual(result["metrics"]["closed"], 0)
        self.assertEqual(result["carry_in_closes_excluded_from_complete_cohort"], 1)
        self.assertEqual(result["carry_in_closed_net_r_witness"], Decimal("-.3"))
        self.assertEqual(result["campaign_roster"][0]["exit_parts_reconciled"], "NOT_FULLY_WITNESSED")

    def test_partial_at_end_remains_open_and_preserves_realized_part(self):
        rows = [event("QUEUED", 0), event("SIM_ENTRY", 1, fraction="1"),
                event("SIM_TP1", 1, fraction=".33333333", gross=".3", cost=".1")]
        ledger = [snapshot(0, 1, PLAN, 0, 0), snapshot(1, 2, PLAN, 1, 0)]
        result = summarize_raw(capture(rows, ledger))
        record = result["campaign_roster"][0]
        self.assertEqual(record["state"], "OPEN_AT_WINDOW_END")
        self.assertEqual(record["reported_part_totals_r"]["net_r"], Decimal(".2"))
        self.assertEqual(record["remaining_fraction_at_last_ledger"], Decimal(".66666667"))
        self.assertEqual(result["metrics"]["wins"], 0)
        self.assertEqual(result["open_or_pending_at_last_close"], {"phase": 2, "plan_id": PLAN})

    def test_no_fill_and_capacity_are_separate_from_a_position(self):
        blocked = PLAN + "|capacity"
        rows = [event("QUEUED", 0), event("CAPACITY", 0, plan=blocked), event("CANCELLED", 1)]
        ledger = [snapshot(0, 1, PLAN, 0, 0, capacity=1), snapshot(1, 0, NONE, 0, 0, capacity=1)]
        result = summarize_raw(capture(rows, ledger))
        self.assertEqual(result["roster_counts"], {"CANCELLED_ZERO_FILL": 1, "BLOCKED_CAPACITY": 1})
        self.assertEqual(result["metrics"]["closed"], 0)

    def test_rejects_missing_close_record_or_ledger_discrepancy(self):
        rows = [event("QUEUED", 0), event("SIM_ENTRY", 1, fraction="1")]
        ledger = [snapshot(0, 1, PLAN, 0, 0), snapshot(1, 0, NONE, 1, 1)]
        with self.assertRaisesRegex(ValueError, "closed change lacks"):
            summarize_raw(capture(rows, ledger))


if __name__ == "__main__":
    unittest.main()

