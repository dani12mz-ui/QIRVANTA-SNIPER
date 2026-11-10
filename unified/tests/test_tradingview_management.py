"""Static integration tests and explicit arithmetic fixtures. No Pine compiler."""
import hashlib
from decimal import Decimal
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "unified/tools"))
from tradingview_management import patch_management


MARKER = '// One native portfolio.'


class ManagementContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        production = ROOT/'unified/QIRVANTA_UNIFIED.pine'
        cls.original_bytes = production.read_bytes()
        cls.champion = cls.original_bytes.decode('utf-8')
        cls.component = (ROOT/'unified/experimental/tradingview/management.pineinc').read_text(encoding='utf-8')
        cls.composed = cls.champion.replace(MARKER, cls.component + '\n' + MARKER)
        cls.result = patch_management(cls.composed, cls.component)
        cls.native = cls.result.split(MARKER, 1)[1]

    def test_champion_hash_and_shadow_prefix_preserved(self):
        self.assertEqual(hashlib.sha256(self.original_bytes).hexdigest(),
                         '92307768be68946dc87cbe4ea45a8b5e32446fd3b7a0c1a38dce40dffe358be3')
        self.assertEqual(self.composed.split(MARKER, 1)[0], self.result.split(MARKER, 1)[0])
        self.assertIn('bool tvManagement = input.bool(false,', self.component)
        self.assertIn('Plan p = original', self.component)
        self.assertIn('if tvManagement and not na(original)\n        p := original.copy()', self.component)
        self.assertIn('bool tvUsesBaselineLock = not tvManagement or', self.component)
        self.assertIn('if note == "TP1" and not na(nativeRisk) and tvUsesBaselineLock', self.native)

    def test_order_identity_reservations_and_native_size_remain_consistent(self):
        self.assertEqual(self.result.count('strategy.entry('), 1)
        self.assertEqual(self.result.count('strategy.exit('), 6)
        self.assertIn('qty=nativeCampaignQty, limit=tvOrderLimit,', self.native)
        self.assertIn('float limitPrice = na', self.component)
        self.assertIn('tvManagement ? 3.0 * nativeUnitQty : 3.0 * nativeLegQty', self.native)
        for leg, qty in ((1, 'nativeLegQty'), (2, 'nativeLeg2Qty'), (3, 'nativeLeg3Qty')):
            anchor = f'strategy.exit(nativePlan.id + "|T{leg}", from_entry=nativePlan.id, qty={qty},'
            self.assertEqual(self.native.count(anchor), 2)
            self.assertEqual(self.native.count(f'oca_name=nativePlan.id + "|BRACKET_{leg}"'), 2)

    def test_structural_targets_are_unique_forward_levels_and_missing_levels_veto(self):
        for level in ('lastPivotHigh', 'lastPivotLow', 'priorDayHigh', 'priorDayLow', 'lastAsianH', 'lastAsianL'):
            self.assertIn(level, self.component)
        self.assertIn('p.tp2 := f_tvNextStructure(levels, p.tp1, side)', self.component)
        self.assertIn('p.tp3 := f_tvNextStructure(levels, p.tp2, side)', self.component)
        self.assertIn('p.targetBasis := "STRUCTURE"', self.component)
        self.assertIn('else if missingLevels', self.component)
        self.assertIn('else if not f_validPlan(p)', self.component)
        self.assertNotIn('request.', self.component)
        self.assertNotIn('ta.', self.component)

    def test_protection_uses_observed_fills_and_stop_tightens_in_both_directions(self):
        for part in (1, 2):
            self.assertIn(f'if tvManagement and note == "TP{part}"\n                        nativeTvTP{part} := true',
                          self.native)
        observe = self.native.index('nativeSeenClosed := strategy.closedtrades')
        adjust = self.native.index('= f_tvManagedStop(nativePlan')
        issue = self.native.index('strategy.exit(nativePlan.id + "|T1"')
        self.assertLess(observe, adjust)
        self.assertLess(adjust, issue)
        self.assertIn('(nextStop - oldStop) * side >= syminfo.mintick * 0.5', self.component)
        self.assertIn('(close - nextStop) * side >= syminfo.mintick * 0.5', self.component)
        self.assertIn('bar_index - fillBar >= 1 and tvContiguousClose', self.component)
        self.assertNotIn('immediately=true', self.native)

    def test_experimental_pending_expiration_does_not_erase_observed_fill(self):
        self.assertIn('bar_index + tvLimitBars', self.component)
        self.assertIn('time_close >= testEnd', self.component)
        self.assertIn('not nativeEntryObserved and (bar_index >= nativePlan.expiresAfterBar', self.native)
        self.assertLess(self.native.index('if strategy.closedtrades > nativeSeenClosed'),
                        self.native.index('string tvPendingExplanation'))
        self.assertEqual(self.native.count('f_tvNativeOpenRejection(nativePlan,'), 2)

    def test_mutated_immutable_anchor_fails_loudly(self):
        drift = self.composed.replace('nativeLegQty := nativeUnitQty', 'nativeLegQty := 1.0', 1)
        with self.assertRaisesRegex(ValueError, 'management anchor'):
            patch_management(drift)


class ManagementArithmeticFixtures(unittest.TestCase):
    def test_equal_vs_weighted_size_examples(self):
        allocation = (Decimal('.5'), Decimal('.3'), Decimal('.2'))
        for total, grid, feasible in ((3, '1', False), (30, '1', True), (3, '.1', True)):
            parts = tuple(total * fraction for fraction in allocation)
            step = Decimal(grid)
            self.assertEqual(sum(parts), Decimal(total))
            self.assertEqual(all(x >= step and x % step == 0 for x in parts), feasible)

    def test_directional_tightening_fixture(self):
        # Expected numeric examples using price units, independent of the Pine parser.
        for side, old, market, proposal, expected in (
                (1, 99, 103, 100, 100), (1, 99, 103, 98, 99), (1, 99, 103, 104, 99),
                (-1, 101, 97, 100, 100), (-1, 101, 97, 102, 101), (-1, 101, 97, 96, 101)):
            chosen = proposal if (proposal-old)*side > 0 and (market-proposal)*side > 0 else old
            self.assertEqual(chosen, expected)


if __name__ == '__main__':
    unittest.main()
