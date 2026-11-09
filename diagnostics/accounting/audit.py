"""Audit exported native rows, never reconstruct trades from aggregate statistics.

All calculations use Decimal internally. This does not execute Pine, simulate an
OHLC path, infer missing fills, or mutate any strategy/source/baseline file.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import re

SCHEMA = 'qirvanta.native.v1'
ZERO = Decimal(0)
DEFAULT_TOLERANCE = Decimal('0.00000001')


class AuditError(ValueError):
    """Malformed, contradictory or unsupported input; no audited result exists."""


def number(value, name, *, positive=False, nonnegative=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise AuditError(f'{name}: expected finite number')
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise AuditError(f'{name}: invalid number') from exc
    if not result.is_finite() or positive and result <= 0 or nonnegative and result < 0:
        raise AuditError(f'{name}: invalid numeric range')
    return result


def integer(value, name, minimum=0):
    result = number(value, name)
    if result != result.to_integral_value() or result < minimum:
        raise AuditError(f'{name}: expected integer >= {minimum}')
    return int(result)


def text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise AuditError(f'{name}: expected nonempty string')
    return value


def required(record, names):
    missing = set(names.split()) - record.keys()
    if missing:
        raise AuditError(f'{record.get("kind", "record")}: missing {sorted(missing)}')


def close(a, b, tolerance=DEFAULT_TOLERANCE):
    return abs(a - b) <= tolerance


def json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(type(value).__name__)


def read_jsonl(path, allow_pine_prefix=False, origin_override=None):
    """Read pure NDJSON, or explicitly permit timestamp-prefixed Pine log lines.

    Empty lines are ignored. Nonempty noise is always rejected. The optional
    prefix is retained in ingestion metadata rather than silently discarded.
    A JSON array is accepted as an explicitly batched list of complete records.
    """
    raw = Path(path).read_bytes()
    decoded = raw.decode('utf-8-sig')
    records, prefixes, markers = [], [], []
    bar_mode, bar_seen, bar_done = False, False, False
    bar_lines, bar_metadata, bar_count, warning_probes = [], None, 0, []
    csv_rows = None
    if decoded.startswith('Date,Message'):
        reader = csv.DictReader(io.StringIO(decoded, newline=''), strict=True)
        try:
            csv_rows = list(reader)
        except csv.Error as exc:
            raise AuditError(f'invalid Pine Logs CSV: {exc}') from exc
        if reader.fieldnames != ['Date', 'Message'] or any(set(x) != {'Date', 'Message'} or x['Message'] is None for x in csv_rows):
            raise AuditError('expected supported Pine Logs Date,Message CSV')
        decoded = '\n'.join(row['Message'] for row in csv_rows)
    for lineno, line in enumerate(decoded.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if line == 'QIRVANTA_BARS_BEGIN':
            if bar_seen:
                raise AuditError('duplicate observed-bars section')
            bar_mode = bar_seen = True
            continue
        if line == 'QIRVANTA_BARS_END':
            if not bar_mode:
                raise AuditError('observed-bars END without BEGIN')
            bar_mode = False; bar_done = True
            continue
        if bar_mode:
            try:
                value = json.loads(line, parse_float=Decimal)
            except json.JSONDecodeError as exc:
                raise AuditError('invalid JSON in explicitly separate observed-bars section') from exc
            if bar_metadata is None:
                if not isinstance(value, dict) or not isinstance(value.get('columns'), list):
                    raise AuditError('observed-bars section missing columns metadata')
                bar_metadata = value
            elif not isinstance(value, list) or len(value) != len(bar_metadata['columns']):
                raise AuditError('observed-bars row does not match column count')
            else:
                for item in value:
                    if item is not None: number(item, 'observed-bars value')
                bar_count += 1
            bar_lines.append(line)
            continue
        if line.startswith('QIRVANTA_WARNING_PROBE '):
            try:
                probe = json.loads(line[len('QIRVANTA_WARNING_PROBE '):])
            except json.JSONDecodeError as exc:
                raise AuditError('invalid warning-probe JSON') from exc
            if probe.get('schema') != 'qirvanta.warnings.v1' or probe.get('kind') != 'history_scope':
                raise AuditError('unrecognized warning-probe schema/kind')
            required(probe, 'baseline_sha256 symbol timeframe cutoff confirmed_bar_count fast_long_calls fast_short_calls reversal_long_calls reversal_short_calls fast_long_mismatches fast_short_mismatches reversal_long_mismatches reversal_short_mismatches netprofit closed_count')
            for name in ('cutoff', 'confirmed_bar_count', 'fast_long_calls', 'fast_short_calls',
                         'reversal_long_calls', 'reversal_short_calls', 'fast_long_mismatches',
                         'fast_short_mismatches', 'reversal_long_mismatches', 'reversal_short_mismatches', 'closed_count'):
                integer(probe[name], name)
            number(probe['netprofit'], 'warning netprofit')
            warning_probes.append(probe)
            continue
        if line in ('QIRVANTA_DATA_BEGIN', 'QIRVANTA_DATA_END'):
            markers.append(line)
            continue
        if csv_rows is not None and (not markers or markers[-1] == 'QIRVANTA_DATA_END'):
            raise AuditError('CSV data outside BEGIN/END boundaries')
        if allow_pine_prefix and not line.startswith(('{', '[{')):
            found = re.match(r'^(\[[^\]\r\n]+\]\s*:?\s*)([\[{].*)$', line)
            if found:
                prefixes.append({'line': lineno, 'prefix': found[1]})
                line = found[2]
        try:
            value = json.loads(line, parse_float=Decimal,
                               parse_constant=lambda x: (_ for _ in ()).throw(AuditError(f'nonfinite JSON: {x}')))
        except (json.JSONDecodeError, AuditError) as exc:
            raise AuditError(f'line {lineno}: {exc}') from exc
        batch = value if isinstance(value, list) else [value]
        if not batch or not all(isinstance(x, dict) for x in batch):
            raise AuditError(f'line {lineno}: expected object or nonempty object batch')
        records.extend(batch)
    if (csv_rows is not None or markers) and markers != ['QIRVANTA_DATA_BEGIN', 'QIRVANTA_DATA_END']:
        raise AuditError('Pine CSV requires exactly one BEGIN/END marker pair')
    if bar_mode or bar_seen and (not bar_done or bar_metadata is None):
        raise AuditError('incomplete observed-bars section')
    supplements = []
    if origin_override is not None:
        for record in records:
            if record.get('kind') == 'run' and 'origin' not in record:
                record['origin'] = origin_override
                supplements.append({'field': 'origin', 'value': origin_override, 'source': 'explicit CLI declaration; absent in raw export'})
    return records, {'input_sha256': hashlib.sha256(raw).hexdigest(),
                     'format': 'PINE_LOGS_CSV' if csv_rows is not None else 'NDJSON',
                     'csv_log_rows': len(csv_rows) if csv_rows is not None else None,
                     'records_read': len(records), 'removed_log_prefixes': prefixes,
                     'markers': markers, 'header_supplements': supplements,
                     'separate_observed_bars': {'rows': bar_count, 'metadata': bar_metadata,
                        'canonical_section_sha256': hashlib.sha256(('\n'.join(bar_lines)+'\n').encode()).hexdigest(),
                        'scope': 'EXPLICITLY_SEPARATE_CONTEXT_SECTION_NOT_USED_TO_INFER_FILLS'} if bar_seen else None,
                     'separate_warning_probes': warning_probes}


def _unique(store, key, value, what):
    if key in store:
        if store[key] != value:
            raise AuditError(f'conflicting duplicate {what}: {key}')
        return False
    store[key] = value
    return True


def _entry_key(row):
    return row['entry_id'], row['entry_time'], row['entry_bar_index']


def _entry(row):
    required(row, 'entry_id entry_time entry_bar_index size entry_price')
    row = dict(row)
    row['entry_id'] = text(row['entry_id'], 'entry_id')
    for name in ('entry_time', 'entry_bar_index'):
        row[name] = integer(row[name], name)
    row['size'] = number(row['size'], 'size')
    if row['size'] == 0:
        raise AuditError('size must be signed and nonzero')
    row['entry_price'] = number(row['entry_price'], 'entry_price')
    return row


def normalize(records):
    runs, plans, closed, snapshots = {}, {}, {}, {}
    duplicates = Counter()
    run_ids = set()
    for raw in records:
        required(raw, 'kind run_id')
        run_ids.add(text(raw['run_id'], 'run_id'))
        if raw.get('schema', SCHEMA) != SCHEMA:
            raise AuditError('unsupported schema')
        row = dict(raw)
        kind = row['kind']
        if kind == 'run':
            required(row, 'schema symbol timeframe source_sha256 account_currency symbol_currency pointvalue origin')
            if not re.fullmatch('[0-9a-fA-F]{64}', text(row['source_sha256'], 'source_sha256')):
                raise AuditError('source_sha256 must be 64 hexadecimal characters')
            for name in ('symbol', 'timeframe', 'account_currency', 'symbol_currency'):
                text(row[name], name)
            if row['origin'] not in ('REAL_TRADINGVIEW', 'SYNTHETIC'):
                raise AuditError('origin must explicitly be REAL_TRADINGVIEW or SYNTHETIC')
            if row['account_currency'] != row['symbol_currency']:
                raise AuditError('FX conversion unsupported: account and symbol currency must match')
            row['pointvalue'] = number(row['pointvalue'], 'pointvalue', positive=True)
            if 'mintick' in row:
                row['mintick'] = number(row['mintick'], 'mintick', positive=True)
            for key, default in (('quantity_tolerance', DEFAULT_TOLERANCE),
                                 ('cash_tolerance', Decimal('0.00001')),
                                 ('price_tolerance', DEFAULT_TOLERANCE)):
                row[key] = number(row.get(key, default), key, positive=True)
            row['profit_semantics'] = row.get('profit_semantics', 'UNVERIFIED')
            if row['profit_semantics'] not in ('UNVERIFIED', 'NET_INCLUDES_COMMISSION', 'GROSS_EXCLUDES_COMMISSION'):
                raise AuditError('unrecognized profit_semantics')
            added = _unique(runs, row['run_id'], row, 'run')
        elif kind == 'submitted':
            required(row, 'plan_id entry_id submitted_time submitted_bar quantity family side htf_bias regime reference initial_sl')
            if text(row['plan_id'], 'plan_id') != text(row['entry_id'], 'entry_id'):
                raise AuditError('QIRVANTA submitted entry_id must equal plan_id')
            row['quantity'] = number(row['quantity'], 'quantity', positive=True)
            for name in ('submitted_time', 'submitted_bar'):
                row[name] = integer(row[name], name)
            row['family'] = integer(row['family'], 'family', 1)
            if row['family'] not in (1, 2, 3) or row['side'] not in (-1, 1) or isinstance(row['side'], bool):
                raise AuditError('invalid QIRVANTA family or side')
            if row['htf_bias'] is not None and (row['htf_bias'] not in (-1, 0, 1) or isinstance(row['htf_bias'], bool)):
                raise AuditError('htf_bias must be -1,0,1 or explicit null')
            text(row['regime'], 'regime')
            for name in ('reference', 'initial_sl'):
                row[name] = number(row[name], name)
            added = _unique(plans, row['entry_id'], row, 'submission')
        elif kind == 'closed':
            required(row, 'trade_index exit_id exit_comment exit_time exit_bar_index exit_price profit commission max_drawdown max_runup')
            row = _entry(row)
            for name in ('trade_index', 'exit_time', 'exit_bar_index'):
                row[name] = integer(row[name], name)
            if row['entry_time'] > row['exit_time'] or row['entry_bar_index'] > row['exit_bar_index']:
                raise AuditError('exit precedes entry')
            row['exit_id'] = text(row['exit_id'], 'exit_id')
            if row['exit_comment'] is not None and not isinstance(row['exit_comment'], str):
                raise AuditError('exit_comment must be string or null')
            for name in ('exit_price', 'profit'):
                row[name] = number(row[name], name)
            for name in ('commission', 'max_drawdown', 'max_runup'):
                row[name] = number(row[name], name, nonnegative=True)
            added = _unique(closed, row['trade_index'], row, 'closed trade index')
        elif kind == 'snapshot':
            required(row, 'at_time at_bar closed_count first_closed_index open_complete open netprofit max_drawdown')
            for name in ('at_time', 'at_bar', 'closed_count', 'first_closed_index'):
                row[name] = integer(row[name], name)
            if row['first_closed_index'] > row['closed_count']:
                raise AuditError('first_closed_index exceeds closed_count')
            if not isinstance(row['open_complete'], bool) or not isinstance(row['open'], list):
                raise AuditError('snapshot requires explicit boolean open_complete and list open')
            row['netprofit'] = number(row['netprofit'], 'netprofit')
            row['max_drawdown'] = number(row['max_drawdown'], 'max_drawdown', nonnegative=True)
            if 'submitted_count' in row:
                row['submitted_count'] = integer(row['submitted_count'], 'submitted_count')
            opens = {}
            for value in row['open']:
                value = _entry(value)
                if _entry_key(value) in opens:
                    raise AuditError('duplicate open entry instance')
                if value['entry_time'] > row['at_time'] or value['entry_bar_index'] > row['at_bar']:
                    raise AuditError('open entry is later than snapshot')
                opens[_entry_key(value)] = value
            row['open'] = list(opens.values())
            added = _unique(snapshots, (row['at_time'], row['at_bar']), row, 'snapshot')
        else:
            raise AuditError(f'unrecognized record kind: {kind}')
        if not added:
            duplicates[kind] += 1
    if len(run_ids) != 1 or len(runs) != 1:
        raise AuditError('exactly one run header and one run_id are required')
    if not snapshots:
        raise AuditError('missing final snapshot: cannot infer open/closed state from rows')
    run = next(iter(runs.values()))
    snapshot = snapshots[max(snapshots)]
    for row in closed.values():
        if row['trade_index'] >= snapshot['closed_count']:
            raise AuditError('closed row index is outside final snapshot count')
        if row['exit_time'] > snapshot['at_time'] or row['exit_bar_index'] > snapshot['at_bar']:
            raise AuditError('closed row is later than final snapshot')
    return run, plans, closed, snapshot, dict(duplicates)


def stats(values):
    """Unit is caller-defined: rows or complete campaigns, never conflated."""
    values = list(values)
    wins = [x for x in values if x > 0]
    losses = [x for x in values if x < 0]
    total = sum(values, ZERO)
    gain, loss = sum(wins, ZERO), -sum(losses, ZERO)
    return {'count': len(values), 'wins': len(wins), 'losses': len(losses),
            'breakevens': len(values) - len(wins) - len(losses),
            'net_cash': total, 'sum_positive_net_cash': gain,
            'sum_negative_net_cash_magnitude': loss,
            'profit_factor': gain / loss if loss else None,
            'profit_factor_status': 'DEFINED' if loss else 'NO_LOSSES' if wins else 'NO_REALIZED_OUTCOME',
            'win_rate': Decimal(len(wins)) / len(values) if values else None,
            'average_net_cash': total / len(values) if values else None,
            'average_win_cash': gain / len(wins) if wins else None,
            'average_loss_cash': -loss / len(losses) if losses else None}


def closed_drawdown(events):
    """Cash recognized at exported exits; excludes unrealized/intrabar path."""
    cash = peak = maximum = ZERO
    curve = []
    for time, order, amount in sorted(events):
        cash += amount
        peak = max(peak, cash)
        maximum = max(maximum, peak - cash)
        curve.append({'time': time, 'order': order, 'cumulative_net_cash': cash,
                      'drawdown_cash': peak - cash})
    return {'max_drawdown_cash': maximum, 'curve': curve,
            'scope': 'REALIZED_EXPORTED_EXIT_EVENTS_ONLY_NOT_NATIVE_INTRABAR_EQUITY_DRAWDOWN'}


def campaign_metrics(campaigns):
    """Describe an actual completed-entry subset, not a new execution portfolio."""
    campaigns = list(campaigns)
    result = stats(c['realized_net_cash'] for c in campaigns)
    rows = [row for campaign in campaigns for row in campaign['exit_rows']]
    result['closed_cash_event_drawdown'] = closed_drawdown((x['exit_time'], x['trade_index'], x['net_cash']) for x in rows)['max_drawdown_cash']
    result['campaign_close_drawdown'] = closed_drawdown((c['last_exit_time'], i, c['realized_net_cash']) for i, c in enumerate(campaigns))['max_drawdown_cash']
    result['drawdown_scope'] = 'REALIZED_COMPLETE_CAMPAIGN_ATTRIBUTION_SUBSEQUENCE_NOT_STANDALONE_OR_NATIVE_EQUITY_DRAWDOWN'
    label = lambda row: row['exit_comment'] if row['exit_comment'] is not None else '<NO_COMMENT>'
    result['exit_comment_record_counts'] = dict(Counter(label(row) for row in rows))
    quantities = defaultdict(lambda: ZERO)
    for row in rows:
        quantities[label(row)] += abs(row['size'])
    result['exit_comment_quantities'] = dict(quantities)
    result['final_exit_comment_campaign_counts'] = dict(Counter(label(c['exit_rows'][-1]) for c in campaigns))
    return result


def audit(records):
    run, plans, closed, snapshot, duplicates = normalize(records)
    qt, ct, pt = (run[x] for x in ('quantity_tolerance', 'cash_tolerance', 'price_tolerance'))
    issues = []
    missing_indices = sorted(set(range(snapshot['closed_count'])) - closed.keys())
    if missing_indices:
        issues.append({'code': 'MISSING_CLOSED_ROWS', 'indices': missing_indices})
    if snapshot['first_closed_index'] > 0:
        issues.append({'code': 'PLATFORM_TRIMMED_HISTORY', 'first_index': snapshot['first_closed_index']})
    if not snapshot['open_complete']:
        issues.append({'code': 'OPEN_SNAPSHOT_INCOMPLETE'})
    if snapshot.get('submitted_count', len(plans)) != len(plans):
        issues.append({'code': 'SUBMITTED_COUNT_MISMATCH', 'expected': snapshot['submitted_count'], 'observed': len(plans)})
    groups = defaultdict(list)
    raw_profit_classes = Counter()
    row_values = []
    for row in sorted(closed.values(), key=lambda x: x['trade_index']):
        row = dict(row)
        gross = (row['exit_price'] - row['entry_price']) * row['size'] * run['pointvalue']
        row['gross_cash_from_prices'] = gross
        row['net_cash'] = gross - row['commission']
        is_net, is_gross = close(row['profit'], row['net_cash'], ct), close(row['profit'], gross, ct)
        classification = 'BOTH_ZERO_FEE_OR_WITHIN_TOLERANCE' if is_net and is_gross else 'NET' if is_net else 'GROSS' if is_gross else 'NEITHER'
        row['api_profit_classification'] = classification
        raw_profit_classes[classification] += 1
        expected = run['profit_semantics']
        if classification == 'NEITHER' or expected == 'NET_INCLUDES_COMMISSION' and not is_net or expected == 'GROSS_EXCLUDES_COMMISSION' and not is_gross:
            issues.append({'code': 'API_PROFIT_RECONCILIATION_FAILED', 'trade_index': row['trade_index'], 'classification': classification})
        groups[_entry_key(row)].append(row)
        row_values.append(row)
    if raw_profit_classes['NET'] and raw_profit_classes['GROSS']:
        issues.append({'code': 'MIXED_API_PROFIT_SEMANTICS'})
    opens = {_entry_key(x): x for x in snapshot['open']}
    all_keys = groups.keys() | opens.keys()
    instances = Counter(key[0] for key in all_keys)
    campaigns = []
    for key in sorted(all_keys, key=lambda k: (k[1], k[2], k[0])):
        entry_id, entry_time, entry_bar = key
        rows = sorted(groups[key], key=lambda x: (x['exit_time'], x['trade_index']))
        opened = opens.get(key)
        plan = plans.get(entry_id)
        observed = rows + ([opened] if opened else [])
        side = 1 if observed[0]['size'] > 0 else -1
        if any((1 if x['size'] > 0 else -1) != side for x in observed):
            raise AuditError(f'opposite sides under one entry instance: {entry_id}')
        if plan and (plan['side'] != side or entry_time < plan['submitted_time'] or entry_bar < plan['submitted_bar']):
            raise AuditError(f'entry side/time contradicts submitted plan: {entry_id}')
        prices_agree = all(close(x['entry_price'], observed[0]['entry_price'], pt) for x in observed)
        closed_qty = sum((abs(x['size']) for x in rows), ZERO)
        open_qty = abs(opened['size']) if opened else ZERO
        total = plan['quantity'] if plan else None
        if total is not None and closed_qty + open_qty > total + qt:
            raise AuditError(f'closed plus open quantity exceeds submitted quantity: {entry_id}')
        reasons = []
        if not plan:
            reasons.append('MISSING_SUBMITTED_METADATA')
        if instances[entry_id] > 1:
            reasons.append('REUSED_ENTRY_ID_WITH_MULTIPLE_ENTRY_INSTANCES')
        if not prices_agree:
            reasons.append('ENTRY_PRICE_CHANGED_WITHIN_INSTANCE')
        if not snapshot['open_complete']:
            reasons.append('OPEN_SNAPSHOT_INCOMPLETE')
        if total is not None and not close(closed_qty + open_qty, total, qt):
            reasons.append('QUANTITY_NOT_RECONCILED')
        state = 'INCOMPLETE' if reasons else 'PARTIALLY_OPEN' if open_qty and closed_qty else 'OPEN' if open_qty else 'COMPLETE'
        if reasons:
            issues.append({'code': 'CAMPAIGN_INCOMPLETE', 'entry_id': entry_id, 'entry_time': entry_time, 'reasons': reasons})
        net = sum((x['net_cash'] for x in rows), ZERO)
        commission = sum((x['commission'] for x in rows), ZERO)
        actual_entry = observed[0]['entry_price'] if prices_agree else None
        distance = (actual_entry - plan['initial_sl']) * side if plan and actual_entry is not None else None
        valid_risk = distance is not None and run.get('mintick') is not None and distance >= run['mintick']
        denominator = distance * total * run['pointvalue'] if valid_risk else None
        bias = plan['htf_bias'] if plan else None
        trend = 'UNKNOWN' if bias is None else 'NEUTRAL_HTF' if bias == 0 else 'WITH_HTF' if bias == side else 'AGAINST_HTF'
        campaigns.append({'entry_id': entry_id, 'plan_id': plan['plan_id'] if plan else None,
                          'entry_time': entry_time, 'entry_bar_index': entry_bar, 'actual_entry': actual_entry,
                          'family': plan['family'] if plan else None, 'side': side,
                          'htf_bias': bias, 'trend_relation': trend, 'regime': plan['regime'] if plan else 'UNKNOWN',
                          'submitted_metadata': plan, 'state': state, 'incomplete_reasons': reasons,
                          'submitted_quantity': total, 'closed_quantity': closed_qty, 'open_quantity': open_qty,
                          'closed_record_count': len(rows), 'closed_indices': [x['trade_index'] for x in rows],
                          'realized_gross_cash': net + commission, 'realized_commission_cash': commission,
                          'realized_net_cash': net, 'net_r': net / denominator if state == 'COMPLETE' and denominator else None,
                          'initial_risk_cash': denominator,
                          'risk_status': 'VALID_ACTUAL_INITIAL_DISTANCE' if valid_risk else 'UNDEFINED_MISSING_MINTICK' if run.get('mintick') is None else 'UNDEFINED_NONPOSITIVE_OR_SUBTICK_DISTANCE',
                          'last_exit_time': rows[-1]['exit_time'] if rows else None,
                          'holding_bars': rows[-1]['exit_bar_index'] - entry_bar + 1 if state == 'COMPLETE' else None,
                          'campaign_max_drawdown': None, 'campaign_max_runup': None,
                          'excursion_limitation': 'Per-part excursions retained below; never summed or maxed into whole-entry MAE/MFE without a position path',
                          'exit_rows': rows})
    observed_ids = {x['entry_id'] for x in campaigns}
    unfilled = [p for key, p in plans.items() if key not in observed_ids]
    complete = [c for c in campaigns if c['state'] == 'COMPLETE']
    grouped = {}
    for dimension in ('family', 'side', 'trend_relation', 'regime'):
        buckets = defaultdict(list)
        for campaign in complete:
            buckets[str(campaign[dimension])].append(campaign)
        grouped[dimension] = {key: campaign_metrics(values) for key, values in sorted(buckets.items())}
    combined = defaultdict(list)
    for c in complete:
        combined[f"M{c['family']}|{c['side']}|{c['trend_relation']}|{c['regime']}"].append(c)
    grouped['family_side_trend_regime'] = {key: campaign_metrics(values) for key, values in sorted(combined.items())}
    derived_net = sum((x['net_cash'] for x in row_values), ZERO)
    full_closed_coverage = not missing_indices and snapshot['first_closed_index'] == 0
    cash_match = close(derived_net, snapshot['netprofit'], ct) if full_closed_coverage else None
    if cash_match is False:
        issues.append({'code': 'NATIVE_NETPROFIT_MISMATCH', 'delta': derived_net - snapshot['netprofit']})
    failed = any(x['code'] in ('API_PROFIT_RECONCILIATION_FAILED', 'NATIVE_NETPROFIT_MISMATCH', 'MIXED_API_PROFIT_SEMANTICS') for x in issues)
    state_counts = Counter(c['state'] for c in campaigns)
    return {'schema': 'qirvanta.native.audit.v1', 'run': run,
            'status': 'RECONCILIATION_FAILED' if failed else 'INCOMPLETE' if issues else 'PASS_OBSERVED_JOURNAL',
            'scope': 'EXPORTED_NATIVE_JOURNAL_ACCOUNTING_NOT_MARKET_SIGNAL_OR_FORWARD_VALIDATION',
            'coverage': {'closed_count_reported': snapshot['closed_count'], 'closed_rows_observed': len(closed),
                         'first_closed_index': snapshot['first_closed_index'], 'missing_indices': missing_indices,
                         'full_closed_history': full_closed_coverage, 'open_snapshot_complete': snapshot['open_complete'],
                         'duplicates_ignored': duplicates},
            'issues': issues, 'campaign_state_counts': dict(state_counts),
            'submitted_without_recorded_fill': unfilled,
            'submitted_without_recorded_fill_qualification': 'No fill is manufactured; missing data or a rejected/unfilled order is not resolved from aggregate totals',
            'complete_campaign_metrics': campaign_metrics(complete),
            'complete_campaign_groups': grouped,
            'observed_closed_row_metrics': stats(x['net_cash'] for x in row_values),
            'cash_reconciliation': {'derived_net_cash': derived_net, 'native_netprofit': snapshot['netprofit'],
                                    'difference': derived_net - snapshot['netprofit'], 'matches': cash_match,
                                    'commission_cash': sum((x['commission'] for x in row_values), ZERO),
                                    'formula': '(exit_price-entry_price)*signed_size*pointvalue - commission, once; same currency required',
                                    'api_profit_classifications': dict(raw_profit_classes)},
            'closed_cash_drawdown': closed_drawdown((x['exit_time'], x['trade_index'], x['net_cash']) for x in row_values),
            'complete_campaign_close_drawdown': closed_drawdown((c['last_exit_time'], i, c['realized_net_cash']) for i, c in enumerate(complete)),
            'native_equity_max_drawdown': {'value': snapshot['max_drawdown'], 'source': 'snapshot.strategy.max_drawdown',
                                         'scope': 'NATIVE_EQUITY_INTRABAR_METRIC_NOT_RECONSTRUCTED_FROM_CLOSED_ROWS'},
            'campaigns': campaigns}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--allow-pine-prefix', action='store_true')
    parser.add_argument('--origin', choices=['REAL_TRADINGVIEW', 'SYNTHETIC'],
                        help='Explicitly supplement origin only if missing in raw header')
    args = parser.parse_args()
    if args.input.resolve() == args.out.resolve():
        parser.error('output must not overwrite the raw input')
    try:
        records, ingestion = read_jsonl(args.input, args.allow_pine_prefix, args.origin)
        result = audit(records)
        result['ingestion'] = ingestion
    except (AuditError, OSError, UnicodeDecodeError) as exc:
        parser.exit(2, f'Input audit failed: {exc}\n')
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=json_default, allow_nan=False) + '\n', encoding='utf-8', newline='\n')
    print(json.dumps({'status': result['status'], 'output': str(args.out),
                      'complete_campaigns': result['complete_campaign_metrics']['count'],
                      'observed_rows': result['observed_closed_row_metrics']['count']}, indent=2))
    return 0 if result['status'] == 'PASS_OBSERVED_JOURNAL' else 1


if __name__ == '__main__':
    raise SystemExit(main())
