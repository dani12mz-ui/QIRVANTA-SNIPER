"""Replay independent engine review on archived/current sources and fixed actual data.

Run from the repository root:
  python -m research.validation.independent_review --output work/review-replay.json

Outputs are never overwritten. These deterministic replay checks are NOT counted
as unittest methods and do not establish native parity or an untouched holdout.
"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
import gzip
import hashlib
import importlib.machinery
import importlib.util
import json
from math import isclose, isfinite
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / 'research/reports/method_history/paid_cash_precorrection'
DATA = ROOT / 'diagnostics/protocol/datasets/B-ALL-FULL/bars.csv'
DATA_SHA = '7015f6386d2627237c57ff8625d59cbe3c9b852e4742b786cbb4679db3d48c56'
ORIGINAL_ENGINE_SHA = 'bbb2c5c17e2f7a6d5d4e467586a733a6c5476a2b5a14bdacc0367ccc0980d3ee'
SOURCES = ('research/backtesting/engine.py', 'research/data/loader.py',
           'research/features/causal.py', 'research/strategies/library.py')
EPSILON = 1e-8


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_module(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses resolve their declaring module here.
    loader.exec_module(module)
    return module


def require(condition, message):
    if not condition:
        raise ValueError(message)


def finite_tree(value):
    if isinstance(value, dict):
        return all(finite_tree(x) for x in value.values())
    if isinstance(value, (tuple, list)):
        return all(finite_tree(x) for x in value)
    return isfinite(value) if isinstance(value, (int, float)) else True


def original_projection(old, new):
    """Compare every old plan/exit field while allowing additive paid-cash fields."""
    if isinstance(old, dict):
        return isinstance(new, dict) and all(k in new and original_projection(v, new[k]) for k, v in old.items())
    if isinstance(old, list):
        return isinstance(new, list) and len(old) == len(new) and all(original_projection(a, b) for a, b in zip(old, new))
    return old == new


def actual_replays(engine, bars):
    config = engine.BacktestConfig(enabled_families=engine.FAMILIES[:9])
    full = engine.run_backtest(bars, config)
    prefixes = []
    finite = finite_tree(full)
    for n in (500, 2500, 5000):
        short = engine.run_backtest(bars[:n], config)
        prefixes.append({'bars': n,
                         'decisions_equal': short['decisions'] == [d for d in full['decisions'] if d['index'] < n],
                         'events_equal': short['events'] == [d for d in full['events'] if d['index'] < n]})
        finite &= finite_tree(short)
    mirror = deepcopy(bars)
    for b in mirror:
        b.update(open=10000-b['open'], close=10000-b['close'],
                 high=10000-b['low'], low=10000-b['high'])
    flipped = engine.run_backtest(mirror, config)
    finite &= finite_tree(flipped)
    plans, mirrored = full['plans'], flipped['plans']
    timing = ('family', 'confirmation_index', 'entry_index', 'final_exit_index', 'status')
    same_timing = len(plans) == len(mirrored) and all(
        all(p[k] == q[k] for k in timing) and p['side'] == -q['side'] for p, q in zip(plans, mirrored))
    max_plan_delta = max([abs(p['net_cash']-q['net_cash']) for p, q in zip(plans, mirrored)] or [0])
    net_delta = full['summary']['net_cash'] - flipped['summary']['net_cash']
    scope = {'status': 'PASS' if finite and same_timing and max_plan_delta < EPSILON and abs(net_delta) < EPSILON
             and all(x['decisions_equal'] and x['events_equal'] for x in prefixes) else 'FAIL',
             'all_numeric_outputs_finite': finite, 'prefix_checks': prefixes,
             'combined_families': list(config.enabled_families),
             'mirror': {'transform': 'P -> 10000 - P; high/low exchanged; timestamps unchanged',
                        'plans': len(plans), 'mirrored_plans': len(mirrored),
                        'same_timing_family_status_opposite_side': same_timing,
                        'net_cash_delta': net_delta, 'max_plan_net_cash_delta': max_plan_delta,
                        'cash_tolerance': EPSILON},
             'summary': full['summary'], 'config_sha256': full['config_sha256']}
    return scope, full


def synthetic(engine, rows, *, start=1704067200000, candidates=(0,), **kwargs):
    bars = [dict(zip(('open', 'high', 'low', 'close'), row),
                 time=start+i*900000, time_close=start+(i+1)*900000) for i, row in enumerate(rows)]
    def provider(i, data, features):
        return [{'family': 'S1' if i == 0 else 'S2', 'side': 1, 'index': i,
                 'confirmation_time': data[i]['time_close'], 'reference': 100.,
                 'initial_stop': 99., 'status': 'CONFIRMED', 'raw_setup': True, 'reasons': []}] if i in candidates else []
    return engine.run_backtest(bars, engine.BacktestConfig(max_entry_deviation_atr=100, **kwargs),
                               candidate_provider=provider)


def fee_cases(engine):
    rows = [(100, 100, 100, 100), (100, 100.5, 99.5, 100.2)]
    open_result = synthetic(engine, rows)
    partial = synthetic(engine, [rows[0], (100, 101.2, 99.5, 100.5)])
    pending = synthetic(engine, rows[:1])
    # Signal at 23:30; fill/entry fee at 23:45; TP exits after midnight.
    start = 1704067200000 + 23*3600000 + 30*60000
    cross = synthetic(engine, rows + [(100, 103.5, 99.5, 103)], start=start)
    # A close-time exit crosses midnight. The entry fee belongs to the prior day.
    guarded = synthetic(engine, rows + [(100, 100.5, 99.5, 100.2)], start=start,
                        candidates=(0, 2), max_hold_bars=1, daily_loss_limit_cash=.8)
    def compact(result):
        p = result['plans'][0]
        return {'status': p['status'], 'net_cash_closed_parts': p['net_cash'],
                'cash_paid_net': p.get('cash_paid_net'), 'entry_fee_paid': p['entry_fee_paid'],
                'unallocated_entry_fee': p['unallocated_entry_fee'], 'fees_paid': p.get('fees_paid'),
                'summary_all_realized_net_cash': result['summary']['all_realized_net_cash'],
                'cash_paid_drawdown': result['summary'].get('cash_paid_drawdown'),
                'daily_cash_UTC': result['summary'].get('daily_cash_UTC'),
                'cash_events': result.get('equity_cash_paid', [])}
    guard_decision = next(d for d in guarded['decisions'] if d['index'] == 2)
    return {'all_cases_explicitly_synthetic': True, 'open': compact(open_result),
            'partial': compact(partial), 'pending': compact(pending), 'cross_day': compact(cross),
            'cross_day_guard': {'status': guard_decision['status'], 'reasons': guard_decision['reasons'],
                                'daily_cash_UTC': guarded['summary'].get('daily_cash_UTC')}}


def fractional_csv_case(loader):
    observed = {}
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / 'fractional.csv'
        for fraction in ('.5', '.00001'):
            path.write_text('time,time_close,open,high,low,close\n'
                            f'1704067200000{fraction},1704068100000{fraction},100,101,99,100\n', encoding='utf-8')
            try:
                loader.load_bars(path)
            except ValueError:
                observed[fraction] = 'REJECTED'
            else:
                observed[fraction] = 'ACCEPTED'
    return observed


def archived_fee_impact():
    rows = []
    files = sorted((ARCHIVE / 'candidate_journals').glob('*.json.gz'))
    for path in files:
        journal = json.loads(gzip.decompress(path.read_bytes()))
        unpaid_in_closed_ledger = sum(p['unallocated_entry_fee'] for p in journal['plans'])
        if abs(unpaid_in_closed_ledger) > EPSILON:
            rows.append({'journal': path.relative_to(ROOT).as_posix(), 'sha256': sha(path),
                         'entry_fee_missing_from_old_all_realized_cash': unpaid_in_closed_ledger,
                         'open_plans': journal['summary']['open_plans']})
    return {'archived_journals_inspected': len(files), 'affected_at_cutoff': rows,
            'completed_plan_net_cash_unchanged_by_fee_allocation': True}


def review():
    from research.backtesting import engine as current
    from research.data import loader as current_loader
    before = {name: sha(ROOT/name) for name in SOURCES}
    require(sha(DATA) == DATA_SHA, 'Actual data hash changed')
    old_freeze_path = ARCHIVE / 'candidate_source_freeze.json'
    old_freeze = json.loads(old_freeze_path.read_text(encoding='utf-8'))
    old_hashes = {k.replace('\\', '/'): v for k, v in old_freeze['source_sha256'].items()}
    require(sha(ARCHIVE/'engine_before.txt') == ORIGINAL_ENGINE_SHA == old_hashes[SOURCES[0]], 'Original engine archive changed')
    require(sha(ARCHIVE/'loader_before.txt') == old_hashes[SOURCES[1]], 'Original loader archive changed')
    for name in SOURCES[2:]:
        require(before[name] == old_hashes[name], f'Unchanged candidate dependency changed: {name}')
    old_loader = load_module('qirvanta_review_loader_original', ARCHIVE/'loader_before.txt')
    old_features = load_module('qirvanta_review_features_original', ROOT/'research/features/causal.py')
    old_features.validate_bars = old_loader.validate_bars
    old = load_module('qirvanta_review_engine_original', ARCHIVE/'engine_before.txt')
    old.validate_bars, old.load_bars, old.build_features = old_loader.validate_bars, old_loader.load_bars, old_features.build_features
    bars = current_loader.load_bars(DATA)
    require(old_loader.load_bars(DATA) == bars, 'Corrected parser changed actual integer-bar dataset')
    original_scope, original_run = actual_replays(old, bars)
    corrected_scope, corrected_run = actual_replays(current, bars)
    old_fees, new_fees = fee_cases(old), fee_cases(current)
    old_fractional, new_fractional = fractional_csv_case(old_loader), fractional_csv_case(current_loader)
    daily = new_fees['cross_day']['daily_cash_UTC'] or {}
    first_day = str((1704067200000 + 23*3600000 + 45*60000)//86400000)
    next_day = str(int(first_day)+1)
    fee_checks = {
        'open_entry_fee_debited': isclose(new_fees['open']['summary_all_realized_net_cash'], -.525, abs_tol=EPSILON),
        'open_paid_drawdown_keeps_fee': isclose(new_fees['open']['cash_paid_drawdown'] or 0, .525, abs_tol=EPSILON),
        'partial_remaining_entry_fee_included': isclose(new_fees['partial']['summary_all_realized_net_cash'], .298, abs_tol=EPSILON),
        'pending_has_no_paid_fee': new_fees['pending']['summary_all_realized_net_cash'] == 0 and new_fees['pending']['entry_fee_paid'] == 0,
        'cross_day_entry_debit_correct': isclose(daily.get(first_day, 0), -.525, abs_tol=EPSILON),
        'cross_day_exit_gross_minus_exit_fee': isclose(daily.get(next_day, 0), 5.469, abs_tol=EPSILON),
        'completed_plan_paid_equals_allocated': isclose(new_fees['cross_day']['cash_paid_net'] or 0, new_fees['cross_day']['net_cash_closed_parts'], abs_tol=EPSILON),
        'opt_in_daily_guard_uses_paid_day': old_fees['cross_day_guard']['status'] == 'REJECTED' and new_fees['cross_day_guard']['status'] == 'SUBMITTED',
        'fractional_csv_rejected_before_float_precision_loss': all(x == 'REJECTED' for x in new_fractional.values()),
    }
    paired = {'decisions_identical_default_daily_guard_off': original_run['decisions'] == corrected_run['decisions'],
              'events_identical': original_run['events'] == corrected_run['events'],
              'all_original_plan_and_exit_fields_identical': original_projection(original_run['plans'], corrected_run['plans']),
              'closed_part_equity_identical': original_run['equity_closed_cash'] == corrected_run['equity_closed_cash'],
              'completed_net_cash_identical': original_run['summary']['net_cash'] == corrected_run['summary']['net_cash']}
    impacts = archived_fee_impact()
    after = {name: sha(ROOT/name) for name in SOURCES}
    require(after == before, 'Economic sources changed during independent replay')
    require(sha(DATA) == DATA_SHA, 'Data changed during independent replay')
    passed = original_scope['status'] == corrected_scope['status'] == 'PASS' and all(fee_checks.values()) and all(paired.values())
    return {'schema': 'qirvanta.independent_engine_review.v1', 'status': 'PASS' if passed else 'FAIL',
            'recorded_utc': datetime.now(timezone.utc).isoformat(),
            'scope': 'DETERMINISTIC_REPLAY_REVIEW_NOT_UNITTEST_COUNT_OR_NATIVE_PARITY',
            'random_seed': None, 'determinism': 'No random sampling; fixed prefixes, mirror transform and synthetic fixtures',
            'review_tool': {'path': Path(__file__).relative_to(ROOT).as_posix(), 'sha256': sha(Path(__file__))},
            'dataset': {'path': DATA.relative_to(ROOT).as_posix(), 'sha256': DATA_SHA, 'bars': len(bars),
                        'first_open_ms': bars[0]['time'], 'last_close_ms': bars[-1]['time_close'],
                        'status': 'ACTUAL_ALREADY_OBSERVED_ARCHIVE_NOT_UNTOUCHED_HOLDOUT'},
            'original_freeze': {'path': old_freeze_path.relative_to(ROOT).as_posix(), 'sha256': sha(old_freeze_path),
                                'engine_sha256': ORIGINAL_ENGINE_SHA, 'source_sha256': {k: old_hashes[k] for k in SOURCES}},
            'corrected_source_sha256': before, 'sources_stable_through_replay': before == after,
            'original_replay': original_scope, 'corrected_replay': corrected_scope,
            'original_fee_observations': old_fees, 'corrected_fee_observations': new_fees,
            'fractional_csv': {'original': old_fractional, 'corrected': new_fractional},
            'correction_checks': fee_checks, 'paired_original_vs_corrected': paired,
            'original_journal_impact': impacts,
            'findings': [{'id': 'ENTRY_FEE_TIMING', 'original_severity': 'MATERIAL_ACCOUNTING',
                          'status': 'RESOLVED' if all(v for k, v in fee_checks.items() if not k.startswith('fractional')) else 'UNRESOLVED',
                          'detail': 'Original cash/daily ledger deferred paid entry fee until exits; corrected paid ledger debits entry and exit fees on their respective observed execution days.'},
                         {'id': 'FRACTIONAL_TIMESTAMP_TRUNCATION', 'status': 'RESOLVED' if fee_checks['fractional_csv_rejected_before_float_precision_loss'] else 'UNRESOLVED',
                          'detail': 'Original CSV conversion could silently truncate/round fractional milliseconds; correction must reject both ordinary and sub-float-resolution fractions.'}],
            'limitations': ['Replay checks are not additional unittest methods.',
                            'No Pine/broker execution equivalence or future profitability established.',
                            'Mirror/prefix checks cover fixed defaults with S1-S9 combined; they are not an exhaustive configuration proof.',
                            'All archived prices were already observed; mirror prices and fee fixtures are synthetic and explicitly labeled.',
                            'Paid cash and closed-part cash exclude unrealized mark-to-market; intrabar exit timestamps remain OHLC approximations.',
                            'The opt-in daily guard intentionally changes admission in the cross-day synthetic fixture; original trial defaults have this guard disabled.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='New report path; omitted means stdout summary only')
    args = parser.parse_args()
    if args.output is not None and args.output.exists():
        raise ValueError('Refusing to overwrite an independent review report')
    report = review()
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+'\n')
    print(json.dumps({key: report[key] for key in ('status', 'corrected_source_sha256', 'correction_checks', 'paired_original_vs_corrected')}, indent=2))
    return report['status'] != 'PASS'


if __name__ == '__main__':
    raise SystemExit(main())
