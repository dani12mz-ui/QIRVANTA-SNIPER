"""Regression and positive-control tests; never claim Python compiles Pine."""
import re
import subprocess
import unittest

from audit import REPO, ROOTS, baseline_manifest, code_only, functions, inspect, rejection_witness, source_inputs
from inject_warning_probe import BASE_SHA256, OBSERVER_MARKER, instrument


class ScopeAudit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = source_inputs()

    def test_all_protected_source_and_standalone_bytes_match_exact_baseline(self):
        rows = baseline_manifest()
        self.assertGreater(len(rows), 10)
        self.assertEqual([r["path"] for r in rows if not r["byte_identical"]], [])

    def test_fast_histories_are_only_builtin_close_and_global_ema(self):
        audit = inspect(self.sources, "f_fast")
        reads = {(h["owner"], h["offset"], h["scope"]) for fn in audit["reachable"] for h in fn["history"]}
        self.assertEqual(reads, {("close", "1", "global"), ("emaFast", "1", "global")})
        self.assertEqual(audit["hazards"], [])

    def test_reversal_history_is_global_rsi_only(self):
        audit = inspect(self.sources, "f_reversal")
        reads = {(h["owner"], h["offset"], h["scope"]) for fn in audit["reachable"] for h in fn["history"]}
        self.assertEqual(reads, {("rsi", "1", "global")})
        self.assertEqual(audit["hazards"], [])

    def test_transitive_helpers_are_covered_not_just_root_text(self):
        reach = {x["function"] for x in inspect(self.sources, "f_reversal")["reachable"]}
        self.assertTrue({"f_capture", "f_validPlan", "f_targetRoom", "f_makePlan", "f_planEvent", "f_emit", "f_recordDecision", "f_clear", "f_start", "f_nearest"}.issubset(reach))

    def test_mutated_transitive_stateful_builtin_is_detected(self):
        changed = dict(self.sources)
        key = "unified/src/decision.pineinc"
        changed[key] = changed[key].replace("bool accepted = f_validPlan(p)", "float unexpected = ta.ema(close, 10)\n    bool accepted = f_validPlan(p)")
        for root in ROOTS:
            self.assertIn("f_capture: historical built-in ta.ema", inspect(changed, root)["hazards"])

    def test_mutated_local_history_is_not_misclassified_as_global(self):
        changed = dict(self.sources)
        key = "unified/src/decision.pineinc"
        changed[key] = changed[key].replace("bool risingRsi = d == 1 ? rsi > rsi[1]", "float rsi = close\n    bool risingRsi = d == 1 ? rsi > rsi[1]")
        self.assertIn("f_reversal: local history rsi[1]", inspect(changed, "f_reversal")["hazards"])

    def test_parameter_history_and_persistent_loop_state_positive_control(self):
        source = {"probe": "f_probe(float source) =>\n    var float seen = 0.0\n    seen += source\n    source[1]\n"}
        result = inspect(source, "f_probe")
        self.assertIn("f_probe: parameter history source[1]", result["hazards"])
        self.assertIn("f_probe: persistent local seen", result["hazards"])

    def test_comments_and_strings_do_not_invent_dependencies(self):
        source = {"probe": 'f_probe() =>\n    // ta.sma(close, 20); ghost[1]\n    "request.security(x) ghost[1]"\n'}
        self.assertEqual(inspect(source, "f_probe")["hazards"], [])

    def test_exact_default_close_guard_and_direction_call_sequence(self):
        inputs = code_only(self.sources["unified/src/inputs.pineinc"])
        decision = code_only(self.sources["unified/src/decision.pineinc"])
        self.assertIn("bool processBar = barstate.isconfirmed", inputs)
        call_block = decision[decision.index("\nif processBar\n"):]
        self.assertIn("for directionIndex = 0 to 1", call_block)
        self.assertIn("int d = directionIndex == 0 ? 1 : -1", call_block)
        calls = re.findall(r"array\.set\(diagnostics, [012], (f_\w+)\(", call_block)
        self.assertEqual(calls, ["f_retest", "f_fast", "f_reversal"])
        for root in ROOTS:
            self.assertEqual(len(re.findall(rf"\b{root}\(", decision)), 2)  # definition + one written call

    def test_feature_computations_and_security_calls_remain_global(self):
        features = code_only(self.sources["unified/src/features.pineinc"])
        for assignment in ("float emaFast = ta.ema(close, 21)", "float rsi = ta.rsi(close, 14)", "float fastBreakHigh = ta.highest(high, fastBreakLookback)[1]", "float priorReversalLow = ta.lowest(low, reversalLiquidityBars)[1]"):
            self.assertIn("\n" + assignment, "\n" + features)
        request_lines = [line for line in features.splitlines() if "request.security(" in line]
        self.assertEqual(len(request_lines), 2)
        self.assertTrue(all(line.startswith("[") for line in request_lines))
        self.assertTrue(all("lookahead=barmerge.lookahead_on" in line for line in request_lines))
        self.assertIn("[close[1], ta.ema(close, 50)[1], ta.ema(close, 200)[1], ta.ema(close, 50)[3]]", request_lines[0])
        self.assertIn("[high[1], low[1]]", request_lines[1])

    def test_reversal_direction_state_is_two_objects_not_two_function_scopes(self):
        decision = self.sources["unified/src/decision.pineinc"]
        self.assertIn("array.push(reversalStates, Setup.new())", decision)
        self.assertIn("f_reversal(array.get(reversalStates, directionIndex), d", decision)
        body = functions(self.sources)["f_reversal"].code
        self.assertIn("f_clear(s)", body)
        self.assertIn("f_start(s, 3, d", body)
        self.assertNotRegex(body, r"\bvar(?:ip)?\s")


class AttributionAudit(unittest.TestCase):
    def test_zero_gap_news_rejection_has_inaccurate_gap_label(self):
        row = rejection_witness(news_at_open=True)
        self.assertEqual(row["price_gap"], 0)
        self.assertEqual(row["rejection_reason"], "Новинарска пауза при отваряне")
        self.assertEqual(row["native_exit_comment_if_flattened"], "ИЗХОД СЛЕД ГАП")

    def test_control_zero_gap_without_news_does_not_reject(self):
        row = rejection_witness(news_at_open=False)
        self.assertFalse(row["native_divergence_flag"])
        self.assertIsNone(row["native_exit_comment_if_flattened"])

    def test_witness_is_tied_to_actual_source_not_only_python_fixture(self):
        source = source_inputs()
        rejection = functions(source)["f_openRejection"].code
        native = source["unified/src/native_adapter.pineinc"]
        self.assertRegex(rejection, r"wrongSide\s*\?[^\n]+newsAtOpen\s*\?")
        self.assertIn('f_openRejection(nativePlan, nativeEntry, strategy.opentrades.entry_time(0)) != ""', native)
        self.assertIn('comment="ИЗХОД СЛЕД ГАП"', native)
        self.assertIn('bool newsAtOpen = manualNewsPause or (useNewsWindow', source["unified/src/paper_engine.pineinc"])

    def test_partial_exit_and_campaign_rows_are_different_record_levels(self):
        native = (REPO / "unified/src/native_adapter.pineinc").read_text(encoding="utf-8")
        self.assertIn('"NATIVE_EXIT_" + str.tostring(j)', native)
        self.assertIn("exitEvent.cashPnl := cashPart", native)
        self.assertIn("nativeCampaignCash += cashPart", native)
        self.assertIn("closedEvent.cashPnl := nativeCampaignCash", native)
        # Summing both row kinds doubles the same cash; this example is NOT market data.
        partial_cash = [9.65, -5.35, -5.35]
        campaign_cash = sum(partial_cash)
        self.assertAlmostEqual(sum(partial_cash) + campaign_cash, 2 * campaign_cash)
        self.assertAlmostEqual(campaign_cash, -1.05)


class WarningInjection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = subprocess.check_output(["git", "show", "48e5cdd:unified/QIRVANTA_UNIFIED.pine"], cwd=REPO).decode("utf-8")
        cls.appendix = '\n' + OBSERVER_MARKER + '\nint dgCutoff = timestamp("09 Nov 2026 02:45 +0000")\nvar int dgCount = 0\n// native observer sentinel\n'

    def test_injection_keeps_native_observer_and_protected_calls(self):
        output, manifest = instrument(self.base + self.appendix)
        self.assertTrue(manifest["remove_insertions_restores_input"])
        self.assertTrue(manifest["native_appendix_unchanged"])
        self.assertTrue(manifest["original_model_and_order_calls_unchanged"])
        self.assertIn(self.appendix, output)
        self.assertEqual(manifest["baseline_sha256"], BASE_SHA256)
        self.assertLess(output.index("float dgExpectedClosePrev = close[1]"), output.index("f_fast(int d"))
        for name in ("fast_long_calls", "fast_short_calls", "reversal_long_calls", "reversal_short_calls", "fast_long_mismatches", "fast_short_mismatches", "reversal_long_mismatches", "reversal_short_mismatches"):
            self.assertIn(name, output)
        self.assertIn("time_close == dgWarningCutoff", output)

    def test_injection_rejects_any_changed_baseline(self):
        with self.assertRaises(ValueError):
            instrument(self.base.replace("rsi >= 53", "rsi >= 52") + self.appendix)

    def test_injection_is_not_silently_repeatable(self):
        output, _ = instrument(self.base + self.appendix)
        with self.assertRaises(ValueError):
            instrument(output)


if __name__ == "__main__":
    unittest.main()
