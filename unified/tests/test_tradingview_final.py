"""Static release invariants. Native TradingView evidence is reported separately."""
import hashlib
from pathlib import Path
import re
import sys
import unittest

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
from build_tradingview_final import OUTPUT, assemble, champion_bytes
from observe_tradingview_final import observed_buffer


class TradingViewFinalContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.champion = champion_bytes().decode("utf-8")
        cls.final = OUTPUT.read_text(encoding="utf-8")

    def test_generated_bytes_and_single_strategy(self):
        self.assertEqual(OUTPUT.read_bytes(), assemble(champion_bytes()))
        self.assertEqual(len(re.findall(r"^strategy\(", self.final, re.M)), 1)
        self.assertNotRegex(self.final, r"(?m)^\s*import ")
        self.assertEqual(hashlib.sha256(champion_bytes()).hexdigest(),
                         "92307768be68946dc87cbe4ea45a8b5e32446fd3b7a0c1a38dce40dffe358be3")

    def test_original_setup_functions_remain_verbatim(self):
        marker = "f_retest(Setup"
        original = self.champion.split(marker)[1].split("\nif processBar\n")[0]
        candidate = self.final.split(marker)[1].split("\nif processBar and (tvStrategyMode")[0]
        self.assertEqual(original, candidate)
        self.assertIn('input.string("S0", "Стратегия за независим тест"', self.final)
        self.assertIn('input.bool(false, "Изследване на вход и изход"', self.final)

    def test_confirmed_htf_uses_offsets_and_lookahead_together(self):
        requests = [line for line in self.final.splitlines() if "request.security(" in line]
        self.assertEqual(len(requests), 4)
        for line in requests:
            self.assertIn("[1]", line)
            self.assertIn("lookahead=barmerge.lookahead_on", line)
        self.assertIn("if tfSeconds < 3600", self.final)
        self.assertIn("if tfSeconds < 14400", self.final)

    def test_marker_and_fill_alerts_are_distinct(self):
        self.assertIn('e.book == "NATIVE" and e.kind == "NATIVE_SUBMITTED"', self.final)
        self.assertIn('bool nativeFillObservation = e.kind == "NATIVE_ENTRY" or str.startswith(e.kind, "NATIVE_EXIT_")', self.final)
        self.assertIn("alertsEnabled and not nativeFillObservation", self.final)
        self.assertIn("alert.freq_once_per_bar_close", self.final)
        self.assertIn("{{strategy.order.alert_message}}", self.final)
        self.assertNotRegex(self.final, r"plotshape\([^\n]*offset\s*=")

    def test_diagnostic_trimming_preserves_order_calls(self):
        source = OUTPUT.read_bytes()
        trace = observed_buffer(source, None, candidate=True).decode("utf-8")
        orders = lambda code: [line.strip() for line in code.splitlines()
                               if re.search(r"\bstrategy\.(entry|exit|close|cancel)\(", line)]
        self.assertEqual(orders(self.final), orders(trace))
        self.assertIn("QIRVANTA_DATA_BEGIN", trace)
        self.assertNotIn("table.new(", trace)
        self.assertIn(hashlib.sha256(source).hexdigest(), trace)


if __name__ == "__main__":
    unittest.main()
