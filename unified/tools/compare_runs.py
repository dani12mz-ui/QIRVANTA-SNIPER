#!/usr/bin/env python3
"""Replay baseline/fixed exported candidates against one OHLC file and equal costs.

Both logs use the same current Python execution reference. Candidate discovery is
external Pine input; this command neither runs Pine nor establishes OOS evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.compare_events import compare
from tools.validate import load_events, main as validate

EQUAL_MANIFEST_FIELDS = ('dataset_kind', 'provider', 'symbol', 'timeframe_seconds',
                         'config', 'pine_inputs')
SUMMARY_METRICS = ('closed', 'positive', 'negative', 'zero', 'gross_r', 'cost_r',
                   'net_r', 'mean_net_r', 'profit_factor',
                   'closed_equity_max_drawdown_r', 'mean_holding_bars')


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')


def fingerprint(paths):
    return {name: {'path': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
            for name, path in paths.items()}


def check_paired_inputs(baseline, fixed, baseline_events, fixed_events):
    for field in EQUAL_MANIFEST_FIELDS:
        if field not in baseline or field not in fixed:
            raise ValueError(f'Missing paired manifest field: {field}')
        if baseline[field] != fixed[field]:
            raise ValueError(f'Paired manifests must have identical {field}')
    # Pine freezes one round-trip price cost for a run. A different exported cost
    # must not escape validation just because the manifests look identical.
    profiles = []
    for name, rows in (('baseline', baseline_events), ('fixed', fixed_events)):
        costs = set()
        for event in rows:
            if event.get('event') == 'CONFIRMED' and event.get('book') == 'CORE':
                cost = float(event['roundtrip_cost_price'])
                if not math.isfinite(cost) or cost < 0:
                    raise ValueError(f'Invalid {name} exported cost')
                costs.add(cost)
        if len(costs) > 1:
            raise ValueError(f'{name} log contains mixed round-trip price costs')
        profiles.append(costs)
    if profiles[0] and profiles[1] and profiles[0] != profiles[1]:
        raise ValueError('Paired logs must use identical exported round-trip price costs')
    return {'baseline_observed_roundtrip_cost_price': sorted(profiles[0]),
            'fixed_observed_roundtrip_cost_price': sorted(profiles[1]),
            'zero_candidate_note': 'An empty candidate cohort has no observed plan cost; manifest settings are still required to match.'}


def summarize(baseline, fixed):
    result = {}
    for name in baseline['books']:
        a, b = baseline['books'][name], fixed['books'][name]
        result[name] = {
            'baseline': {key: a[key] for key in SUMMARY_METRICS},
            'fixed': {key: b[key] for key in SUMMARY_METRICS},
            'delta_fixed_minus_baseline': {
                key: b[key]-a[key] if a[key] is not None and b[key] is not None else None
                for key in SUMMARY_METRICS},
            'baseline_open_phase': a['open_phase'],
            'fixed_open_phase': b['open_phase'],
            'baseline_ledger': a['ledger'],
            'fixed_ledger': b['ledger'],
        }
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bars', type=Path, required=True)
    parser.add_argument('--baseline-events', type=Path, required=True)
    parser.add_argument('--fixed-events', type=Path, required=True)
    parser.add_argument('--baseline-manifest', type=Path, required=True)
    parser.add_argument('--fixed-manifest', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(argv)
    # Preserve previous evidence and avoid treating stale metrics as a new success.
    if args.out.exists() and (not args.out.is_dir() or any(args.out.iterdir())):
        print('Use a new or empty output directory; previous evidence is preserved.', file=sys.stderr)
        return 2
    args.out.mkdir(parents=True, exist_ok=True)
    paths = {name: getattr(args, name) for name in
             ('bars', 'baseline_events', 'fixed_events', 'baseline_manifest', 'fixed_manifest')}
    try:
        initial_fingerprints = fingerprint(paths)
        baseline_manifest = json.loads(args.baseline_manifest.read_text(encoding='utf-8-sig'))
        fixed_manifest = json.loads(args.fixed_manifest.read_text(encoding='utf-8-sig'))
        baseline_events = load_events(args.baseline_events)
        fixed_events = load_events(args.fixed_events)
        costs = check_paired_inputs(baseline_manifest, fixed_manifest, baseline_events, fixed_events)
        runs = {}
        for name in ('baseline', 'fixed'):
            rc = validate(['--bars', str(args.bars),
                           '--events', str(paths[f'{name}_events']),
                           '--manifest', str(paths[f'{name}_manifest']),
                           '--out', str(args.out/name)])
            if rc != 0:
                raise ValueError(f'{name} validation failed; see {name}/status.json')
            runs[name] = json.loads((args.out/name/'metrics.json').read_text(encoding='utf-8'))
        if fingerprint(paths) != initial_fingerprints:
            raise ValueError('Input files changed during paired replay; results are not comparable')
        candidates = compare(baseline_events, fixed_events)
        candidates['match'] = not (candidates['only_a'] or candidates['only_b'] or
                                   candidates['changed'] or candidates['empty_comparison'])
        write_json(args.out/'candidate_comparison.json', candidates)
        synthetic = baseline_manifest['dataset_kind'] == 'synthetic'
        report = {
            'status': 'SYNTHETIC_FUNCTIONAL_ONLY' if synthetic else 'REAL_DESCRIPTIVE_REPLAY_NOT_OUT_OF_SAMPLE',
            'scope': 'PAIRED_EXPORTED_CANDIDATES_SAME_PYTHON_EXECUTION_REFERENCE',
            'signal_generation': 'EXPORTED_PINE_INPUT_NOT_REGENERATED_IN_PYTHON',
            'pine_runtime_parity': 'NOT_VERIFIED',
            'out_of_sample': 'NOT_ESTABLISHED',
            'profitability_improvement_claimed': False,
            'inputs': initial_fingerprints,
            'equal_manifest_fields': list(EQUAL_MANIFEST_FIELDS),
            'baseline_core_sha256': baseline_manifest['pine_core_sha256'],
            'fixed_core_sha256': fixed_manifest['pine_core_sha256'],
            'cost_checks': costs,
            'candidate_counts': {name: run['candidate_count'] for name, run in runs.items()},
            'candidate_comparison': 'candidate_comparison.json',
            'run_metrics': {name: f'{name}/metrics.json' for name in runs},
            'books': summarize(runs['baseline'], runs['fixed']),
            'limitations': [
                'Both runs share the same provided OHLC file and current Python execution reference.',
                'A fixed data file does not independently prove the provenance or completeness of either Pine export.',
                'Metrics are descriptive; synthetic cases are not market evidence and real cases do not constitute untouched OOS or forward testing.',
            ],
        }
        write_json(args.out/'comparison.json', report)
        print(json.dumps({'status': report['status'], 'report': str(args.out/'comparison.json')}))
        return 0
    except (ValueError, KeyError, TypeError, OSError) as error:
        write_json(args.out/'status.json', {'status': 'INVALID_PAIRED_INPUT', 'error': str(error)})
        print(str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
