"""Regression checks for the final GOLD PRO Pine presentation."""

import unittest

from unified.tools import build_gold_final_ux as final, build_gold_precision as precision


class FinalGoldUxTests(unittest.TestCase):
    def test_generated_files_preserve_the_pinned_engine_and_native_properties(self):
        for kind, path in final.TARGETS.items():
            with self.subTest(kind=kind):
                previous = precision.TARGETS[kind].read_bytes()
                self.assertEqual(precision.digest(previous), final.PRECISION_SHA[kind])
                candidate = path.read_bytes()
                self.assertEqual(candidate, final.build(kind))
                engine = precision.pinned_engine(kind).encode()
                self.assertIn(b"\n\n" + engine + b"\n\n", candidate)
                self.assertEqual(candidate.count(engine), 1)
                original_title, original_short = precision.TITLES[kind]
                title, short = final.TITLES[kind]
                declaration = final.declaration(kind)
                self.assertIn("behind_chart=false", declaration)
                declaration = declaration.replace(title, original_title, 1).replace(short, original_short, 1)
                if kind == "indicator":
                    declaration = declaration.replace(" behind_chart=false,", "", 1)
                self.assertEqual(declaration, precision.declaration(kind))
                visual = candidate.split(engine, 1)[1].decode()
                self.assertNotRegex(visual, r"\b(?:gp|v22)\w+(?:\.\w+)?\s*:=")
                self.assertNotRegex(visual, r"\bstrategy\.(?:entry|exit|order|close|close_all|cancel|cancel_all|risk)\b")
                self.assertNotRegex(visual, r"\b(?:alert|alertcondition|f_gpEvent|log\.info)\s*\(")
                self.assertNotIn("request.security", visual)

    def test_readiness_precedes_context_and_separates_native_position_from_a_new_plan(self):
        dashboard = final.fragment("dashboard.pineinc")
        parts = ("ACTIONABLE SETUP", "MARKET BIAS", "OPPORTUNITY STATE", "TRADE STATUS", "LAST CLOSED RESULT")
        self.assertEqual([dashboard.index(x) for x in parts], sorted(dashboard.index(x) for x in parts))
        self.assertIn("bool prActionable = prEligiblePlan and not prNativeActive and not prSimMode", dashboard)
        self.assertIn("gpLastPlan.knownAt == time_close", dashboard)
        self.assertIn('prNativeActive ? (na(prViewFill) ? "ORDER PENDING" : "POSITION OPEN")', dashboard)
        self.assertIn('prEligiblePlan and prSimMode ?', dashboard)
        self.assertIn('opportunity == "EXPIRED" ? "EXPIRED" : "WAIT"', dashboard)
        self.assertIn('gpOpportunityText == "CONFLICT" and not prConflictNow ? "WAIT"', dashboard)

    def test_default_keeps_native_fills_without_duplicate_pine_arrows(self):
        dashboard = final.fragment("dashboard.pineinc")
        markers = final.fragment("markers.pineinc")
        self.assertIn('input.string("PROFESSIONAL", "Dashboard preset"', dashboard)
        self.assertIn('input.bool(false, "Latest closed campaign levels"', dashboard)
        self.assertIn('input.bool(false, "Extra Pine fill badges in FULL AUDIT"', dashboard)
        self.assertIn("if prNativeFillMark\n        if prFull and prShowFills", markers)
        self.assertIn('glyph := side > 0 ? "SIM B" : "SIM S"', markers)
        self.assertIn("if gpClosedBar and prMarkerLimit > 0", markers)
        self.assertIn("label.set_xy(marker, bar_index, markerPrice)", markers)
        self.assertNotIn("shape.arrow", markers)

    def test_confirmed_and_watch_markers_take_direction_from_their_own_events(self):
        markers = final.fragment("markers.pineinc")
        self.assertIn(
            "else if (gpBuyConfirmed or gpSellConfirmed) and not prSignalConflict\n"
            "        side := gpBuyConfirmed ? 1 : -1",
            markers,
        )
        self.assertIn(
            "else if prShowWatches and (prBuyWatching or prSellWatching) and not prMinimal\n"
            "        side := prBuyWatching and not prSellWatching ? 1 : prSellWatching and not prBuyWatching ? -1 : 0",
            markers,
        )

    def test_tags_keep_level_coordinates_and_do_not_extend_closed_plan(self):
        geometry = final.fragment("geometry.pineinc")
        self.assertIn("style=label.style_label_right", geometry)
        self.assertIn("visible and tag ? price : na", geometry)
        self.assertIn("showClosed ? prViewClosedAt : bar_index + 1", geometry)
        self.assertIn('prFull ? caption + " " + f_prPrice(price) : caption', geometry)
        self.assertIn("f_prFar(targetThree, targetTwoTag ? targetTwo : na, minSpacing)", geometry)


if __name__ == "__main__":
    unittest.main()
