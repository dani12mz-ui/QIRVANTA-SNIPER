"""Synthetic native-record regressions + adapter contracts, NOT Pine runtime proof."""
import unittest
import re
from pathlib import Path
from tools.native_accounting import NativeCampaign, NativeFill, admission_quantity_valid

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = (ROOT / 'src/native_adapter.pineinc').read_text(encoding='utf-8')
ALERT_HELPERS = (ROOT / 'src/alerts_helpers.pineinc').read_text(encoding='utf-8')


def fill(index, px, kind, qty=1.0, entry=100.0, fee=0.35, t_entry=10, t_exit=20, t_known=30):
    return NativeFill(index, 'P1', entry, px, qty, fee, t_entry, t_exit, t_known, kind)


class NativeAccountingTests(unittest.TestCase):
    def test_three_reservations_same_entry_do_not_reclose(self):
        campaign = NativeCampaign('P1', 1, 90, 3)
        data = [fill(0, 110, 'TP1'), fill(1, 120, 'TP2'), fill(2, 130, 'TP3')]
        campaign.observe(data)
        campaign.observe(data)
        r = campaign.report()
        self.assertEqual(r['closed_quantity'], 3)
        self.assertEqual(r['remaining_quantity'], 0)
        self.assertTrue(r['completed'])
        self.assertAlmostEqual(r['net_cash'], 58.95)
        self.assertAlmostEqual(r['gross_r'], 2)
        self.assertAlmostEqual(r['cost_r'], 0.035)
        self.assertAlmostEqual(r['net_r'], 1.965)

    def test_tp1_tp2_same_bar_are_two_events(self):
        c = NativeCampaign('P1', 1, 90, 3)
        c.observe([fill(0, 110, 'TP1'), fill(1, 120, 'TP2')])
        self.assertEqual(c.report()['exit_events'], ['TP1', 'TP2'])
        self.assertEqual(c.report()['effective_times'], [20, 20])
        self.assertEqual(c.report()['known_times'], [30, 30])
        self.assertEqual(c.remaining_quantity, 1)

    def test_gap_loss_remains_actual_fill(self):
        c = NativeCampaign('P1', 1, 90, 3)
        c.observe([fill(0, 80, 'SL', qty=3, entry=95, fee=1.05)])
        self.assertEqual(c.report()['actual_entry'], 95)
        self.assertAlmostEqual(c.report()['net_cash'], -46.05)
        self.assertAlmostEqual(c.report()['net_r'], -3.07)

    def test_nonpositive_actual_risk_is_cash_loss_not_invented_r(self):
        c = NativeCampaign('P1', 1, 90, 3)
        c.observe([fill(0, 80, 'SL', qty=3, entry=85, fee=1.05)])
        self.assertIsNone(c.report()['net_r'])
        self.assertAlmostEqual(c.report()['net_cash'], -16.05)

    def test_entry_and_exit_inside_one_bar_are_kept(self):
        c = NativeCampaign('P1', 1, 90, 3)
        c.observe([fill(0, 90, 'SL', qty=3, fee=1.05, t_entry=100, t_exit=100, t_known=160)])
        self.assertTrue(c.report()['completed'])
        self.assertEqual(c.report()['effective_times'], [100])
        self.assertEqual(c.report()['known_times'], [160])
        self.assertAlmostEqual(c.report()['net_cash'], -31.05)

    def test_remaining_position_matches_partial_native_sizes(self):
        c = NativeCampaign('P1', -1, 110, 6, pointvalue=100)
        c.observe([fill(0, 90, 'TP1', qty=2, fee=0.7)])
        self.assertEqual(c.remaining_quantity, 4)
        self.assertAlmostEqual(c.report()['net_cash'], 1999.3)
        self.assertAlmostEqual(c.report()['net_r'], 1999.3 / 6000)

    def test_no_fabricated_result_without_fill(self):
        c = NativeCampaign('P1', 1, 90, 3)
        r = c.report()
        self.assertIsNone(r['actual_entry'])
        self.assertFalse(r['completed'])
        self.assertEqual(r['exit_events'], [])
        self.assertEqual(r['net_cash'], 0)

    def test_wrong_identity_overclose_and_revised_records_rejected(self):
        c = NativeCampaign('P1', 1, 90, 3)
        with self.assertRaises(ValueError):
            c.observe([NativeFill(0, 'P2', 100, 110, 1, .35, 10, 20, 30, 'TP1')])
        c.observe([fill(0, 110, 'TP1')])
        with self.assertRaises(ValueError):
            c.observe([fill(0, 120, 'TP1')])
        with self.assertRaises(ValueError):
            c.observe([fill(1, 90, 'SL', qty=3)])
        self.assertEqual(c.remaining_quantity, 2)

    def test_no_observation_before_fill(self):
        with self.assertRaises(ValueError):
            NativeCampaign('P1', 1, 90, 3).observe([fill(0, 110, 'TP1', t_known=15)])

    def test_quantity_floor_does_not_increase_risk(self):
        self.assertFalse(admission_quantity_valid(.1, 1))
        self.assertFalse(admission_quantity_valid(1, None))
        self.assertTrue(admission_quantity_valid(1, 1))
        self.assertTrue(admission_quantity_valid(.001, .001))


class NativeAdapterContracts(unittest.TestCase):
    def test_single_entry_path_and_selected_plan_identity(self):
        self.assertEqual(ADAPTER.count('strategy.entry('), 1)
        self.assertIn('nativePlan := selectedNativePlan.copy()', ADAPTER)
        self.assertIn('nativeLastSubmittedId := nativePlan.id', ADAPTER)
        self.assertIn('nativeLastSubmissionBar == bar_index', ADAPTER)
        self.assertIn('"NATIVE_ADMITTED"', ADAPTER)
        self.assertIn('"NATIVE_BLOCKED"', ADAPTER)

    def test_three_independent_brackets_at_submit_and_update(self):
        exits = [x for x in ADAPTER.splitlines() if 'strategy.exit(' in x]
        self.assertEqual(len(exits), 6)
        for leg in range(1, 4):
            calls = [x for x in exits if f'"|T{leg}"' in x]
            self.assertEqual(len(calls), 2)
            for x in calls:
                for token in ('from_entry=nativePlan.id', 'qty=nativeLegQty', 'stop=nativeStop', f'limit=nativePlan.tp{leg}', f'"|BRACKET_{leg}"', 'alert_profit=', 'alert_loss=', 'disable_alert=not (alertsEnabled and alertNativeFills)'):
                    self.assertIn(token, x)
        self.assertIn('nativeCampaignQty := 3.0 * nativeLegQty', ADAPTER)
        self.assertIn('qty=nativeCampaignQty', ADAPTER)

    def test_cash_and_position_have_native_source(self):
        self.assertIn('shownRemainingQty = math.abs(strategy.position_size)', ADAPTER)
        self.assertIn('nativeEntry := strategy.position_avg_price', ADAPTER)
        self.assertIn('str.tostring(strategy.netprofit, "#.##")', ADAPTER)
        self.assertIn('strategy.closedtrades.commission(j)', ADAPTER)
        self.assertNotIn('roundTripPriceCost', ADAPTER)

    def test_samebar_closed_records_observed_before_unfilled_cancellation(self):
        self.assertLess(ADAPTER.index('strategy.closedtrades > nativeSeenClosed'), ADAPTER.index('bar_index >= nativePlan.expiresAfterBar'))
        self.assertIn('nativeEntry := fillEntry', ADAPTER)
        self.assertIn('nativeSeenClosed := strategy.closedtrades', ADAPTER)

    def test_protection_submitted_before_unknown_entry_and_not_retroactive(self):
        block = ADAPTER[ADAPTER.index('strategy.entry('):]
        self.assertIn('strategy.exit(', block)
        self.assertNotIn('strategy.position_avg_price', block)
        self.assertIn('if note == "TP1" and not na(nativeRisk)', ADAPTER)
        self.assertIn('"NATIVE_STOP_SCHEDULED"', ADAPTER)
        self.assertIn('nativeRisk * nativeCampaignQty * syminfo.pointvalue', ADAPTER)
        self.assertNotIn('immediately=true', ADAPTER)

    def test_fill_messages_use_frozen_plan_quantity_and_metadata(self):
        calls = re.findall(r'f_nativeMessage\(([^\n]*?)\)', ADAPTER)
        self.assertEqual(len(calls), 21)
        for call in calls:
            self.assertTrue(call.startswith('nativePlan, nativeCampaignQty, '), call)
        self.assertLess(ADAPTER.index('nativeCampaignQty := 3.0 * nativeLegQty'), ADAPTER.index('strategy.entry('))
        message = ALERT_HELPERS.split('f_nativeMessage(', 1)[1].split('f_isRiskEvent(', 1)[0]
        for field in ('plannedCampaignQty', 'p.maxHoldBarsFrozen', 'p.maxDeviationATRFrozen', 'p.expiresAfterBar', 'p.cancellationReason'):
            self.assertIn(field, message)
        self.assertNotIn('nativeUnitQty', message)
        self.assertNotIn('{{strategy.order.', message)
        self.assertIn('заявен размер на целия план', message)

    def test_time_and_gap_fill_messages_have_specific_titles(self):
        self.assertIn('kind == "NATIVE_EXIT_TIME" ? "СИМУЛИРАН ИЗХОД — ИЗТЕКЪЛ СРОК"', ALERT_HELPERS)
        self.assertIn('kind == "NATIVE_EXIT_GAP" ? "СИМУЛИРАН ИЗХОД — НЕВАЛИДЕН ГАП"', ALERT_HELPERS)


if __name__ == '__main__':
    unittest.main()
