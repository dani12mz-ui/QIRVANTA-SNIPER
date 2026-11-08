import unittest,re,hashlib,json,tempfile,subprocess,sys,copy
from pathlib import Path
from tools.validate import load_bars,load_events,plans_by_bar,metrics,block_bootstrap_mean,main as validate
from tools.compare_events import compare
ROOT=Path(__file__).resolve().parents[1]

def remove_strings_comments(s):
    out=[];quote=None;escape=False;i=0
    while i<len(s):
        c=s[i]
        if quote:
            if escape:escape=False
            elif c=='\\':escape=True
            elif c==quote:quote=None
            out.append('\n' if c=='\n' else ' ');i+=1;continue
        if c in ('"',"'"):quote=c;out.append(' ');i+=1;continue
        if s[i:i+2]=='//':
            end=s.find('\n',i)
            if end<0:break
            out.extend(' '*(end-i));i=end;continue
        out.append(c);i+=1
    if quote:raise ValueError('Unterminated string')
    return ''.join(out)

def core(s):return s.split('// BEGIN CANONICAL CORE ',1)[1].split('\n',1)[1].split('// END CANONICAL CORE',1)[0]

class SourceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ind=(ROOT/'QIRVANTA_SNIPER_PRO.pine').read_text();cls.strategy=(ROOT/'QIRVANTA_SNIPER_VALIDATION.pine').read_text()
        cls.old=(ROOT/'baseline/QIRVANTA_SNIPER_v7_6_TIMELY_SIGNALS.pine').read_bytes()
    def test_baseline_exact_git_blob(self):self.assertEqual(hashlib.sha1(b'blob '+str(len(self.old)).encode()+b'\0'+self.old).hexdigest(),'46263e3c4740e4fc57553b785ccc76b89df32865')
    def test_generation_reproducible(self):
        before=[(ROOT/f).read_bytes() for f in ('QIRVANTA_SNIPER_PRO.pine','QIRVANTA_SNIPER_VALIDATION.pine')]
        subprocess.run([sys.executable,'tools/build.py'],cwd=ROOT,check=True,stdout=subprocess.DEVNULL)
        after=[(ROOT/f).read_bytes() for f in ('QIRVANTA_SNIPER_PRO.pine','QIRVANTA_SNIPER_VALIDATION.pine')];self.assertEqual(before,after)
    def test_source_core_identical_not_runtime_proof(self):self.assertEqual(core(self.ind),core(self.strategy))
    def test_native_orders_only_in_strategy(self):
        self.assertNotIn('strategy.entry(',self.ind);self.assertIn('strategy.entry(',self.strategy);self.assertIn('strategy.exit(',self.strategy)
    def test_native_has_no_intrabar_recalculation(self):
        for token in ('calc_on_every_tick=false','calc_on_order_fills=false','process_orders_on_close=false'):self.assertIn(token,self.strategy)
    def test_all_native_order_alerts_disabled(self):
        for line in self.strategy.splitlines():
            if re.search(r'strategy\.(entry|exit|close)\(',line):self.assertIn('disable_alert=true',line)
    def test_all_custom_function_calls_defined(self):
        for s in (self.ind,self.strategy):
            code=remove_strings_comments(s)
            defs=set(re.findall(r'^(f_\w+)\([^\n]*\)\s*=>',code,re.M));calls=set(re.findall(r'\b(f_\w+)\s*\(',code));self.assertEqual(calls-defs,set())
    def test_balanced_punctuation_lexical_not_compiler(self):
        pairs={')':'(',']':'[','}':'{'}
        for s in (self.ind,self.strategy):
            stack=[]
            for c in remove_strings_comments(s):
                if c in '([{':stack.append(c)
                elif c in ')]}':self.assertTrue(stack);self.assertEqual(stack.pop(),pairs[c])
            self.assertEqual(stack,[])
    def test_mobile_table_five_rows_default(self):
        self.assertIn('input.string("МОБИЛЕН"',self.ind);self.assertIn('uiMode == "ПОДРОБЕН" ? 9 : 5',self.ind)
    def test_context_rendering_not_discovery(self):
        c=core(self.ind)
        for token in ('showPanel','uiMode','keepLabels'):
            decision=(ROOT/'src/decision.pineinc').read_text();self.assertNotIn(token,decision)
    def test_obstacle_uses_min_distance_not_ema_priority(self):self.assertIn('f_nearest(emaFast, emaSlow,',self.ind)
    def test_fast_fvg_respects_input(self):
        f=(ROOT/'src/decision.pineinc').read_text().split('f_fast(',1)[1].split('f_reversal(',1)[0];self.assertIn('not requireFVG or gap',f)
    def test_last_closed_symmetric_diagnostics(self):
        self.assertIn('buyReasons',self.ind);self.assertIn('sellReasons',self.ind);self.assertIn('for directionIndex = 0 to 1',self.ind)
    def test_confirmed_state_processing_only_close(self):self.assertIn('bool processBar = barstate.isconfirmed',self.ind);self.assertNotIn('varip ',self.ind)
    def test_htf_completed_requests(self):
        lines=[x for x in self.ind.splitlines() if 'request.security(' in x];self.assertEqual(len(lines),2)
        for line in lines:self.assertIn('[1]',line);self.assertIn('lookahead=barmerge.lookahead_on',line)
    def test_input_error_standard_charts_timeframe(self):
        self.assertIn('if not chart.is_standard',self.ind);self.assertIn('tfSeconds == 14400',self.ind)
    def test_no_hidden_import_dependency(self):self.assertNotRegex(self.ind,r'(?m)^import\s');self.assertNotRegex(self.strategy,r'(?m)^import\s')
    def test_native_gap_difference_explicit(self):self.assertIn('NATIVE_DIVERGENCE',self.strategy);self.assertIn('INVALID_OPEN',self.strategy)
    def test_targets_not_repriced_at_simulated_open(self):
        f=(ROOT/'src/paper_engine.pineinc').read_text();self.assertNotIn('target1R',f);self.assertIn('s.p.tp1',f)
    def test_scope_global_alertcondition_gated(self):
        self.assertIn('alertsEnabled and (coreWatch or corePlan or riskEvent or executionEvent)',self.ind)
        for line in self.ind.splitlines():
            if line.startswith('alertcondition('):self.assertRegex(line,r'^alertcondition\(alert(?:Buy|Sell|Risk)Now,')
    def test_plot_call_lexical_budget_includes_assignment(self):
        code=remove_strings_comments(self.ind)
        self.assertEqual(len(re.findall(r'\bplot\(',code)),2);self.assertEqual(len(re.findall(r'\balertcondition\(',code)),3)
        old=remove_strings_comments(self.old.decode());self.assertEqual(len(re.findall(r'\bplot\(',old)),17)
    def test_no_lookahead_label_offsets(self):
        self.assertNotIn('offset=-',self.ind);self.assertNotRegex(self.ind,r'\bbar_index\s*-\s*pivotRight')
    def test_test_status_not_compiler_pass(self):
        build=json.loads((ROOT/'reports/build.json').read_text());self.assertEqual(build['pine_compile'],'UNVERIFIED')

class ValidatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.fixture=ROOT/'tests/fixtures'
    def test_fixture_plan_mapping(self):
        bars=load_bars(self.fixture/'synthetic_ohlc.csv');ev=load_events(self.fixture/'synthetic_events.jsonl');ps=plans_by_bar(ev,bars);self.assertEqual(ps[0][0].signal_time,bars[0].time_close)
    def test_fixture_workflow_outputs_explicit_synthetic(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc=validate(['--bars',str(self.fixture/'synthetic_ohlc.csv'),'--events',str(self.fixture/'synthetic_events.jsonl'),'--manifest',str(self.fixture/'synthetic_manifest.json'),'--out',tmp]);self.assertEqual(rc,0)
            r=json.loads((Path(tmp)/'metrics.json').read_text());self.assertEqual(r['status'],'SYNTHETIC_FUNCTIONAL_ONLY');self.assertAlmostEqual(r['books']['RAW']['net_r'],1.97)
    def test_missing_data_not_fabricated(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc=validate(['--out',tmp]);self.assertEqual(rc,2);self.assertEqual(json.loads((Path(tmp)/'status.json').read_text())['status'],'PERFORMANCE_UNVERIFIED_MISSING_DATA');self.assertFalse((Path(tmp)/'trades.csv').exists())
    def test_candidate_outside_data_rejected(self):
        ev=load_events(self.fixture/'synthetic_events.jsonl');bars=load_bars(self.fixture/'synthetic_ohlc.csv');ev[0]['known_time']+=1
        with self.assertRaises(ValueError):plans_by_bar(ev,bars)
    def test_event_comparison_changed_level(self):
        a=load_events(self.fixture/'synthetic_events.jsonl');b=copy.deepcopy(a);b[0]['tp1']+=1;self.assertEqual(len(compare(a,b)['changed']),1)
    def test_empty_parity_is_not_pass(self):self.assertTrue(compare([],[])['empty_comparison'])
    def test_metrics_and_dd(self):
        ts=[{'net_r':r,'gross_r':r+.1,'cost_r':.1,'hold_bars':2} for r in (1,-2,1)]
        m=metrics(ts);self.assertEqual(m['net_r'],0);self.assertEqual(m['closed_equity_max_drawdown_r'],2);self.assertEqual(m['profit_factor'],1)
    def test_no_losses_pf_explicit(self):
        m=metrics([{'net_r':1,'gross_r':1.1,'cost_r':.1,'hold_bars':1}]);self.assertIsNone(m['profit_factor']);self.assertEqual(m['profit_factor_status'],'NO_LOSSES')
    def test_bootstrap_requires_sample(self):self.assertEqual(block_bootstrap_mean([])['status'],'INSUFFICIENT_SAMPLE')
    def test_duplicate_event_detection(self):
        line=(self.fixture/'synthetic_events.jsonl').read_text()
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/'e.jsonl';f.write_text(line+line)
            with self.assertRaises(ValueError):load_events(f)
    def test_invalid_ohlc_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            f=Path(tmp)/'b.csv';f.write_text('time,time_close,open,high,low,close\n0,10,100,90,101,100\n')
            with self.assertRaises(ValueError):load_bars(f)
