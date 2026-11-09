import unittest
import json
from decimal import Decimal as D

from analyze import REPO, RUNS, analyze_audit, classify_exit, diagnose, expected_locked_exit, metrics


def campaign(side=1):
    c = {"state": "COMPLETE", "plan_id": "P", "family": 2, "side": side, "trend_relation": "WITH_HTF", "regime": "TREND", "actual_entry": 100, "submitted_metadata": {"initial_sl": 100 - side * 10, "reference": 100, "atr": 5, "efficiency": .25, "local_aligned": True, "countertrend": False}, "closed_quantity": 3, "realized_gross_cash": 30, "realized_commission_cash": 1.05, "realized_net_cash": 28.95, "net_r": .965, "initial_risk_cash": 30, "holding_bars": 2, "entry_time": 0, "last_exit_time": 2, "exit_rows": []}
    return c


def part(comment, quantity, price, bar, index=0):
    return {"exit_comment": comment, "size": quantity, "exit_price": price, "exit_bar_index": bar, "exit_time": bar, "trade_index": index, "gross_cash_from_prices": 0, "commission": 0, "net_cash": 0}


class ExitAttribution(unittest.TestCase):
    def test_full_stop_keeps_all_three_units(self):
        c = campaign(); c["exit_rows"] = [part("SL", 1, 89.998, 1, i) for i in range(3)]
        r = classify_exit(c, D(".001"))
        self.assertEqual(r["exit_class"], "SL_FULL")
        self.assertEqual(r["quantity_by_exit_comment"], {"SL": D(3)})

    def test_later_locked_sl_checks_both_direction_and_fill_slippage(self):
        for side in (1, -1):
            c = campaign(side)
            expected = expected_locked_exit(c, D(".001"))
            self.assertEqual(expected, D("100.498") if side == 1 else D("99.502"))
            c["exit_rows"] = [part("TP1", side, 100 + side * 10, 1), part("SL", side * 2, expected, 2, 1)]
            r = classify_exit(c, D(".001"))
            self.assertEqual(r["exit_class"], "TP1_THEN_LOCKED_SL_CONSISTENT")
            self.assertEqual(r["quantity_by_exit_comment"]["SL"], 2)

    def test_samebar_tp_and_sl_is_not_called_locked(self):
        c = campaign(); c["exit_rows"] = [part("TP1", 1, 110, 1), part("SL", 2, 89.998, 1, 1)]
        self.assertEqual(classify_exit(c, D(".001"))["exit_class"], "TP_AND_SAMEBAR_SL")

    def test_later_gap_through_amended_stop_does_not_falsely_match_price(self):
        c = campaign(); c["exit_rows"] = [part("TP1", 1, 110, 1), part("SL", 2, 95, 3, 1)]
        self.assertEqual(classify_exit(c, D(".001"))["exit_class"], "TP_THEN_OTHER_SL")

    def test_time_exit_preserves_prior_tp_quantity(self):
        c = campaign(); c["exit_rows"] = [part("TP1", 1, 110, 1), part("ИЗТЕКЪЛ СРОК", 2, 105, 49, 1)]
        r = classify_exit(c, D(".001"))
        self.assertEqual(r["exit_class"], "TIME")
        self.assertEqual(r["quantity_by_exit_comment"], {"TP1": 1, "ИЗТЕКЪЛ СРОК": 2})

    def test_gap_label_does_not_assert_true_gap_cause(self):
        c = campaign(); c["exit_rows"] = [part("ИЗХОД СЛЕД ГАП", 3, 100, 2)]
        self.assertEqual(classify_exit(c, D(".001"))["exit_class"], "GAP_LABEL")

    def test_quantity_mismatch_is_rejected(self):
        c = campaign(); c["exit_rows"] = [part("SL", 2, 90, 1)]
        with self.assertRaises(ValueError):
            classify_exit(c, D(".001"))


class MetricsAttribution(unittest.TestCase):
    def valid_campaign(self):
        c = campaign(); c["exit_rows"] = [part("SL", 3, 90, 1)]
        return c

    def test_efficiency_boundary_is_fixed_and_missing_is_separate(self):
        c = self.valid_campaign()
        self.assertEqual(diagnose(c, {"mintick": .001})["efficiency_bin"], "GE_0.25")
        c["submitted_metadata"]["efficiency"] = None
        self.assertEqual(diagnose(c, {"mintick": .001})["efficiency_bin"], "MISSING")

    def test_adverse_deviation_is_positive_in_both_directions(self):
        for side in (1, -1):
            c = campaign(side); c["actual_entry"] = 100 + side
            c["exit_rows"] = [part("SL", 3 * side, 90, 1)]
            r = diagnose(c, {"mintick": .001})
            self.assertEqual(r["signed_deviation_atr"], D(".2"))
            self.assertEqual(r["signed_deviation_planned_r"], D(".1"))
            self.assertEqual(r["abs_atr_deviation_bin"], "LE_0.20")
            self.assertEqual(r["signed_r_deviation_bin"], "GT_0_LE_0.10")

    def test_fees_are_subtracted_once_not_added_to_already_net(self):
        r = diagnose(self.valid_campaign(), {"mintick": .001})
        m = metrics([r])
        self.assertEqual(m["gross"], 30)
        self.assertEqual(m["commission"], D("1.05"))
        self.assertEqual(m["net"], D("28.95"))

    def test_partial_campaign_is_not_in_complete_metrics(self):
        c = self.valid_campaign(); c["state"] = "PARTIALLY_OPEN"
        with self.assertRaises(ValueError):
            diagnose(c, {"mintick": .001})

    def test_small_n_and_fee_flip_are_explicit(self):
        c = self.valid_campaign()
        c.update(realized_gross_cash=.5, realized_commission_cash=1.05, realized_net_cash=-.55, net_r=-.018333333)
        m = metrics([diagnose(c, {"mintick": .001})])
        self.assertTrue(m["small_n"])
        self.assertEqual(m["gross_positive_but_net_nonpositive"], 1)
        self.assertEqual(m["losses"], 1)

    def test_unreconciled_audit_cannot_supply_performance_diagnosis(self):
        with self.assertRaises(ValueError):
            analyze_audit({"status": "INCOMPLETE"})
        with self.assertRaises(ValueError):
            analyze_audit({"status": "PASS_OBSERVED_JOURNAL", "issues": [], "cash_reconciliation": {"matches": False}})

    def test_four_real_journals_reconcile_all_exit_classes_and_fixed_bins(self):
        for name in RUNS:
            path = REPO / "diagnostics/accounting" / (name + ".audit.json")
            source = json.loads(path.read_text(encoding="utf-8"), parse_float=D)
            result = analyze_audit(source)
            n = result["metrics"]["n"]
            net = result["metrics"]["net"]
            for groups in result["groups"].values():
                self.assertEqual(sum(row["n"] for row in groups.values()), n)
                self.assertEqual(sum((row["net"] for row in groups.values()), D(0)), net)
            self.assertEqual(sum((c["quantity"] for c in result["campaigns"]), D(0)), D(3) * n)


if __name__ == "__main__":
    unittest.main()
