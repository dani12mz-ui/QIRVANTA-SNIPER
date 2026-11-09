"""Evaluate the actual Pine route expressions, not a second handwritten router.

This deliberately covers the side-effect-free subset used by f_allowedUnifiedEvent.
It is not a Pine compiler or a test of TradingView alert delivery.
"""
from pathlib import Path
from types import SimpleNamespace
import re
import unittest

ROOT=Path(__file__).resolve().parents[1]

def route(kind, book, **switches):
    source=(ROOT/'src/alerts_helpers.pineinc').read_text(encoding='utf-8')
    scope=dict(e=SimpleNamespace(kind=kind,book=book),
               str=SimpleNamespace(startswith=lambda value,prefix:value.startswith(prefix)))
    for name in ('alertsEnabled','alertNativeFills','alertPlan','alertWatch','alertRisk','alertDiagnostics','alertShadow'):
        scope[name]=switches.get(name,True)
    risk=source.split('f_isRiskEvent(Event e) =>\n',1)[1].split('\nf_allowedUnifiedEvent',1)[0].strip()
    scope['f_isRiskEvent']=lambda e:eval(risk,{'__builtins__':{}},scope)
    body=source.split('f_allowedUnifiedEvent(Event e) =>\n',1)[1].split('\nf_readableEvent',1)[0]
    for line in body.strip().splitlines():
        line=line.strip()
        if line.startswith('bool '):
            name,expr=line[5:].split(' = ',1)
            scope[name]=eval(expr,{'__builtins__':{}},scope)
        else:
            return eval(line,{'__builtins__':{}},scope)
    raise AssertionError('Missing Pine route return')

class Alerts(unittest.TestCase):
    def test_no_manual_duplicate_native_fills_even_all_categories_on(self):
        for kind in ('NATIVE_ENTRY','NATIVE_EXIT_0','NATIVE_EXIT_1','NATIVE_EXIT_2','NATIVE_CLOSED'):
            self.assertFalse(route(kind,'NATIVE'),kind)

    def test_selected_plan_is_not_an_executed_fill(self):
        self.assertTrue(route('NATIVE_SUBMITTED','NATIVE'))
        self.assertFalse(route('NATIVE_SUBMITTED','NATIVE',alertPlan=False))
        self.assertFalse(route('NATIVE_ADMITTED','NATIVE'))

    def test_unselected_candidate_requires_diagnostic_opt_in(self):
        self.assertFalse(route('CONFIRMED','CORE',alertDiagnostics=False))
        self.assertTrue(route('CONFIRMED','CORE',alertDiagnostics=True))

    def test_master_key_mutes_every_route(self):
        for book in ('CORE','NATIVE','RAW','FILTERED','M1'):
            for kind in ('WATCH','CONFIRMED','NATIVE_SUBMITTED','NATIVE_BLOCKED','REJECTED','SIM_TP1','CLOSED'):
                self.assertFalse(route(kind,book,alertsEnabled=False),(book,kind))

    def test_shadow_events_require_explicit_category(self):
        for kind in ('SIM_ENTRY','SIM_TP1','SIM_TP2','CLOSED'):
            self.assertFalse(route(kind,'RAW',alertShadow=False))
            self.assertTrue(route(kind,'RAW',alertShadow=True))

    def test_pending_and_stop_risks_have_distinct_route(self):
        for kind in ('NATIVE_BLOCKED','NATIVE_NOT_FILLED','NATIVE_STOP_SCHEDULED','NATIVE_DIVERGENCE'):
            self.assertTrue(route(kind,'NATIVE'))
            self.assertFalse(route(kind,'NATIVE',alertRisk=False))

    def test_fill_template_uses_supported_external_placeholders(self):
        header=(ROOT/'src/native_header.pineinc').read_text(encoding='utf-8')
        helper=(ROOT/'src/alerts_helpers.pineinc').read_text(encoding='utf-8')
        self.assertIn('//@strategy_alert_message {{strategy.order.alert_message}}',header)
        for item in ('{{strategy.order.price}}','{{strategy.order.contracts}}','{{time}}','{{timenow}}'):
            self.assertIn(item,header)
        self.assertNotIn('{{',helper)
        self.assertIn('p.signalTime',helper)
        self.assertIn('e.effectiveTime',helper)
        self.assertIn('e.knownTime',helper)

    def test_every_order_call_honors_master_and_fill_category(self):
        native=(ROOT/'src/native_adapter.pineinc').read_text(encoding='utf-8')
        calls=[line for line in native.splitlines() if re.search(r'strategy\.(entry|exit|close)\(',line)]
        self.assertGreaterEqual(len(calls),5)
        for line in calls:
            self.assertRegex(line,r'disable_alert\s*=\s*not\s*\(alertsEnabled and alertNativeFills\)')
            self.assertIn('alert_message=',line)

    def test_dispatcher_preserves_all_journal_events_including_simultaneous_exits(self):
        body=(ROOT/'src/alerts.pineinc').read_text(encoding='utf-8')
        self.assertIn('for e in events',body)
        self.assertLess(body.index('log.info(f_json(e))'),body.index('if f_allowedUnifiedEvent(e)'))
        self.assertEqual(len(re.findall(r'\balert\(',body)),1)
        self.assertNotIn('alertcondition(',body)
        self.assertIn('alert.freq_once_per_bar_close',body)

if __name__=='__main__':unittest.main()
