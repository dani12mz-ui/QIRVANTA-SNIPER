"""Evaluate extracted FAST predicate algebra offline; NOT a Pine runtime/compiler.

These tests execute the boolean expressions read from the additive Pine source,
including their negative cases and a directional mirror. They do not certify
Pine state/rollback, native execution or profitability.
"""
from pathlib import Path
import math
import re
from types import SimpleNamespace
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "unified/experimental/tradingview/candidates.pineinc"


def extracted_gate(mode, side=1, **changes):
    text = SOURCE.read_text(encoding="utf-8")
    block = text.split("f_tvFastGate(Plan p) =>\n", 1)[1].split("\nif processBar", 1)[0]
    values = dict(reference=100., obstacle=100. + side * 2., risk=1.,
                  tp1=100. + side, atrValue=2.)
    for name in tuple(values):
        if name in changes:
            values[name] = changes.pop(name)
    values["side"] = side
    environment = dict(
        p=SimpleNamespace(**values), d=side, close=100., open=100. - side * 1.4,
        boundary=100. - side * .2, barRange=2., atr=2., prior2Close=100. - side,
        rsi=50 + side * 10, tvH1Bias=side, tvH4Bias=side, tvRegime=side,
        tvLondonActive=True, tvNewYorkActive=False, syminfo=SimpleNamespace(mintick=.01),
        math=SimpleNamespace(max=max), na=lambda x: x is None or math.isnan(x),
    )
    environment.update(changes)
    for name, expression in re.findall(r"^    bool (\w+) = (.+)$", block, re.M):
        environment[name] = eval(expression.replace("close[2]", "prior2Close"),
                                 {"__builtins__": {}}, environment)
    clauses = dict(re.findall(r'^        "(F\d+)" => (.+?) \? "" :', block, re.M))
    return bool(eval(clauses.get(mode, "True"), {"__builtins__": {}}, environment))


class FastExtractedPredicateTests(unittest.TestCase):
    def test_default_original_fast_has_no_additional_gate(self):
        self.assertTrue(extracted_gate("F0", tvH1Bias=0, tvH4Bias=0, tvRegime=2,
                                       tvLondonActive=False, obstacle=None))

    def test_directional_mirror_accepts_all_non_delayed_variants(self):
        for side in (1, -1):
            for mode in ("F0", "F1", "F3", "F5", "F6", "F7", "F8", "F9", "F10"):
                with self.subTest(side=side, mode=mode):
                    self.assertTrue(extracted_gate(mode, side))

    def test_rejected_f1_is_directional_not_absolute_body(self):
        for side in (1, -1):
            self.assertFalse(extracted_gate("F1", side, open=100. + side * 1.4))
            self.assertFalse(extracted_gate("F1", side, open=100. - side * 1.0))

    def test_f3_requires_both_confirmed_contexts(self):
        for side in (1, -1):
            self.assertFalse(extracted_gate("F3", side, tvH1Bias=0))
            self.assertFalse(extracted_gate("F3", side, tvH4Bias=-side))

    def test_f5_unknown_or_before_actual_rounded_target_rejects(self):
        for side in (1, -1):
            self.assertFalse(extracted_gate("F5", side, obstacle=None))
            self.assertFalse(extracted_gate("F5", side, obstacle=100. + side * .99))
            self.assertFalse(extracted_gate("F5", side, obstacle=100. + side * 1.005,
                                            tp1=100. + side * 1.01))
            self.assertTrue(extracted_gate("F5", side, obstacle=100. + side * 1.01,
                                           tp1=100. + side * 1.01))

    def test_f6_f7_regime_and_freshness_are_independent(self):
        for side in (1, -1):
            self.assertFalse(extracted_gate("F6", side, tvRegime=2))
            self.assertTrue(extracted_gate("F7", side, boundary=100. - side * .5))
            self.assertFalse(extracted_gate("F7", side, boundary=100. - side * .501))

    def test_f8_f9_have_negative_controls(self):
        for side in (1, -1):
            self.assertFalse(extracted_gate("F8", side, rsi=50.))
            self.assertFalse(extracted_gate("F8", side, prior2Close=100. - side * .49))
            self.assertFalse(extracted_gate("F9", side, tvLondonActive=False, tvNewYorkActive=False))
            self.assertTrue(extracted_gate("F9", side, tvLondonActive=False, tvNewYorkActive=True))

    def test_f10_is_only_f3_plus_f7_not_reintroduced_f1(self):
        for side in (1, -1):
            self.assertTrue(extracted_gate("F10", side, open=100. - side * .5))
            self.assertFalse(extracted_gate("F10", side, tvH1Bias=0))
            self.assertFalse(extracted_gate("F10", side, boundary=100. - side * .501))


if __name__ == "__main__":
    unittest.main()
