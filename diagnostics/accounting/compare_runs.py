"""Rebuild native campaign audits/comparison from retained raw Pine Logs exports."""
from pathlib import Path
import hashlib
import json
import re
from audit import audit, read_jsonl, json_default, SCHEMA

ROOT = Path(__file__).resolve().parent
EVIDENCE = ROOT.parent / 'evidence'
RUNS = ('B-ALL', 'B-RETEST', 'B-FAST', 'B-REVERSAL')
SETTINGS = ('original_inputs.txt', 'B-RETEST.inputs.txt', 'B-FAST.inputs.txt', 'B-REVERSAL.inputs.txt')
DATA_FIELDS = ('symbol', 'timeframe', 'source_sha256', 'account_currency', 'symbol_currency',
               'pointvalue', 'mintick', 'first_bar_time', 'last_bar_open', 'cutoff', 'bar_count', 'checksum1', 'checksum2')


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=json_default,
                               allow_nan=False)+'\n', encoding='utf-8', newline='\n')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), default=json_default).encode()).hexdigest()


def settings_text(path):
    """Compare recorded complete dialog text, not underlying stale chart/log rows."""
    raw = path.read_bytes()
    value = raw.decode('utf-8').split('- dialog "QIRV ONE":', 1)[1].split('  - button "Defaults":', 1)[0]
    value = value.replace(' [active]', '')  # Focus only, not a setting.
    match = re.search(r'(  - generic: Модел за този тест\n  - combobox:\n    - generic: )([^\n]+)', value)
    if match is None:
        raise ValueError('Model selector missing from recorded Inputs dialog')
    normalized = value[:match.start(2)]+'<MODEL>'+value[match.end(2):]
    return {'evidence': '../evidence/'+path.name, 'raw_sha256': hashlib.sha256(raw).hexdigest(),
            'selected_model': match[2], 'all_other_dialog_settings_sha256': hashlib.sha256(normalized.encode()).hexdigest()}


def build():
    audits, parsed, rows, input_checks = {}, {}, [], []
    for name, settings in zip(RUNS, SETTINGS):
        records, ingestion = read_jsonl(EVIDENCE/(name+'.pine-logs.csv'), origin_override='REAL_TRADINGVIEW')
        result = audit(records); result['ingestion'] = ingestion
        write(ROOT/(name+'.audit.json'), result)
        audits[name] = result; parsed[name] = records
        s = settings_text(EVIDENCE/settings)
        s['run_id_matches_selected_model'] = result['run']['run_id'] == 'baseline|'+s['selected_model']
        input_checks.append(s)
        c, parts = result['complete_campaign_metrics'], result['observed_closed_row_metrics']
        rows.append({'run': name, 'run_id': result['run']['run_id'], 'audit_status': result['status'],
                     'complete_campaigns': c['count'], 'native_closed_rows': parts['count'],
                     'winning_campaigns': c['wins'], 'losing_campaigns': c['losses'],
                     'campaign_win_rate': c['win_rate'], 'campaign_profit_factor': c['profit_factor'],
                     'partial_row_profit_factor': parts['profit_factor'], 'net_cash': c['net_cash'],
                     'average_winning_campaign_cash': c['average_win_cash'],
                     'average_losing_campaign_cash': c['average_loss_cash'],
                     'closed_cash_event_drawdown': result['closed_cash_drawdown']['max_drawdown_cash'],
                     'campaign_close_drawdown': result['complete_campaign_close_drawdown']['max_drawdown_cash'],
                     'native_equity_drawdown': result['native_equity_max_drawdown']['value'],
                     'commission_cash': result['cash_reconciliation']['commission_cash'],
                     'native_net_difference': result['cash_reconciliation']['difference'],
                     'coverage': result['coverage'], 'data_header': {k: result['run'][k] for k in DATA_FIELDS},
                     'raw_export_sha256': ingestion['input_sha256'], 'settings_evidence': s})
    full_records, full_ingestion = read_jsonl(EVIDENCE/'B-ALL-FULL.pine-logs.csv', origin_override='REAL_TRADINGVIEW')
    full_result = audit(full_records); full_result['ingestion'] = full_ingestion
    write(ROOT/'B-ALL-FULL.audit.json', full_result)
    probes = full_ingestion['separate_warning_probes']
    probe_pass = len(probes) == 1 and all(probes[0][k] == audits['B-ALL']['run']['bar_count'] for k in
             ('confirmed_bar_count', 'fast_long_calls', 'fast_short_calls', 'reversal_long_calls', 'reversal_short_calls'))
    probe_pass = probe_pass and all(probes[0][k] == 0 for k in
             ('fast_long_mismatches', 'fast_short_mismatches', 'reversal_long_mismatches', 'reversal_short_mismatches'))
    probe_pass = probe_pass and probes[0]['baseline_sha256'] == audits['B-ALL']['run']['source_sha256']
    comparison = {'status': 'PASS' if parsed['B-ALL'] == full_records and probe_pass else 'FAIL',
                  'original_native_records': len(parsed['B-ALL']), 'instrumented_native_records': len(full_records),
                  'native_records_exactly_equal_after_same_origin_supplement': parsed['B-ALL'] == full_records,
                  'native_canonical_sha256': digest(parsed['B-ALL']), 'instrumented_native_canonical_sha256': digest(full_records),
                  'separate_bars': full_ingestion['separate_observed_bars'], 'separate_warning_probes': probes,
                  'warning_call_counts_and_history_predicates_match': probe_pass,
                  'input_sha256': {'original': audits['B-ALL']['ingestion']['input_sha256'], 'instrumented': full_ingestion['input_sha256']},
                  'scope': 'Exact exported native records + tested history reads/predicates; not full signal/realtime/rollback proof'}
    write(ROOT/'probe_journal_comparison.json', comparison)
    headers_equal = all(row['data_header'] == rows[0]['data_header'] for row in rows)
    inputs_equal = len({x['all_other_dialog_settings_sha256'] for x in input_checks}) == 1 and all(x['run_id_matches_selected_model'] for x in input_checks)
    result = {'status': 'PASS_SCOPED_COMPARISON' if headers_equal and inputs_equal and all(x['audit_status'] == 'PASS_OBSERVED_JOURNAL' for x in rows) and comparison['status'] == 'PASS' else 'FAIL',
              'scope': 'Same reported data fingerprints/source/settings except intentional model selection; separate native portfolios, not additive model results',
              'run_comparison': rows, 'shared_data_header': rows[0]['data_header'],
              'reported_data_headers_identical': headers_equal, 'other_inputs_dialog_text_identical': inputs_equal,
              'settings_normalization': 'Only selected runModel value and [active] UI focus annotation are removed; full dialog including all other values retained',
              'data_completeness_limit': 'All4 runs match bar count/range/cutoff/two integer checksums. Full serialized OHLC/HTF rows were exported for B-ALL-FULL only; collision-free byte equality of all4 original full-precision feeds is not claimed.',
              'properties_evidence': '../evidence/original_properties.txt',
              'properties_evidence_sha256': hashlib.sha256((EVIDENCE/'original_properties.txt').read_bytes()).hexdigest(),
              'cost_check': 'Every exported row reconciles fill-price gross minus native entry+exit commission once to API net; each run sums exactly to its native netprofit. Slippage is already in actual fills.',
              'whole_entry_vs_partial_rows': 'Rows are grouped by entry_id+entry_time+entry_bar_index and checked against submitted quantity and a complete final open snapshot. No division of total trades by3, no inferred fills from totals.',
              'all_portfolio_attribution': audits['B-ALL']['complete_campaign_groups'],
              'isolated_runs_not_additive': True, 'probe_comparison': 'probe_journal_comparison.json',
              'parser_sha256': hashlib.sha256((ROOT/'audit.py').read_bytes()).hexdigest(),
              'comparison_tool_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              'performance_improvement_claim': False}
    write(ROOT/'run_comparison.json', result)
    def f(x, places=3): return '—' if x is None else f'{x:.{places}f}'
    table = ['# Native отчетност на цели входове', '', 'Данните са действителни native journal exports; това не е оптимизация или OOS тест.', '',
             '| Run | Цели входове | Частични native записи | Net USD | PF цели входове | PF записи | Средна печалба | Средна загуба | Closed cash DD | Native equity DD |',
             '|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|']
    for x in rows:
        table.append(f"| {x['run']} | {x['complete_campaigns']} | {x['native_closed_rows']} | {f(x['net_cash'])} | {f(x['campaign_profit_factor'],6)} | {f(x['partial_row_profit_factor'],6)} | {f(x['average_winning_campaign_cash'])} | {f(x['average_losing_campaign_cash'])} | {f(x['closed_cash_event_drawdown'])} | {f(x['native_equity_drawdown'])} |")
    table += ['', 'За RETEST PF е неопределен: един печеливш вход, без загуба в извадката. Това не е доказателство за предимство.', '',
              'Всички cash reconciliation разлики са нула. Комисионата вече е включена в exported `profit`; цените и native commission дават същия net. Не се начислява повторно.', '',
              'Closed cash DD отчита реализираните native изходи. Native equity DD включва различна intrabar equity семантика и е запазен отделно. Per-part MAE/MFE не се сумират в предполагаемо MAE/MFE на целия вход.', '',
              'Самостоятелните runs не се сумират като резултат на общ портфейл. Общият RAW портфейл има конкуренция за един вход и фиксиран приоритет. Например ранното обръщане в ALL е само подмножество на самостоятелния REVERSAL run.', '',
              'Съвпадат source SHA, 6445 бара, първо време, cutoff и двата checksum; Inputs диалозите са идентични след премахване само на избрания модел и UI focus. Пълният OHLC/HTF export е наличен за B-ALL-FULL; останалите runs имат същите отчетени fingerprints, без претенция за byte equality на всички първични feeds.', '',
              'Подробности, хешове и ограничения: [run_comparison.json](run_comparison.json), [probe_journal_comparison.json](probe_journal_comparison.json).']
    (ROOT/'RUNS_BG.md').write_text('\n'.join(table)+'\n', encoding='utf-8', newline='\n')
    return result


if __name__ == '__main__':
    result = build()
    print(json.dumps({'status': result['status'], 'rows': len(result['run_comparison']),
                      'data_headers_identical': result['reported_data_headers_identical'],
                      'inputs_except_model_identical': result['other_inputs_dialog_text_identical']}, indent=2))
    raise SystemExit(result['status'] == 'FAIL')
