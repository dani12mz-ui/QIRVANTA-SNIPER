"""Compare two real TradingView observations and emit trade-only public evidence."""

import argparse
import json
from decimal import Decimal
from pathlib import Path

from diagnostics.accounting.audit import audit, json_default, read_jsonl
from diagnostics.protocol.extract_bars import canonical_csv, parse_export
from research.data.extract_platform import inspect_platform
from research.validation.statistics import block_bootstrap, quantile, wilson
from unified.tools.build_ultimate import ROOT, OUTPUT, assemble, champion_bytes, observation, sha


def without_run_id(value):
    if isinstance(value, dict):
        return {key: without_run_id(item) for key, item in value.items() if key != 'run_id'}
    if isinstance(value, list):
        return [without_run_id(item) for item in value]
    return value


def underwater_duration(curve, first_entry, cutoff):
    peak_at = first_entry
    longest = 0
    underwater = False
    for point in curve:
        time = point['time']
        current = point['drawdown_cash'] > 0
        if underwater or current:
            longest = max(longest, time - peak_at)
        if not current:
            peak_at = time
        underwater = current
    if underwater:
        longest = max(longest, cutoff - peak_at)
    return {'longest_ms': longest, 'right_censored': underwater,
            'scope': 'Campaign-close cash peak to recovery or cutoff; excludes intrabar equity.'}


def observe(path, variant, source_sha):
    raw = path.read_bytes()
    export = parse_export(raw)
    coverage = inspect_platform(export, expected_timeframe='15', expected_source_sha256=source_sha)
    if export['run']['run_id'] != f'ultimate_f1_{variant}|Всички':
        raise ValueError('Wrong or stale variant export')
    records, _ = read_jsonl(path, True, 'REAL_TRADINGVIEW')
    result = audit(records)
    if result['status'] != 'PASS_OBSERVED_JOURNAL' or not result['cash_reconciliation']['matches']:
        raise ValueError('Incomplete native accounting')
    campaigns = sorted(result['campaigns'], key=lambda row: (row['last_exit_time'], row['entry_id']))
    net = [float(row['realized_net_cash']) for row in campaigns]
    net_r = [float(row['net_r']) for row in campaigns if row['net_r'] is not None]
    metrics = result['complete_campaign_metrics']
    metric_extra = {
        'win_rate_wilson95': wilson(metrics['wins'], metrics['count']),
        'net_r_descriptive_interval': block_bootstrap(net_r),
        'campaign_cash_p05': quantile(net, .05),
        'cash_drawdown_duration': underwater_duration(result['complete_campaign_close_drawdown']['curve'],
                                                       min((c['entry_time'] for c in campaigns), default=export['run']['cutoff']),
                                                       export['run']['cutoff']),
        'tp_campaign_counts': {name: sum(any(x['exit_comment'] == name for x in c['exit_rows']) for c in campaigns)
                               for name in ('TP1', 'TP2', 'TP3')},
    }
    sparse = []
    for row in campaigns:
        sparse.append({key: row[key] for key in ('entry_id', 'entry_time', 'family', 'side', 'state',
            'last_exit_time', 'closed_record_count', 'realized_net_cash', 'realized_commission_cash', 'net_r')})
        sparse[-1]['partial_exits'] = [{key: part[key] for key in ('exit_time', 'exit_comment', 'size', 'profit', 'commission')}
                                      for part in row['exit_rows']]
    public = {'log_sha256': sha(raw), 'audit_status': result['status'], 'bar_count': coverage['bar_count'],
              'first_open_utc': coverage['first_open_utc'], 'last_close_utc': coverage['last_close_utc'],
              'bar_gap_count': coverage['gap_count'], 'campaign_metrics': {**metrics, **metric_extra},
              'native_parts': result['observed_closed_row_metrics']['count'],
              'native_equity_max_drawdown': result['native_equity_max_drawdown']['value'],
              'cash_reconciliation': result['cash_reconciliation'], 'family': result['complete_campaign_groups']['family'],
              'campaigns': sparse}
    return export, result, public


def compare(off_path, on_path):
    registration_path = ROOT / 'docs/ultimate_evidence/native_f1_registration.json'
    registration_raw = registration_path.read_bytes()
    registration = json.loads(registration_raw)
    source = OUTPUT.read_bytes()
    if source != assemble(champion_bytes()) or sha(source) != registration['experimental_sha256']:
        raise ValueError('Registered strategy differs from build')
    if sha(observation(source)) != registration['observer_sha256']:
        raise ValueError('Registered observer differs from build')
    off_export, off, off_public = observe(off_path, 'off', sha(source))
    on_export, on, on_public = observe(on_path, 'on', sha(source))
    off_bars = canonical_csv(off_export['rows'])
    on_bars = canonical_csv(on_export['rows'])
    if off_bars != on_bars:
        raise ValueError('A/B bars or HTF inputs differ')
    baseline = json.loads((ROOT / 'docs/platform_evidence/ui_parity.audit.json').read_bytes())
    off_serialized = json.loads(json.dumps(off, default=json_default, allow_nan=False))
    baseline_report = json.loads((ROOT / 'docs/platform_evidence/ui_native_parity.json').read_bytes())
    if sha(off_bars) != baseline_report['reobserved_dataset_csv_sha256']:
        raise ValueError('Original parity dataset differs')
    if without_run_id(off_serialized['campaigns']) != without_run_id(baseline['campaigns']):
        raise ValueError('F1 OFF differs from archived native campaigns')
    if off_serialized['native_equity_max_drawdown']['value'] != baseline['native_equity_max_drawdown']['value']:
        raise ValueError('F1 OFF native drawdown differs')
    off_ids = {row['entry_id'] for row in off['campaigns']}
    on_ids = {row['entry_id'] for row in on['campaigns']}
    bars_at_close = {row[1]: row for row in off_export['rows']}
    vetoed = []
    for campaign in off['campaigns']:
        if campaign['family'] != 2:
            continue
        bar = bars_at_close[campaign['submitted_metadata']['submitted_time']]
        op, high, low, close = bar[2:6]
        if high <= low or campaign['side'] * (close - op) < Decimal('.55') * (high - low):
            vetoed.append(campaign['entry_id'])
    return {'schema': 'qirvanta.ultimate.native.compare.v1', 'registration_sha256': sha(registration_raw),
            'experimental_sha256': sha(source), 'observer_sha256': registration['observer_sha256'],
            'canonical_bars_sha256': sha(off_bars), 'identical_bars_off_on_archived': True,
            'off_campaign_objects_equal_to_archived_except_run_id': True,
            'off_native_drawdown_equal_to_archived': True, 'costs': {'commission_per_unit_per_fill': .175, 'slippage_ticks': 2},
            'coverage': {'off': off_public, 'on': on_public},
            'admission_identity': {'shared': len(off_ids & on_ids), 'off_only_ids': sorted(off_ids - on_ids),
                'on_only_ids': sorted(on_ids - off_ids), 'off_fast_ids_failing_f1_geometry': sorted(vetoed),
                'scope': 'Identity overlap and original geometry. Later admissions can change through occupancy.'},
            'net_usd_change': on['cash_reconciliation']['native_netprofit'] - off['cash_reconciliation']['native_netprofit'],
            'limitations': ['Observed historical window', 'Native emulator execution', 'No prospective holdout']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('off', type=Path)
    parser.add_argument('on', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.off, args.on)
    with args.out.open('x', encoding='utf-8', newline='\n') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, default=json_default, allow_nan=False)
        stream.write('\n')
    print(f"Matched native A/B. Net USD change: {result['net_usd_change']}")


if __name__ == '__main__':
    main()
