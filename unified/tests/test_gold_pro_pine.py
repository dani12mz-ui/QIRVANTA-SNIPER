"""Keep the canonical Gold Pro observations identical across entry points."""

import re
import unittest

from unified.tools import build_gold_pro


class GoldProGenerationTests(unittest.TestCase):
    def test_both_deliverables_match_the_same_uncapped_context_and_models(self):
        shared = build_gold_pro.shared_source().encode()
        for kind, path in build_gold_pro.TARGETS.items():
            with self.subTest(kind=kind):
                source = path.read_bytes()
                self.assertEqual(source, build_gold_pro.build(kind))
                self.assertEqual(source.count(shared), 1)
                self.assertNotIn(b"calc_bars_count", source)
                self.assertEqual(len(re.findall(rb"\brequest\.security\(", source)), 3)
                self.assertIn(b"else if not gpContextReady\n        plan.reason := \"HTF_UNKNOWN\"", source)

    def test_observation_has_no_orders_and_strategy_is_explicitly_opt_in(self):
        indicator = build_gold_pro.build("indicator")
        strategy = build_gold_pro.build("strategy")
        self.assertIsNone(re.search(rb"\bstrategy\.\w+\(", indicator))
        self.assertIn(b'bool gpSimEnabled = input.bool(false, "Enable experimental TradingView simulation"', strategy)
        self.assertIn(b"if gpClosedBar and gpSimEnabled", strategy)
        self.assertIn(b"process_orders_on_close=false", strategy)
        self.assertIn(b"backtest_fill_limits_assumption=1", strategy)
        for order in re.findall(rb"strategy\.(?:entry|exit|close)\(.*?disable_alert\s*=\s*(\w+)", strategy, flags=re.S):
            self.assertEqual(order, b"true")
        self.assertEqual(len(re.findall(rb"strategy\.(?:entry|exit|close)\(", strategy)), 5)


if __name__ == "__main__":
    unittest.main()
