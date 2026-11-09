"""Publish sparse outcome evidence while preserving full local journals unchanged.

Exactly three price fields are removed from EVERY decision row:
reference, initial_stop, structure_target. Every row/status/reason/identity remains,
as do all actual trade plans, losses, exits, fees, events and cash curves.
This is a documented publication reduction, not a licensing certification.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / 'research/reports/public_journals_manifest.json'
POLICY_ID = 'qirvanta.sparse_decision_prices.v2'
REMOVED_FIELDS = ('reference', 'initial_stop', 'structure_target')
FULL_DIRECTORIES = (
    'research/reports/candidate_journals',
    'research/reports/method_history/paid_cash_precorrection/candidate_journals',
)
NON_SETUP_FIELDS = frozenset(('signal_id', 'family', 'side', 'index', 'confirmation_time',
                             'raw_setup', 'status', 'reasons', 'regime', 'session',
                             'setup_born_index', 'setup_age', 'selected_families'))
DECISION_FIELDS = NON_SETUP_FIELDS | frozenset(('plan_id', 'opening_session',
                                               'ablation_removed_gate', 'ablation_original_reasons'))
JOURNAL_FIELDS = frozenset(('schema', 'implementation', 'synthetic_candidate_provider',
                          'config', 'config_sha256', 'dataset', 'blocked_families',
                          'decisions', 'plans', 'events', 'equity_closed_cash',
                          'equity_cash_paid', 'summary', 'limitations', 'research_variant'))
DATASET_FIELDS = frozenset(('bars', 'first_open', 'last_close', 'canonical_input_sha256',
                           'interval_ms', 'historical_status'))
SOURCE_FILES = ('research/backtesting/engine.py', 'research/data/loader.py',
                'research/features/causal.py', 'research/strategies/library.py',
                'research/strategies/ablation.py', 'research/validation/common_window.py',
                'research/validation/statistics.py', 'research/validation/run_extensions.py')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(obj):
    """Stable uncompressed representation, independent of gzip platform headers."""
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'),
                      allow_nan=False, sort_keys=True).encode('utf-8')


def sanitize_journal(obj):
    if not isinstance(obj, dict) or obj.get('schema') not in (
            'qirvanta.research.backtest.v1', 'qirvanta.research.backtest.v2'):
        raise ValueError('Unsupported journal schema; review publication policy first')
    if set(obj) - JOURNAL_FIELDS:
        raise ValueError('Unexpected top-level fields require a publication review')
    dataset = obj.get('dataset', {})
    if not isinstance(dataset, dict) or set(dataset) - DATASET_FIELDS:
        raise ValueError('Dataset must contain approved metadata only')
    if 'bars' in dataset and (type(dataset['bars']) is not int or dataset['bars'] < 0):
        raise ValueError('Dataset bars must be a count, never an embedded price array')
    if not isinstance(obj.get('decisions'), list):
        raise ValueError('Expected decision rows')
    sparse = deepcopy(obj)
    for row in sparse['decisions']:
        if not isinstance(row, dict):
            raise ValueError('Malformed decision row')
        for field in REMOVED_FIELDS:
            row.pop(field, None)
        if set(row) - DECISION_FIELDS:
            raise ValueError('Unexpected decision fields may disclose dense prices; review before publication')
        if not isinstance(row.get('reasons', []), list) or not all(
                isinstance(reason, str) for reason in row.get('reasons', [])):
            raise ValueError('Unexpected structured decision reasons require publication review')
    return sparse


def redaction_counts(original):
    rows = original['decisions']
    non_setup = [row for row in rows if row.get('raw_setup') is not True]
    fields = {field: sum(field in row for row in rows) for field in REMOVED_FIELDS}
    return {'decision_rows_retained': len(rows),
            'raw_setup_rows_retained_without_price_fields': len(rows)-len(non_setup),
            'non_setup_rows_retained': len(non_setup),
            'non_setup_rows_with_removed_fields': sum(any(field in row for field in REMOVED_FIELDS) for row in non_setup),
            'all_decision_rows_with_removed_fields': sum(any(field in row for field in REMOVED_FIELDS) for row in rows),
            'removed_field_counts': fields, 'removed_fields_total': sum(fields.values()),
            'plans_retained': len(original.get('plans', [])),
            'losing_completed_plans_retained': sum(p.get('status') == 'COMPLETE' and p.get('net_cash', 0) < 0 for p in original.get('plans', [])),
            'partial_exits_retained': sum(len(p.get('exits', [])) for p in original.get('plans', [])),
            'events_retained': len(original.get('events', []))}


def _public_path(full_path):
    return full_path.parent.with_name('public_journals') / full_path.name


def export_public_evidence(root=ROOT, manifest_path=None):
    root = Path(root).resolve()
    manifest_path = Path(manifest_path) if manifest_path else root/'research/reports/public_journals_manifest.json'
    files = sorted(p for directory in FULL_DIRECTORIES for p in (root/directory).glob('*.json.gz'))
    if not files:
        raise ValueError('No full journals found')
    outputs = [_public_path(p) for p in files]
    for path in [manifest_path, *outputs]:
        if path.exists() or path.is_symlink():
            raise ValueError(f'Refusing to overwrite publication evidence: {path}')
    before_sources = {name: digest((root/name).read_bytes()) for name in SOURCE_FILES if (root/name).exists()}
    records = {}
    total_rows = total_removed = total_plans = total_exits = 0
    for i, (full_path, public_path) in enumerate(zip(files, outputs), 1):
        full_bytes = full_path.read_bytes()
        full_hash = digest(full_bytes)
        original_json = gzip.decompress(full_bytes)
        obj = json.loads(original_json)
        sparse = sanitize_journal(obj)
        counts = redaction_counts(obj)
        # Mutation of any trade/outcome field fails before this file is published.
        for key in obj:
            if key != 'decisions' and sparse[key] != obj[key]:
                raise ValueError(f'Unexpected change outside decision prices: {key}')
        public_json = canonical_json_bytes(sparse)
        public_bytes = gzip.compress(public_json, compresslevel=6, mtime=0)
        public_hash = digest(public_bytes)
        public_path.parent.mkdir(parents=True, exist_ok=True)
        with public_path.open('xb') as stream:
            stream.write(public_bytes)
        if digest(full_path.read_bytes()) != full_hash:
            raise ValueError('Original full journal changed during publication')
        full_name, public_name = full_path.relative_to(root).as_posix(), public_path.relative_to(root).as_posix()
        if full_hash in records:
            record = records[full_hash]
            if record['public_sha256'] != public_hash:
                raise ValueError('Identical original hash produced different public evidence')
            record['original_full_paths'].append(full_name)
            record['public_paths'].append(public_name)
        else:
            records[full_hash] = {
                'original_full_sha256': full_hash, 'original_full_json_sha256': digest(original_json),
                'original_full_path': full_name, 'original_full_paths': [full_name],
                'public_path': public_name, 'public_paths': [public_name],
                'public_sha256': public_hash, 'public_json_sha256': digest(public_json),
                'policy_id': POLICY_ID, 'counts': counts,
                'original_config_sha256': obj.get('config_sha256'),
                'original_research_variant': obj.get('research_variant'),
                'all_non_decision_sections_exactly_preserved': True,
            }
        total_rows += counts['decision_rows_retained']
        total_removed += counts['removed_fields_total']
        total_plans += counts['plans_retained']
        total_exits += counts['partial_exits_retained']
        if i % 25 == 0:
            print(f'Published sparse journal {i}/{len(files)}', flush=True)
    after_sources = {name: digest((root/name).read_bytes()) for name in before_sources}
    if before_sources != after_sources:
        raise ValueError('Economic sources changed during publication')
    report = {
        'schema': 'qirvanta.public_journals_manifest.v1', 'policy_id': POLICY_ID,
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'policy': {'condition': 'Every decision row, including confirmed/rejected raw setups',
                   'remove_only': list(REMOVED_FIELDS),
                   'preserve': 'Every decision row, identity, status, reason and raw-setup flag; all actual trade plans, losing outcomes, exits, fees, events, cash curves, configs and source identities. Raw-candidate-only price levels remain private.',
                   'full_originals': 'Unchanged local candidate_journals directories; recursively Git-ignored.',
                   'scope': 'Sparse publication reduction, not proof of licensing rights or new economic results.'},
        'exporter': {'path': 'research/validation/public_evidence.py', 'sha256': digest(Path(__file__).read_bytes())},
        'economic_source_sha256': before_sources, 'sources_stable_through_export': True,
        'journal_files': len(files), 'distinct_original_hashes': len(records),
        'totals': {'decision_rows_retained': total_rows, 'fields_removed': total_removed,
                   'plans_retained': total_plans, 'partial_exits_retained': total_exits},
        'by_original_full_sha256': records,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open('x', encoding='utf-8', newline='\n') as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    return report


def resolve_public_record(original_full_sha256, manifest_path=None):
    manifest = json.loads(Path(manifest_path or DEFAULT_MANIFEST).read_text(encoding='utf-8'))
    if manifest.get('policy_id') != POLICY_ID:
        raise ValueError('Unsupported publication policy')
    record = manifest['by_original_full_sha256'].get(original_full_sha256)
    if record is None:
        raise ValueError('Original full journal hash has no published sparse counterpart')
    if record.get('original_full_sha256') != original_full_sha256:
        raise ValueError('Publication hash mapping is inconsistent')
    return record


def load_public_journal(original_full_sha256, manifest_path=None, root=None):
    record = resolve_public_record(original_full_sha256, manifest_path)
    root = Path(root or ROOT).resolve()
    path = (root/record['public_path']).resolve()
    if not path.is_relative_to(root):
        raise ValueError('Public evidence path leaves repository')
    data = path.read_bytes()
    if digest(data) != record['public_sha256']:
        raise ValueError('Public compressed journal hash mismatch')
    obj = json.loads(gzip.decompress(data))
    if digest(canonical_json_bytes(obj)) != record['public_json_sha256']:
        raise ValueError('Public canonical journal hash mismatch')
    if sanitize_journal(obj) != obj:
        raise ValueError('Public journal still contains prohibited decision-row prices')
    return obj


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export', action='store_true', required=True)
    args = parser.parse_args()
    report = export_public_evidence()
    print(json.dumps({k: report[k] for k in ('policy_id', 'journal_files', 'distinct_original_hashes', 'totals')}, indent=2))


if __name__ == '__main__':
    main()
