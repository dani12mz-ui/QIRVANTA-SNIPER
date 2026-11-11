"""Verify that the Precision presentation uses the exact original Gold Pro engine."""

import re
import unittest

from unified.tools import build_gold_precision, build_gold_pro


class PrecisionPineTests(unittest.TestCase):
    def test_generated_files_use_the_pinned_engine(self):
        for kind, path in build_gold_precision.TARGETS.items():
            with self.subTest(kind=kind):
                original = build_gold_pro.TARGETS[kind].read_bytes()
                self.assertEqual(build_gold_precision.digest(original), build_gold_precision.BASELINE_SHA[kind])
                new = path.read_bytes()
                engine = build_gold_precision.pinned_engine(kind).encode()
                self.assertEqual(new, build_gold_precision.build(kind))
                self.assertEqual(new.count(engine), 1)
                self.assertIn(b"\n\n" + engine + b"\n\n", new)
                self.assertEqual(original.count(engine), 1)

    def test_declaration_changes_only_titles_and_rendering_limits(self):
        for kind, original in build_gold_pro.DECLARATIONS.items():
            with self.subTest(kind=kind):
                title, short = build_gold_precision.TITLES[kind]
                declared = build_gold_precision.declaration(kind)
                self.assertIn(f'"{title}", shorttitle="{short}"', declared)
                original_settings = original.split('", overlay=', 1)[1]
                precision_settings = declared.split('", overlay=', 1)[1]
                self.assertEqual(
                    precision_settings.removesuffix(build_gold_precision.LAYOUT_LIMITS + ")") + ")",
                    original_settings,
                )

    def test_view_has_read_only_access_to_signal_and_native_execution_state(self):
        for kind in build_gold_precision.TARGETS:
            with self.subTest(kind=kind):
                new = build_gold_precision.build(kind).decode()
                visual = new.split(build_gold_precision.pinned_engine(kind), 1)[1]
                self.assertNotRegex(visual, r"\b(?:gp|v22)\w+(?:\.\w+)?\s*:=")
                self.assertNotRegex(visual, r"\bstrategy\.(?:entry|exit|order|close|close_all|cancel|cancel_all|risk)\b")
                self.assertNotRegex(visual, r"\b(?:alert|alertcondition|f_gpEvent|log\.info)\s*\(")
                self.assertNotIn("request.security", visual)
                self.assertEqual(len(re.findall(r"\brequest\.security\(", new)), 3)

    def test_target_completion_requires_matching_native_exit_reason(self):
        indicator = build_gold_precision.fragment("indicator_view.pineinc")
        strategy = build_gold_precision.fragment("strategy_view.pineinc")
        for number, target in enumerate(("One", "Two", "Three"), 1):
            self.assertIn(f"bool prHit{target} = false", indicator)
            self.assertIn(f'exitId == entryId + "|{number}" and exitReason == "TP{number}"', strategy)
            self.assertIn(f"bool prHit{target} = prLedgerForPlan and prTarget{target}", strategy)

    def test_markers_use_price_coordinates_on_the_confirmed_candle(self):
        markers = build_gold_precision.fragment("markers.pineinc")
        self.assertIn("if gpClosedBar and prMarkerLimit > 0", markers)
        self.assertIn("float markerPrice = buy ? low - padding : high + padding", markers)
        self.assertIn("label.new(bar_index, markerPrice, yloc=yloc.price", markers)
        self.assertIn("label.set_xy(marker, bar_index, markerPrice)", markers)
        self.assertNotRegex(markers, r"yloc\.(?:abovebar|belowbar)")


if __name__ == "__main__":
    unittest.main()
