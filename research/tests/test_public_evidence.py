"""Sparse publication must preserve outcomes and refuse unnoticed data expansion."""
from copy import deepcopy
import gzip
import json
from pathlib import Path
import tempfile
import unittest

from research.validation.public_evidence import (
    canonical_json_bytes, digest, export_public_evidence, load_public_journal,
    redaction_counts, resolve_public_record, sanitize_journal,
)


def fixture():
    return {'schema': 'qirvanta.research.backtest.v2', 'config_sha256': 'fixed-config',
            'dataset': {'bars': 3},
            'decisions': [
                {'index': 0, 'raw_setup': False, 'status': 'NO_SETUP', 'reasons': ['NO_RAW_TRIGGER'],
                 'reference': 100.123, 'initial_stop': None, 'structure_target': 102.},
                {'index': 1, 'raw_setup': True, 'status': 'REJECTED', 'reasons': ['STRUCTURAL_STOP_RANGE'],
                 'reference': 101., 'initial_stop': 99., 'structure_target': 103.},
                {'index': 2, 'raw_setup': True, 'status': 'SUBMITTED', 'reasons': [],
                 'reference': 101., 'initial_stop': 99., 'structure_target': 104.}],
            'plans': [{'plan_id': 'loss', 'status': 'COMPLETE', 'net_cash': -5.25, 'fees_paid': 1.05,
                       'exits': [{'price': 99., 'quantity': 3., 'net_cash': -5.25}]}],
            'events': [{'kind': 'ENTRY', 'price': 101.002}],
            'equity_cash_paid': [{'kind': 'ENTRY_FEE', 'net_cash': -.525}],
            'summary': {'net_cash': -5.25, 'complete_plans': 1}}


class PublicEvidenceTests(unittest.TestCase):
    def test_all_decision_prices_removed_without_mutation_or_outcome_loss(self):
        obj = fixture()
        before = deepcopy(obj)
        sparse = sanitize_journal(obj)
        self.assertEqual(obj, before)
        self.assertEqual(sparse['decisions'][0], {'index': 0, 'raw_setup': False, 'status': 'NO_SETUP', 'reasons': ['NO_RAW_TRIGGER']})
        for old, new in zip(obj['decisions'], sparse['decisions']):
            self.assertEqual(new, {k: v for k, v in old.items() if k not in ('reference', 'initial_stop', 'structure_target')})
        for key in obj.keys()-{'decisions'}:
            self.assertEqual(sparse[key], obj[key], key)
        self.assertEqual(sanitize_journal(sparse), sparse)
        self.assertEqual(redaction_counts(obj)['losing_completed_plans_retained'], 1)
        self.assertEqual(redaction_counts(obj)['removed_fields_total'], 9)

    def test_missing_or_non_boolean_true_flag_never_keeps_dense_prices(self):
        for flag in (None, False, True, 0, 1, 'true'):
            with self.subTest(flag=flag):
                obj = fixture()
                obj['decisions'][0]['raw_setup'] = flag
                self.assertNotIn('reference', sanitize_journal(obj)['decisions'][0])

    def test_unknown_dense_fields_or_raw_bar_arrays_fail_closed(self):
        for extra in ('close', 'bid', 'unexpected_feature'):
            obj = fixture(); obj['decisions'][0][extra] = 100.
            with self.assertRaises(ValueError): sanitize_journal(obj)
        obj = fixture(); obj['all_bars'] = [{'open': 100., 'high': 101., 'low': 99., 'close': 100.}]
        with self.assertRaises(ValueError): sanitize_journal(obj)
        obj = fixture(); obj['dataset']['bars'] = [{'open': 100., 'high': 101., 'low': 99., 'close': 100.}]
        with self.assertRaises(ValueError): sanitize_journal(obj)

    def test_export_records_full_to_sparse_hashes_and_preserves_original_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            full = root/'research/reports/candidate_journals/loss.json.gz'
            full.parent.mkdir(parents=True)
            original = gzip.compress(canonical_json_bytes(fixture()), mtime=0)
            full.write_bytes(original)
            report = export_public_evidence(root)
            manifest = root/'research/reports/public_journals_manifest.json'
            record = resolve_public_record(digest(original), manifest)
            self.assertEqual(full.read_bytes(), original)
            self.assertEqual(report['journal_files'], 1)
            self.assertEqual(record['counts']['plans_retained'], 1)
            self.assertEqual(record['counts']['partial_exits_retained'], 1)
            self.assertEqual(load_public_journal(digest(original), manifest, root), sanitize_journal(fixture()))
            self.assertEqual(record['public_json_sha256'], digest(canonical_json_bytes(sanitize_journal(fixture()))))

    def test_export_refuses_existing_outputs_and_loader_rejects_tampering(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            full = root/'research/reports/candidate_journals/loss.json.gz'
            full.parent.mkdir(parents=True)
            original = gzip.compress(canonical_json_bytes(fixture()), mtime=0)
            full.write_bytes(original)
            report = export_public_evidence(root)
            manifest = root/'research/reports/public_journals_manifest.json'
            before = manifest.read_bytes()
            with self.assertRaises(ValueError): export_public_evidence(root)
            self.assertEqual(manifest.read_bytes(), before)
            self.assertEqual(full.read_bytes(), original)
            public = root/report['by_original_full_sha256'][digest(original)]['public_path']
            public.write_bytes(b'tampered')
            with self.assertRaises(ValueError): load_public_journal(digest(original), manifest, root)

    def test_identical_full_bytes_in_two_archives_keep_both_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = gzip.compress(canonical_json_bytes(fixture()), mtime=0)
            for directory in ('research/reports/candidate_journals',
                              'research/reports/method_history/paid_cash_precorrection/candidate_journals'):
                path = root/directory/'same.json.gz'; path.parent.mkdir(parents=True); path.write_bytes(original)
            report = export_public_evidence(root)
            self.assertEqual(report['journal_files'], 2)
            self.assertEqual(report['distinct_original_hashes'], 1)
            record = report['by_original_full_sha256'][digest(original)]
            self.assertEqual(len(record['original_full_paths']), 2)
            self.assertEqual(len(record['public_paths']), 2)
            self.assertEqual((root/record['public_paths'][0]).read_bytes(), (root/record['public_paths'][1]).read_bytes())


if __name__ == '__main__':
    unittest.main()
