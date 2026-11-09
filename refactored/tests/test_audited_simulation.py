"""Behavioral regressions for the published audited execution contract.

All OHLC below is invented unit-test input. It is never market evidence.
"""
import copy
import hashlib
import json
import sys
import tempfile
import types
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from tools.ref_engine import Bar, Plan, Config, Engine, Paper, valid_plan, target_room, policy_reason, allowed_event
from tools.validate import load_bars, load_events, plans_by_bar, validate_provenance, main as validate
from tools.compare_events import compare

T = 1735732800000
FIXTURES = Path(__file__).parent / 'fixtures'
EXPERIMENT = 'СЪГЛАСУВАН ТРЕНД — ПРОБЕН'


def bar(i, o=100, h=104, l=96, c=101):
    return Bar(i, T+i*900000, T+(i+1)*900000, o, h, l, c)


def plan(side=1, **kw):
    values = dict(id='synthetic', family=1, side=side, signal_bar=0,
                  signal_time=bar(0).time_close, reference=100,
                  stop=90 if side == 1 else 110,
                  targets=(110, 120, 130) if side == 1 else (90, 80, 70),
                  atr=10, cost_price=.3)
    values.update(kw)
    return Plan(**values)


def mirror(b):
    return Bar(b.index, b.time, b.time_close, 200-b.open, 200-b.low, 200-b.high, 200-b.close)


def opened(side=1, config=None):
    p = Paper('RAW', config or Config())
    p.queue(plan(side), bar(0))
    p.step(bar(1))
    return p


def synthetic_baseline_comparison():
    """Compare the stale repository Python engine with its audited-semantic repair.

    This is NOT a baseline-versus-fixed Pine signal or market-performance comparison.
    """
    with zipfile.ZipFile(FIXTURES.parents[1]/'baseline/delivery_103bc307.zip') as archive:
        source = archive.read('refactored/tools/ref_engine.py')
    digest = hashlib.sha256(source).hexdigest()
    if digest != '86b3972a6467df77facf1db11fe1211989d4da622e6627b896e98f63c720b2bb':
        raise ValueError('Unexpected baseline Python engine')
    old = types.ModuleType('_qirvanta_frozen_baseline')
    sys.modules[old.__name__] = old
    try:
        exec(compile(source, 'baseline/ref_engine.py', 'exec'), old.__dict__)
        rows = []
        cases = [('open_targets_then_sl', (125,132,89,100)),
                 ('unknown_path_stop_first', (100,135,89,130)),
                 ('three_targets_no_stop', (100,135,95,130))]
        for side in (1,-1):
            for name, ohlc in cases:
                before = old.Paper('RAW', old.Config())
                before.queue(old.Plan('synthetic',1,side,0,bar(0).time_close,100,
                                     90 if side==1 else 110,
                                     (110,120,130) if side==1 else (90,80,70),10,.3),bar(0))
                before.step(bar(1))
                after = opened(side)
                candle = bar(2,*ohlc)
                candle = candle if side==1 else mirror(candle)
                before.step(candle)
                after.step(candle)
                rows.append({'case':name,'side':side,'ohlc':[candle.open,candle.high,candle.low,candle.close],
                             'roundtrip_cost_price':.3,'old_python_net_r':before.ledger.net,
                             'audited_semantics_python_net_r':after.ledger.net})
        return {'status':'SYNTHETIC_FUNCTIONAL_ONLY','baseline_commit':'103bc30788b18234f5938ed0f5346e142097a6fe',
                'baseline_python_sha256':digest,'scope':'STALE_PYTHON_TOOLING_VS_AUDITED_SEMANTICS_NOT_PINE_OR_MARKET_PERFORMANCE',
                'rows':rows}
    finally:
        sys.modules.pop(old.__name__,None)


class AuditedExecutionTests(unittest.TestCase):
    def test_frozen_baseline_comparison_detects_old_failure(self):
        report = synthetic_baseline_comparison()
        for row in report['rows']:
            if row['case']=='open_targets_then_sl':
                self.assertAlmostEqual(row['old_python_net_r'],-1.03)
                self.assertAlmostEqual(row['audited_semantics_python_net_r'],2/3-.03)
            else:
                self.assertAlmostEqual(row['old_python_net_r'],row['audited_semantics_python_net_r'])

    def test_marketable_open_targets_precede_later_stop_both_sides(self):
        for side in (1, -1):
            with self.subTest(side=side):
                p = opened(side)
                b = bar(2, 125, 132, 89, 100)
                p.step(b if side == 1 else mirror(b))
                self.assertEqual([x['reason'] for x in p.partials], ['SIM_TP1', 'SIM_TP2', 'SIM_SL'])
                self.assertAlmostEqual(p.ledger.gross, 2/3)
                self.assertAlmostEqual(p.ledger.costs, .03)
                self.assertAlmostEqual(p.ledger.net, 2/3-.03)
                self.assertAlmostEqual(sum(x['quantity_fraction'] for x in p.partials), 1)
                self.assertEqual([x['effective_time'] for x in p.partials], [b.time, b.time, None])
                self.assertEqual(p.ledger.ambiguous, 1)

    def test_open_tp3_closes_before_later_stop(self):
        p = opened()
        p.step(bar(2, 135, 140, 80, 85))
        self.assertAlmostEqual(p.ledger.net, 1.97)
        self.assertEqual(p.ledger.ambiguous, 0)
        self.assertEqual(p.last_closed['exit_reason'], 'TP3')

    def test_open_tp1_keeps_original_stop_for_rest_of_bar(self):
        p = opened()
        p.step(bar(2, 115, 116, 89, 95))
        self.assertAlmostEqual(p.ledger.gross, -1/3)
        self.assertEqual(p.partials[-1]['price'], 90)
        self.assertFalse(any(e['event'] == 'STOP_UPDATE' for e in p.events))

    def test_unknown_path_still_stop_first(self):
        p = opened()
        p.step(bar(2, 100, 135, 89, 130))
        self.assertAlmostEqual(p.ledger.net, -1.03)
        self.assertEqual([x['reason'] for x in p.partials], ['SIM_SL'])

    def test_gap_stop_has_known_open_and_correct_reason(self):
        p = opened()
        p.step(bar(2, 85, 135, 80, 100))
        self.assertEqual(p.last_closed['exit_reason'], 'STOP_GAP')
        self.assertEqual(p.last_closed['exit_effective_time'], bar(2).time)
        self.assertAlmostEqual(p.ledger.gross, -1.5)

    def test_open_targets_financing_uses_open_not_close(self):
        p = opened(config=Config(funding_long=2))
        b = Bar(2, bar(1).time+86400000, bar(1).time+87300000, 135, 140, 130, 138)
        p.step(b)
        self.assertAlmostEqual(p.ledger.costs, (.3+2)/10)

    def test_actual_rounded_target_room_both_sides(self):
        for side in (1, -1):
            p = plan(side, stop=100-side*.91, targets=tuple(100+side*x for x in (1.01, 2, 3)),
                     atr=1, cost_price=.01, obstacle=100+side*1.005, required_room=1.1)
            self.assertFalse(valid_plan(p, Config(tick=.01)))
            self.assertTrue(valid_plan(replace(p, obstacle=p.targets[0]), Config(tick=.01)))

    def test_target_room_not_silently_enabled(self):
        self.assertTrue(valid_plan(plan(obstacle=101, required_room=0), Config()))

    def test_frozen_room_rechecked_at_open(self):
        p = plan(obstacle=109, required_room=.5)
        s = Paper('RAW', Config())
        s.queue(p, bar(0))
        s.step(bar(1))
        self.assertEqual(s.last_cancelled['reason'], 'ROOM')
        self.assertEqual(s.ledger.entered, 0)

    def test_mutation_removing_room_guard_is_detectable(self):
        p = plan(obstacle=109, required_room=.5)
        self.assertFalse(valid_plan(p, Config()))
        with patch('tools.ref_engine.target_room', return_value=True):
            self.assertTrue(valid_plan(p, Config()))

    def test_plan_context_is_frozen(self):
        p = plan(local_aligned=True, efficiency_ratio=.4)
        e = Engine(Config(context_policy=EXPERIMENT))
        selected = e.process(bar(0), [p])[0]
        self.assertFalse(p.eligible)
        self.assertTrue(selected.eligible)
        self.assertEqual(e.books['RAW'].plan.efficiency_ratio, .4)

    def test_policy_preserves_raw_and_model_loss(self):
        for policy in ('БАЗОВ', EXPERIMENT):
            e = Engine(Config(context_policy=policy))
            e.process(bar(0), [plan(local_aligned=False, efficiency_ratio=.1)])
            e.process(bar(1), [])
            e.process(bar(2, 100, 104, 89, 92), [])
            self.assertAlmostEqual(e.books['RAW'].ledger.net, -1.03)
            self.assertAlmostEqual(e.books['M1'].ledger.net, -1.03)
            self.assertEqual(e.books['FILTERED'].ledger.closed, 1 if policy == 'БАЗОВ' else 0)

    def test_policy_all_gates_and_boundary(self):
        c = Config(context_policy=EXPERIMENT)
        p = plan(local_aligned=True, efficiency_ratio=.25)
        self.assertEqual(policy_reason(p, c), '')
        for values, reason in [({'countertrend':True}, 'HTF_MISMATCH'),
                               ({'local_aligned':False}, 'LOCAL_MISMATCH'),
                               ({'efficiency_ratio':None}, 'MISSING_EFFICIENCY'),
                               ({'efficiency_ratio':.249}, 'LOW_EFFICIENCY')]:
            self.assertEqual(policy_reason(replace(p, **values), c), reason)

    def test_invalid_context_values_rejected(self):
        for values in ({'efficiency_ratio':float('nan')}, {'efficiency_ratio':1.1},
                       {'required_room':float('nan')}, {'countertrend':'false'}):
            with self.assertRaises(ValueError):plan(**values)
        with self.assertRaises(ValueError):Config(context_policy='UNKNOWN')

    def test_plan_targets_are_three_immutable_prices(self):
        for targets in ([110,120,130], (), (110,), (110,120,130,140)):
            with self.assertRaises(ValueError):plan(targets=targets)

    def test_native_alert_categories_and_master(self):
        self.assertTrue(allowed_event('NATIVE_LEDGER_INCOMPLETE', 'NATIVE'))
        self.assertFalse(allowed_event('NATIVE_LEDGER_INCOMPLETE', 'NATIVE', risk=False))
        self.assertTrue(allowed_event('NATIVE_STOP_SCHEDULED', 'NATIVE'))
        self.assertFalse(allowed_event('NATIVE_STOP_SCHEDULED', 'NATIVE', paper=False))
        for kind in ('NATIVE_LEDGER_INCOMPLETE', 'NATIVE_STOP_SCHEDULED'):
            self.assertFalse(allowed_event(kind, 'NATIVE', master=False))


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.bars = load_bars(FIXTURES/'synthetic_ohlc.csv')
        self.events = load_events(FIXTURES/'synthetic_events.jsonl')
        self.manifest = json.loads((FIXTURES/'synthetic_manifest.json').read_text(encoding='utf-8'))

    def test_context_survives_export_mapping(self):
        self.events[0].update(local_aligned=True, efficiency_ratio=.42)
        p = plans_by_bar(self.events, self.bars)[0][0]
        self.assertTrue(p.local_aligned)
        self.assertEqual(p.efficiency_ratio, .42)

    def test_fixture_provenance_valid(self):
        validate_provenance(self.events, self.manifest, self.bars, Config())

    def test_wrong_symbol_timeframe_and_present_hash_rejected(self):
        for field, value in [('symbol', 'OTHER:FEED'), ('timeframe', '60'), ('core_sha256', 'bad')]:
            ev = copy.deepcopy(self.events)
            ev[0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_provenance(ev, self.manifest, self.bars, Config())

    def test_real_log_requires_matching_hash_and_context(self):
        m = copy.deepcopy(self.manifest)
        m.update(dataset_kind='real', pine_core_sha256='a'*64,
                 pine_inputs={'contextPolicy':'БАЗОВ', 'minEfficiency':.25})
        with self.assertRaises(ValueError):validate_provenance(self.events, m, self.bars, Config())
        self.events[0].update(core_sha256='a'*64, local_aligned=True, efficiency_ratio=.5)
        # This tests metadata validation only; it does not relabel the fixture as market evidence.
        validate_provenance(self.events, m, self.bars, Config())
        self.events[0]['core_sha256'] = 'b'*64
        with self.assertRaises(ValueError):validate_provenance(self.events, m, self.bars, Config())

    def test_policy_config_mismatch_rejected(self):
        self.manifest['pine_inputs'] = {'contextPolicy':EXPERIMENT, 'minEfficiency':.25}
        with self.assertRaises(ValueError):validate_provenance(self.events, self.manifest, self.bars, Config())

    def test_comparison_detects_feed_cost_and_context_changes(self):
        for field, value in [('symbol','OTHER:FEED'), ('timeframe','60'), ('roundtrip_cost_price',999),
                             ('atr_at_signal',20), ('max_risk_atr',2), ('obstacle',109),
                             ('required_room_r',1), ('local_aligned',True), ('efficiency_ratio',.8),
                             ('filter_eligible',True)]:
            ev = copy.deepcopy(self.events)
            ev[0][field] = value
            with self.subTest(field=field):self.assertEqual(len(compare(self.events, ev)['changed']), 1)

    def test_different_code_hashes_reported_separately(self):
        self.events[0]['core_sha256'] = 'a'*64
        ev = copy.deepcopy(self.events)
        ev[0]['core_sha256'] = 'b'*64
        result = compare(self.events, ev)
        self.assertFalse(result['changed'])
        self.assertNotEqual(result['a_core_sha256'], result['b_core_sha256'])
        self.assertIn('NOT_EXECUTION_PARITY', result['scope'])

    def test_utf8_report_and_synthetic_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            self.manifest['provider'] = 'Синтетична проверка'
            manifest_path = path/'manifest.json'
            manifest_path.write_text(json.dumps(self.manifest, ensure_ascii=False), encoding='utf-8')
            rc = validate(['--bars', str(FIXTURES/'synthetic_ohlc.csv'),
                           '--events', str(FIXTURES/'synthetic_events.jsonl'),
                           '--manifest', str(manifest_path), '--out', str(path/'out')])
            self.assertEqual(rc, 0)
            result = json.loads((path/'out/metrics.json').read_text(encoding='utf-8'))
            self.assertEqual(result['status'], 'SYNTHETIC_FUNCTIONAL_ONLY')
            self.assertEqual(result['manifest']['provider'], 'Синтетична проверка')


if __name__ == '__main__':
    unittest.main()
