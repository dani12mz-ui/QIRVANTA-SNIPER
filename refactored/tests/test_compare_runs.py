"""Paired replay checks use handwritten synthetic fixtures, never market evidence."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.compare_runs import main

FIXTURES = Path(__file__).parent/'fixtures'


class PairedReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bars = self.root/'bars.csv'
        self.bars.write_bytes((FIXTURES/'synthetic_ohlc.csv').read_bytes())
        self.manifest = json.loads((FIXTURES/'synthetic_manifest.json').read_text(encoding='utf-8'))
        self.event = json.loads((FIXTURES/'synthetic_events.jsonl').read_text(encoding='utf-8'))
        self.save_manifest('baseline', self.manifest)
        self.save_manifest('fixed', self.manifest)
        self.save_events('baseline', [self.event])
        self.save_events('fixed', [self.event])

    def save_manifest(self, name, manifest):
        (self.root/f'{name}_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')

    def save_events(self, name, rows):
        (self.root/f'{name}_events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in rows), encoding='utf-8')

    def args(self, out='out'):
        return ['--bars', str(self.bars),
                '--baseline-events', str(self.root/'baseline_events.jsonl'),
                '--fixed-events', str(self.root/'fixed_events.jsonl'),
                '--baseline-manifest', str(self.root/'baseline_manifest.json'),
                '--fixed-manifest', str(self.root/'fixed_manifest.json'),
                '--out', str(self.root/out)]

    def test_same_synthetic_inputs_complete_pair_and_preserve_outputs(self):
        self.assertEqual(main(self.args()), 0)
        report = json.loads((self.root/'out/comparison.json').read_text(encoding='utf-8'))
        self.assertEqual(report['status'], 'SYNTHETIC_FUNCTIONAL_ONLY')
        self.assertFalse(report['profitability_improvement_claimed'])
        self.assertEqual(report['out_of_sample'], 'NOT_ESTABLISHED')
        self.assertIn('NOT_REGENERATED', report['signal_generation'])
        self.assertEqual(report['candidate_counts'], {'baseline':1, 'fixed':1})
        self.assertEqual(report['books']['RAW']['delta_fixed_minus_baseline']['net_r'], 0)
        for name in ('baseline', 'fixed'):
            for filename in ('metrics.json', 'trades.jsonl', 'trades.csv', 'events.jsonl'):
                self.assertTrue((self.root/'out'/name/filename).is_file())
        comparison = json.loads((self.root/'out/candidate_comparison.json').read_text(encoding='utf-8'))
        self.assertTrue(comparison['match'])

    def test_different_core_hashes_allowed_and_retained(self):
        for name, code_hash in [('baseline','a'*64), ('fixed','b'*64)]:
            m, e = copy.deepcopy(self.manifest), copy.deepcopy(self.event)
            m['pine_core_sha256'] = code_hash
            e['core_sha256'] = code_hash
            self.save_manifest(name, m)
            self.save_events(name, [e])
        self.assertEqual(main(self.args()), 0)
        report = json.loads((self.root/'out/comparison.json').read_text(encoding='utf-8'))
        self.assertNotEqual(report['baseline_core_sha256'], report['fixed_core_sha256'])

    def test_different_manifest_feed_and_data_configuration_rejected_before_runs(self):
        variants = [dict(provider='OTHER PROVIDER'), dict(symbol='OTHER:SYMBOL'),
                    dict(timeframe_seconds=300), dict(dataset_kind='real'),
                    dict(config={**self.manifest['config'], 'tick':.01}),
                    dict(config={**self.manifest['config'], 'funding_long':1}),
                    dict(pine_inputs={'spreadAssumption':999})]
        for i, update in enumerate(variants):
            m = copy.deepcopy(self.manifest)
            m.update(update)
            self.save_manifest('fixed', m)
            with self.subTest(update=update), patch('tools.compare_runs.validate') as run:
                self.assertEqual(main(self.args(f'rejected_{i}')), 2)
                run.assert_not_called()
                self.assertFalse((self.root/f'rejected_{i}/comparison.json').exists())

    def test_different_exported_cost_rejected_before_runs(self):
        e = copy.deepcopy(self.event)
        e['roundtrip_cost_price'] = .6
        self.save_events('fixed', [e])
        with patch('tools.compare_runs.validate') as run:
            self.assertEqual(main(self.args()), 2)
            run.assert_not_called()

    def test_full_validator_rejects_event_timeframe_mismatch(self):
        e = copy.deepcopy(self.event)
        e['timeframe'] = '60'
        self.save_events('fixed', [e])
        self.assertEqual(main(self.args()), 2)
        self.assertTrue((self.root/'out/fixed/status.json').is_file())
        self.assertFalse((self.root/'out/comparison.json').exists())

    def test_full_validator_rejects_event_hash_mismatch(self):
        e = copy.deepcopy(self.event)
        e['core_sha256'] = 'WRONG'
        self.save_events('fixed', [e])
        self.assertEqual(main(self.args()), 2)
        self.assertFalse((self.root/'out/comparison.json').exists())

    def test_changed_candidate_prices_are_reported_not_hidden(self):
        e = copy.deepcopy(self.event)
        e['tp1'] = 111
        self.save_events('fixed', [e])
        self.assertEqual(main(self.args()), 0)
        comparison = json.loads((self.root/'out/candidate_comparison.json').read_text(encoding='utf-8'))
        self.assertFalse(comparison['match'])
        self.assertEqual(len(comparison['changed']), 1)

    def test_previous_evidence_not_overwritten(self):
        folder = self.root/'out'
        folder.mkdir()
        old = folder/'comparison.json'
        old.write_text('old evidence', encoding='utf-8')
        self.assertEqual(main(self.args()), 2)
        self.assertEqual(old.read_text(encoding='utf-8'), 'old evidence')

    def test_same_shared_ohlc_file_must_match_declared_duration(self):
        self.bars.write_text('time,time_close,open,high,low,close\n0,300000,100,104,96,101\n', encoding='utf-8')
        self.assertEqual(main(self.args()), 2)
        self.assertFalse((self.root/'out/comparison.json').exists())


if __name__ == '__main__':
    unittest.main()
