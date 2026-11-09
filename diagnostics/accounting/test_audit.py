"""Explicitly synthetic regression records. These tests are not Pine simulation."""
import copy
import csv
from decimal import Decimal as D
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from audit import AuditError, audit, read_jsonl, json_default


def fixture(prices=(110, 120, 130), side=1, family=2, quantity=3):
    common = {'schema': 'qirvanta.native.v1', 'run_id': 'synthetic'}
    header = dict(common, kind='run', symbol='SYNTHETIC', timeframe='15',
                  source_sha256='a'*64, origin='SYNTHETIC', pointvalue=1, mintick='.001',
                  account_currency='USD', symbol_currency='USD')
    plan = dict(common, kind='submitted', plan_id='P1', entry_id='P1',
                submitted_time=1000, submitted_bar=10, quantity=quantity,
                family=family, side=side, htf_bias=side, regime='TREND',
                reference=100, initial_sl=90 if side == 1 else 110,
                countertrend=False, local_aligned=True)
    rows = [dict(common, kind='closed', trade_index=i, entry_id='P1',
                 entry_time=1000, entry_bar_index=11, size=side,
                 entry_price=100, exit_id=f'P1|T{i+1}', exit_comment=f'TP{i+1}',
                 exit_time=2000+i*100, exit_bar_index=20+i, exit_price=px,
                 profit=str((D(str(px))-100)*side-D('.35')),
                 commission='.35', max_drawdown=7, max_runup=15)
            for i, px in enumerate(prices)]
    snapshot = dict(common, kind='snapshot', at_time=3000, at_bar=30,
                    closed_count=len(rows), first_closed_index=0,
                    submitted_count=1, open_complete=True, open=[],
                    netprofit=str(sum((D(x['profit']) for x in rows), D(0))),
                    max_drawdown=777)
    return [header, plan, *rows, snapshot]


class CampaignMetrics(unittest.TestCase):
    def test_three_partials_are_one_campaign_and_fee_is_subtracted_once(self):
        r = audit(fixture())
        self.assertEqual(r['status'], 'PASS_OBSERVED_JOURNAL')
        self.assertEqual(r['complete_campaign_metrics']['count'], 1)
        self.assertEqual(r['observed_closed_row_metrics']['count'], 3)
        self.assertEqual(r['complete_campaign_metrics']['net_cash'], D('58.95'))
        self.assertEqual(r['cash_reconciliation']['commission_cash'], D('1.05'))
        self.assertEqual(r['cash_reconciliation']['api_profit_classifications'], {'NET': 3})
        self.assertEqual(r['campaigns'][0]['net_r'], D('1.965'))

    def test_partial_winner_does_not_make_whole_entry_a_winner(self):
        r = audit(fixture((110, 90, 90)))
        self.assertEqual(r['observed_closed_row_metrics']['wins'], 1)
        self.assertEqual(r['complete_campaign_metrics']['wins'], 0)
        self.assertEqual(r['complete_campaign_metrics']['losses'], 1)
        self.assertEqual(r['complete_campaign_metrics']['average_loss_cash'], D('-11.05'))

    def test_partially_open_campaign_keeps_realized_cash_but_is_not_completed(self):
        rows = fixture((110,))
        rows[-1]['open'] = [dict(entry_id='P1', entry_time=1000, entry_bar_index=11, size=2, entry_price=100)]
        r = audit(rows)
        self.assertEqual(r['status'], 'PASS_OBSERVED_JOURNAL')
        self.assertEqual(r['campaign_state_counts'], {'PARTIALLY_OPEN': 1})
        self.assertEqual(r['complete_campaign_metrics']['count'], 0)
        self.assertEqual(r['observed_closed_row_metrics']['net_cash'], D('9.65'))
        self.assertIsNone(r['campaigns'][0]['net_r'])

    def test_missing_part_flat_snapshot_is_incomplete_not_a_closed_winner(self):
        r = audit(fixture((110,)))
        self.assertEqual(r['status'], 'INCOMPLETE')
        self.assertEqual(r['complete_campaign_metrics']['count'], 0)
        self.assertIn('QUANTITY_NOT_RECONCILED', r['campaigns'][0]['incomplete_reasons'])

    def test_incomplete_open_snapshot_cannot_prove_completion(self):
        rows = fixture(); rows[-1]['open_complete'] = False
        r = audit(rows)
        self.assertEqual(r['campaign_state_counts'], {'INCOMPLETE': 1})

    def test_missing_global_index_is_reported_and_net_match_not_inferred(self):
        rows = fixture(); del rows[3]
        r = audit(rows)
        self.assertEqual(r['coverage']['missing_indices'], [1])
        self.assertIsNone(r['cash_reconciliation']['matches'])
        self.assertEqual(r['status'], 'INCOMPLETE')

    def test_trimmed_history_stays_incomplete_even_if_available_rows_reconcile(self):
        rows = fixture(); rows[-1]['first_closed_index'] = 1
        r = audit(rows)
        self.assertFalse(r['coverage']['full_closed_history'])
        self.assertEqual(r['status'], 'INCOMPLETE')

    def test_missing_plan_metadata_does_not_derive_family_or_quantity_from_id(self):
        rows = fixture(); del rows[1]; rows[-1].pop('submitted_count')
        r = audit(rows)
        self.assertEqual(r['complete_campaign_metrics']['count'], 0)
        self.assertIsNone(r['campaigns'][0]['family'])
        self.assertEqual(r['observed_closed_row_metrics']['net_cash'], D('58.95'))

    def test_replayed_identical_records_are_idempotent(self):
        rows = fixture(); rows += copy.deepcopy(rows)
        r = audit(rows)
        self.assertEqual(r['observed_closed_row_metrics']['count'], 3)
        self.assertEqual(r['coverage']['duplicates_ignored']['closed'], 3)

    def test_same_bar_entry_exit_is_a_real_complete_campaign(self):
        rows = fixture()
        for row in rows[2:-1]: row.update(exit_time=1000, exit_bar_index=11)
        r = audit(rows)
        self.assertEqual(r['complete_campaign_metrics']['count'], 1)
        self.assertEqual(r['campaigns'][0]['holding_bars'], 1)

    def test_combined_market_close_of_two_units_does_not_require_three_rows(self):
        rows = fixture((110,120)); rows[3].update(size=2, commission='.70', profit='39.30', exit_comment='TIME')
        rows[-1]['netprofit'] = '48.95'
        r = audit(rows)
        self.assertEqual(r['campaign_state_counts'], {'COMPLETE': 1})
        self.assertEqual(r['campaigns'][0]['closed_record_count'], 2)
        self.assertEqual(r['campaigns'][0]['closed_quantity'], 3)

    def test_short_pointvalue_and_nonpositive_risk(self):
        rows = fixture((90, 80, 70), side=-1)
        rows[0]['pointvalue'] = 100
        for row in rows[2:-1]: row['profit'] = str((100-D(str(row['exit_price'])))*100-D('.35'))
        rows[-1]['netprofit'] = '5998.95'; rows[1]['initial_sl'] = 99
        r = audit(rows)
        self.assertEqual(r['complete_campaign_metrics']['net_cash'], D('5998.95'))
        self.assertIsNone(r['campaigns'][0]['net_r'])

    def test_cash_drawdown_is_not_native_equity_drawdown_or_sum_of_part_excursions(self):
        r = audit(fixture((110, 90, 90)))
        self.assertEqual(r['closed_cash_drawdown']['max_drawdown_cash'], D('20.70'))
        self.assertEqual(r['native_equity_max_drawdown']['value'], 777)
        self.assertIsNone(r['campaigns'][0]['campaign_max_drawdown'])
        self.assertIsNone(r['campaigns'][0]['campaign_max_runup'])

    def test_neutral_htf_is_not_against_trend(self):
        rows = fixture(); rows[1].update(htf_bias=0, regime='COUNTERTREND', countertrend=True)
        r = audit(rows)
        self.assertIn('NEUTRAL_HTF', r['complete_campaign_groups']['trend_relation'])
        self.assertNotIn('AGAINST_HTF', r['complete_campaign_groups']['trend_relation'])
        self.assertTrue(r['campaigns'][0]['submitted_metadata']['countertrend'])

    def test_model_side_trend_splits_keep_explicit_realized_drawdown_and_exit_counts(self):
        rows=fixture((110,90,90));rows[3]['exit_comment']=rows[4]['exit_comment']='SL'
        r=audit(rows)
        for dimension,key in (('family','2'),('side','1'),('trend_relation','WITH_HTF')):
            group=r['complete_campaign_groups'][dimension][key]
            self.assertEqual(group['closed_cash_event_drawdown'],D('20.70'))
            self.assertEqual(group['exit_comment_record_counts'],{'TP1':1,'SL':2})
            self.assertEqual(group['exit_comment_quantities']['SL'],2)
            self.assertEqual(group['final_exit_comment_campaign_counts'],{'SL':1})

    def test_complete_campaign_order_is_invariant_to_export_record_order(self):
        rows = fixture()
        self.assertEqual(audit(rows)['complete_campaign_metrics'], audit(list(reversed(rows)))['complete_campaign_metrics'])

    def test_unfilled_submission_is_not_manufactured_from_no_open_position(self):
        rows = fixture(())
        r = audit(rows)
        self.assertEqual(len(r['submitted_without_recorded_fill']), 1)
        self.assertEqual(r['complete_campaign_metrics']['count'], 0)
        self.assertIsNone(r['complete_campaign_metrics']['profit_factor'])

    def test_reused_entry_id_instances_are_preserved_but_not_silently_merged(self):
        rows = fixture(); more = copy.deepcopy(rows[2:-1])
        for row in more:
            row['trade_index'] += 3; row['entry_time'] = 1500; row['entry_bar_index'] = 15
        rows[-1].update(closed_count=6, netprofit='117.90')
        r = audit(rows + more)
        self.assertEqual(len(r['campaigns']), 2)
        self.assertEqual(r['complete_campaign_metrics']['count'], 0)
        self.assertEqual(r['observed_closed_row_metrics']['net_cash'], D('117.90'))

    def test_fractional_quantity_does_not_round_up_or_leave_phantom_remainder(self):
        rows = fixture(quantity='.3')
        for row in rows[2:-1]:
            row['size'] = '.1'; row['profit'] = str((D(str(row['exit_price']))-100)*D('.1')-D('.35'))
        rows[-1]['netprofit'] = '4.95'
        r = audit(rows)
        self.assertEqual(r['campaigns'][0]['closed_quantity'], D('.3'))
        self.assertEqual(r['campaign_state_counts'], {'COMPLETE': 1})

    def test_reported_native_cash_mismatch_is_failure_not_forced_match(self):
        rows = fixture(); rows[-1]['netprofit'] = 100000
        r = audit(rows)
        self.assertEqual(r['status'], 'RECONCILIATION_FAILED')
        self.assertEqual(r['complete_campaign_metrics']['net_cash'], D('58.95'))

    def test_gross_api_semantics_can_be_observed_without_double_charging_fees(self):
        rows = fixture(); rows[0]['profit_semantics'] = 'GROSS_EXCLUDES_COMMISSION'
        for row in rows[2:-1]: row['profit'] = str(D(row['profit'])+D('.35'))
        r = audit(rows)
        self.assertEqual(r['cash_reconciliation']['api_profit_classifications'], {'GROSS': 3})
        self.assertEqual(r['complete_campaign_metrics']['net_cash'], D('58.95'))

    def test_incorrect_declared_api_semantics_is_a_reconciliation_failure(self):
        rows = fixture(); rows[0]['profit_semantics'] = 'GROSS_EXCLUDES_COMMISSION'
        self.assertEqual(audit(rows)['status'], 'RECONCILIATION_FAILED')

    def test_subtick_risk_is_not_magnified_into_fabricated_r(self):
        rows=fixture();rows[1]['initial_sl']='99.9999'
        r=audit(rows)
        self.assertIsNone(r['campaigns'][0]['net_r'])
        self.assertEqual(r['complete_campaign_metrics']['net_cash'],D('58.95'))

    def test_mixed_raw_profit_semantics_cannot_pass_an_unknown_declaration(self):
        rows=fixture();rows[2]['profit']='10'
        self.assertEqual(audit(rows)['status'],'RECONCILIATION_FAILED')


class RejectContradictions(unittest.TestCase):
    def test_conflicting_duplicate(self):
        rows = fixture(); changed = dict(rows[2], commission=1)
        with self.assertRaisesRegex(AuditError, 'conflicting duplicate'):
            audit(rows+[changed])

    def test_overclose_or_open_plus_close_exceeding_total(self):
        rows = fixture(); rows[2]['size'] = 2
        with self.assertRaisesRegex(AuditError, 'exceeds'):
            audit(rows)

    def test_wrong_side(self):
        rows = fixture(); rows[1]['side'] = -1
        with self.assertRaisesRegex(AuditError, 'side/time'):
            audit(rows)

    def test_fx_is_not_silently_treated_as_same_currency(self):
        rows = fixture(); rows[0]['symbol_currency'] = 'JPY'
        with self.assertRaisesRegex(AuditError, 'FX'):
            audit(rows)

    def test_invalid_numbers_and_reverse_time(self):
        for field, value in (('size', 0), ('profit', 'NaN'), ('commission', -1), ('exit_time', 500)):
            rows = fixture(); rows[2][field] = value
            with self.subTest(field=field), self.assertRaises(AuditError): audit(rows)

    def test_missing_snapshot_and_mixed_run(self):
        with self.assertRaisesRegex(AuditError, 'snapshot'): audit(fixture()[:-1])
        rows = fixture(); rows[2]['run_id'] = 'other'
        with self.assertRaisesRegex(AuditError, 'run_id'): audit(rows)

    def test_future_fill_outside_snapshot(self):
        rows = fixture(); rows[2]['exit_time'] = 5000
        with self.assertRaisesRegex(AuditError, 'later than final snapshot'): audit(rows)


class Ingestion(unittest.TestCase):
    def test_ndjson_and_explicit_pine_timestamp_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'log.txt'
            path.write_text('\n'.join('[2026-11-09T00:00:00Z] '+json.dumps(x) for x in fixture()), encoding='utf-8')
            with self.assertRaises(AuditError): read_jsonl(path)
            rows, meta = read_jsonl(path, allow_pine_prefix=True)
            self.assertEqual(len(meta['removed_log_prefixes']), len(rows))
            self.assertEqual(audit(rows)['status'], 'PASS_OBSERVED_JOURNAL')

    def test_multiline_pine_csv_supported_without_discarding_raw_origin_gap(self):
        with tempfile.TemporaryDirectory() as d:
            rows = fixture(); rows[0].pop('origin')
            path = Path(d)/'log.csv'
            with path.open('w', encoding='utf-8', newline='') as f:
                writer = csv.writer(f); writer.writerow(['Date','Message'])
                writer.writerow(['2026-11-09','QIRVANTA_DATA_BEGIN\n'+json.dumps(rows[0])])
                writer.writerow(['2026-11-09','\n'.join(json.dumps(x) for x in rows[1:])+'\nQIRVANTA_DATA_END'])
            parsed, meta = read_jsonl(path, origin_override='REAL_TRADINGVIEW')
            self.assertEqual(meta['format'], 'PINE_LOGS_CSV')
            self.assertEqual(len(meta['header_supplements']), 1)
            self.assertEqual(audit(parsed)['status'], 'PASS_OBSERVED_JOURNAL')

    def test_noise_truncation_nonfinite_and_unknown_kind_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'log.txt'
            for content in ('noise', '{"x":NaN}', 'QIRVANTA_DATA_BEGIN\n'+json.dumps(fixture()[0])):
                path.write_text(content, encoding='utf-8')
                with self.subTest(content=content), self.assertRaises(AuditError): read_jsonl(path)
        rows = fixture(); rows[2]['kind'] = 'not_a_trade'
        with self.assertRaisesRegex(AuditError, 'unrecognized'): audit(rows)

    def test_cli_cannot_overwrite_raw_input(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'input.jsonl'; path.write_text('{}', encoding='utf-8')
            result = subprocess.run([sys.executable, str(Path(__file__).with_name('audit.py')), str(path), '--out', str(path)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(path.read_text(), '{}')

    def test_report_is_strict_json_without_infinity(self):
        encoded = json.dumps(audit(fixture()), default=json_default, allow_nan=False)
        self.assertNotIn('Infinity', encoded)

    def test_explicit_bar_section_is_hashed_and_not_mistaken_for_trades(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'log.txt'
            body='\n'.join(json.dumps(x) for x in fixture())+'\nQIRVANTA_BARS_BEGIN\n{"columns":["time","close"]}\n[1000,100]\nQIRVANTA_BARS_END\n'
            path.write_text(body,encoding='utf-8')
            rows, meta=read_jsonl(path)
            self.assertEqual(len(rows),6)
            self.assertEqual(meta['separate_observed_bars']['rows'],1)
            self.assertEqual(audit(rows)['observed_closed_row_metrics']['count'],3)
            path.write_text(body.replace('QIRVANTA_BARS_END',''),encoding='utf-8')
            with self.assertRaisesRegex(AuditError,'incomplete observed-bars'):read_jsonl(path)

    def test_unknown_warning_probe_schema_is_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'log.txt';path.write_text('QIRVANTA_WARNING_PROBE {"schema":"other","kind":"history_scope"}',encoding='utf-8')
            with self.assertRaisesRegex(AuditError,'unrecognized warning'):read_jsonl(path)


if __name__ == '__main__': unittest.main()
