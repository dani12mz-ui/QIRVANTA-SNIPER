"""Architectural/causality regressions, not a substitute for TradingView compilation."""
import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import tempfile
import unittest
from unittest.mock import patch
from tools import build
from tools.check_signal_parity import check,signal_inputs

ROOT=Path(__file__).resolve().parents[1]

def lexical_code(s):
    return re.sub(r'"(?:\\.|[^"\\])*"|//[^\n]*',lambda m:'\n'*m[0].count('\n'),s)

class UnifiedArtifact(unittest.TestCase):
    def source(self):return build.assemble()[0][build.OUTPUT]
    def test_one_standalone_strategy_not_indicator_or_library(self):
        source=self.source();code=lexical_code(source)
        self.assertEqual(source.count('//@version=6'),1)
        self.assertEqual(len(re.findall(r'(?m)^strategy\(',code)),1)
        self.assertNotRegex(code,r'(?m)^(indicator|library)\(')
        self.assertNotRegex(code,r'(?m)^import\s')
        self.assertIn('overlay=true',next(line for line in source.splitlines() if line.startswith('strategy(')))
    def test_complete_build_reproducible_byte_for_byte(self):
        self.assertEqual((ROOT/build.OUTPUT).read_bytes(),self.source().encode('utf-8'))
        self.assertEqual(list(build.OUTPUTS),['QIRVANTA_UNIFIED.pine'])
    def test_build_check_detects_drift_without_overwriting(self):
        with tempfile.TemporaryDirectory() as td:
            dest=Path(td);shutil.copytree(ROOT/'src',dest/'src')
            altered=self.source().replace('\n','\r\n').encode()
            (dest/build.OUTPUT).write_bytes(altered)
            with patch.object(build,'ROOT',dest),contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(build.build(check=True),1)
            self.assertEqual((dest/build.OUTPUT).read_bytes(),altered)
            self.assertFalse((dest/'reports').exists())
    def test_no_duplicated_input_or_custom_function_declarations(self):
        source=self.source();code=lexical_code(source)
        names=re.findall(r'(?m)^(?:bool|int|float|string)\s+(\w+)\s*=\s*input\.',code)
        self.assertEqual(len(names),len(set(names)))
        functions=re.findall(r'(?m)^(f_\w+)\([^\n]*\)\s*=>',code)
        self.assertEqual(len(functions),len(set(functions)))
        calls=set(re.findall(r'\b(f_\w+)\s*\(',code))
        self.assertEqual(calls-set(functions),set())
    def test_balance_lexical_not_pine_compile(self):
        stack=[];pairs={')':'(',']':'[','}':'{'}
        for c in lexical_code(self.source()):
            if c in '([{':stack.append(c)
            elif c in pairs:
                self.assertTrue(stack);self.assertEqual(stack.pop(),pairs[c])
        self.assertEqual(stack,[])
    def test_source_and_decision_defaults_preserved(self):
        report=check()
        self.assertEqual(report['differences'],[])
        self.assertGreater(report['signal_input_count'],60)
    def test_visual_inputs_cannot_change_decision_or_shadow_core(self):
        for name in ('features','decision','paper_engine'):
            code=lexical_code(build.text(name))
            for setting in ('uiMode','showPanel','keepLabels','keepSignalLabels','panelPosition','showContextLabels','showSignalMarkers'):
                self.assertNotRegex(code,r'\b'+setting+r'\b')
    def test_no_future_signal_or_retroactive_drawing_offsets(self):
        source=self.source()
        self.assertNotIn('offset=-',source)
        self.assertNotRegex(source,r'bar_index\s*-\s*pivotRight')
        self.assertNotIn('varip ',lexical_code(source))
        for line in build.text('features').splitlines():
            if 'request.security(' in line:
                self.assertIn('[1]',line);self.assertIn('lookahead=barmerge.lookahead_on',line)
        self.assertIn('bool processBar = barstate.isconfirmed',source)
    def test_constant_compatible_native_execution_defaults(self):
        header=build.text('native_header')
        for setting in ('calc_on_every_tick=false','calc_on_order_fills=false','process_orders_on_close=false','pyramiding=0','use_bar_magnifier=false','initial_capital=100000','commission_value=0.175','slippage=2','margin_long=100','margin_short=100'):
            self.assertIn(setting,header)
        self.assertNotIn('input.',header)
    def test_baseline_entire_delivered_tree_preserved(self):
        manifest=json.loads((ROOT/'reports/baseline_manifest.json').read_text(encoding='utf-8'))
        for name,expected in manifest['files_sha256'].items():
            self.assertEqual(hashlib.sha256((ROOT.parent/name).read_bytes()).hexdigest(),expected,name)
    def test_synthetic_reference_tooling_not_rewritten_during_merge(self):
        for name in ('ref_engine.py','validate.py','compare_events.py','compare_runs.py'):
            self.assertEqual((ROOT/'tools'/name).read_text(encoding='utf-8'),(ROOT.parent/'refactored/tools'/name).read_text(encoding='utf-8'))

if __name__=='__main__':unittest.main()
