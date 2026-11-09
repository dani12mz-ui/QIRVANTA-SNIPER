import json
import unittest
from copy import deepcopy
from decimal import Decimal as D

from analyze import REPO
from compare_h1 import BOUNDARY, cohort_name, compare, phases


class ChronologyTests(unittest.TestCase):
    def test_exact_boundary_belongs_to_observed_confirmation(self):
        self.assertEqual(cohort_name(BOUNDARY - 1), "AUG_SEP_DEVELOPMENT")
        self.assertEqual(cohort_name(BOUNDARY), "OCT_NOV_ALREADY_OBSERVED_CONFIRMATION")

    def test_cross_boundary_position_keeps_entry_cohort_and_split_exit_cash(self):
        c = {"plan_id": "synthetic", "entry_time": BOUNDARY - 100, "last_exit_time": BOUNDARY + 100, "quantity": D(3), "gross": D(15), "commission": D("1.05"), "net": D("13.95"), "net_r": D(".465")}
        rows = [{"exit_time": BOUNDARY - 10, "trade_index": 0, "size": 1, "gross_cash_from_prices": 10, "commission": D(".35"), "net_cash": D("9.65")}, {"exit_time": BOUNDARY + 100, "trade_index": 1, "size": 2, "gross_cash_from_prices": 5, "commission": D(".70"), "net_cash": D("4.30")}]
        p = phases({"campaigns": [{"exit_rows": rows}]}, {"campaigns": [c]})
        early, late = p["AUG_SEP_DEVELOPMENT"], p["OCT_NOV_ALREADY_OBSERVED_CONFIRMATION"]
        self.assertEqual(early["entry_cohort_complete_campaign_metrics"]["net"], D("13.95"))
        self.assertEqual(late["entry_cohort_complete_campaign_metrics"]["n"], 0)
        self.assertEqual(early["realized_exit_period"]["net_cash"], D("9.65"))
        self.assertEqual(late["realized_exit_period"]["net_cash"], D("4.30"))
        self.assertEqual(len(p["cross_boundary_campaigns"]), 1)
        self.assertIsNone(late["realized_exit_period"]["native_equity_drawdown"])

    def test_same_actual_run_has_zero_portfolio_delta(self):
        path = REPO / "diagnostics/accounting/B-ALL.audit.json"
        base = json.loads(path.read_text(encoding="utf-8"), parse_float=D)
        result = compare(base, deepcopy(base))
        self.assertEqual(result["delta_net_cash"], 0)
        self.assertEqual(result["portfolio_difference"]["common_count"], 72)
        self.assertEqual(result["portfolio_difference"]["added_count"], 0)
        self.assertEqual(result["portfolio_difference"]["removed_count"], 0)
        self.assertFalse(result["historical_criteria"]["net_cash_improved"])

    def test_changed_dataset_is_rejected_before_comparing_returns(self):
        path = REPO / "diagnostics/accounting/B-ALL.audit.json"
        base = json.loads(path.read_text(encoding="utf-8"), parse_float=D)
        changed = deepcopy(base); changed["run"]["checksum1"] += 1
        with self.assertRaises(ValueError):
            compare(base, changed)


if __name__ == "__main__":
    unittest.main()
