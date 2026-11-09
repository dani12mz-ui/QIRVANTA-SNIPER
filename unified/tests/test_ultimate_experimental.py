import re
import unittest

from tools.build_ultimate import OUTPUT, assemble, champion_bytes, observation


ORDERS = re.compile(r'^\s*strategy\.(?:entry|order|exit|close|close_all|cancel|cancel_all)\(.*$', re.M)


class UltimateExperimentalTests(unittest.TestCase):
    def setUp(self):
        self.champion = champion_bytes()
        self.candidate = OUTPUT.read_bytes()

    def test_generated_source_and_exact_native_order_calls(self):
        self.assertEqual(assemble(self.champion), self.candidate)
        self.assertEqual(ORDERS.findall(self.champion.decode()), ORDERS.findall(self.candidate.decode()))

    def test_single_default_off_gate_runs_after_native_selection(self):
        source = self.candidate.decode()
        self.assertEqual(source.count('ultimateF1'), 2)
        self.assertIn('bool ultimateF1 = input.bool(false,', source)
        admission = source.index('string nativeAdmissionReason =')
        veto = source.index('if ultimateF1 and nativeAdmissionReason == ""')
        enter = source.index('strategy.entry(', veto)
        self.assertLess(admission, veto)
        self.assertLess(veto, enter)
        self.assertIn('selectedNativePlan.family == 2', source[veto:enter])
        self.assertIn('signedBody < 0.55 * candleRange', source[veto:enter])

    def test_r_display_has_no_dependency_into_trade_state(self):
        source = self.candidate.decode()
        panel = source.index('// Presentation only:')
        self.assertNotIn('f_ultimateR', source[:panel])
        for tp in ('tp1', 'tp2', 'tp3'):
            self.assertIn(f'f_ultimateR(shownEntry, shownInitialStop, shownPlan.{tp}, shownPlan.side)', source)
        self.assertIn('initialRisk > 0 and reward > 0', source)
        for mode in ('MINIMAL', 'STANDARD', 'PRO', 'DEBUG'):
            self.assertIn(f'"{mode}"', source)

    def test_observer_adds_read_only_snapshot_at_fixed_cutoff(self):
        buffer = observation(self.candidate)
        self.assertTrue(buffer.startswith(self.candidate))
        suffix = buffer[len(self.candidate):].decode()
        self.assertEqual(ORDERS.findall(suffix), [])
        self.assertIn('time_close == dgCutoff', suffix)
        self.assertIn('09 Nov 2026 02:45 +0000', suffix)
        self.assertIn('"ultimate_f1_on|" : "ultimate_f1_off|"', suffix)


if __name__ == '__main__':
    unittest.main()
