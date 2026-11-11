"""Regression checks for the disposable WSB comparison probes."""

import difflib
import re
import unittest

from unified.tools import build_wsb_forensic_trace as trace


OBSERVER = "// Add-only closed-bar forensic journal."


class WsbForensicTraceTest(unittest.TestCase):
    def test_default_probe_changes_only_title_and_export_inputs(self):
        for kind, (path, _, _) in trace.SOURCES.items():
            with self.subTest(kind=kind):
                old = (trace.ROOT / path).read_text(encoding="utf-8").rstrip()
                new, observer = trace.build(kind).split(OBSERVER, 1)
                matcher = difflib.SequenceMatcher(a=old.splitlines(), b=new.rstrip().splitlines())
                changes = [old.splitlines()[first] for tag, first, last, _, _ in matcher.get_opcodes()
                           if tag == "replace" for first in range(first, last)]
                self.assertEqual(len(changes), 4)
                self.assertRegex(changes[0], r"^(strategy|indicator)\(")
                self.assertTrue(all("input." in line for line in changes[1:]))
                self.assertIn('timestamp("11 Nov 2026 09:00 +0000")', new)
                self.assertIn('timestamp("11 Nov 2026 16:00 +0000")', new)
                self.assertRegex(observer, r"(?m)^if (rcQaBar|wfAuditBar)$")
                self.assertNotRegex(observer, r"\b(strategy\.(entry|exit|order|close|cancel)|alert)\(")

    def test_named_fields_and_distinct_sources(self):
        for kind in trace.SOURCES:
            observer = trace.build(kind).split(OBSERVER, 1)[1]
            with self.subTest(kind=kind):
                fields = re.findall(r"""wsbRecord \+= ',"([a-z0-9_]+)":'""", observer)
                self.assertTrue(set(trace.COMMON) <= set(fields))
                self.assertEqual(len(fields), len(set(fields)))
                self.assertIn(f'"kind":"{kind}"', observer)
                self.assertIn('log.info("WSB_FORENSIC|"', observer)
                if kind in {"s5", "rc2-audit"}:
                    self.assertIn("native_admitted_now", fields)
                if kind == "rc2-indicator":
                    self.assertNotIn("native_admitted_now", fields)
                    self.assertIn("decision_state_at_close", fields)
                    self.assertIn("wick_active_known_ms", fields)

    def test_wick_simulator_and_chart_cap_are_explicit(self):
        default = trace.build("wick")
        probe = trace.build("wick", "2026-11-11T13:00:00Z", "2026-11-11T14:00:00+00:00",
                            wick_sim=True, cap_wick_1200=True)
        self.assertIn("bool wfSim = input.bool(false,", default)
        self.assertNotIn("calc_bars_count=1200", default)
        self.assertIn("bool wfSim = input.bool(true,", probe)
        self.assertIn('timestamp("11 Nov 2026 13:00 +0000"), "Simulation start"', probe)
        self.assertIn('timestamp("11 Nov 2026 14:00 +0000"), "Simulation end"', probe)
        self.assertIn("calc_bars_count=1200", probe)
        with self.assertRaisesRegex(ValueError, "require the wick"):
            trace.build("s5", wick_sim=True)

    def test_invalid_or_inverted_timestamps_are_rejected(self):
        for stamp in ("2026-11-11T09:00", "2026-11-11T09:00+02:00", "2026-11-11T09:00:15Z"):
            with self.subTest(stamp=stamp), self.assertRaisesRegex(ValueError, "Expected"):
                trace.build("s5", start=stamp)
        with self.assertRaisesRegex(ValueError, "start must precede end"):
            trace.build("s5", start=trace.DEFAULT_END, end=trace.DEFAULT_START)

    def test_bar_cap_is_explicit_and_applies_to_all_roles(self):
        for kind in trace.SOURCES:
            with self.subTest(kind=kind):
                self.assertIn("calc_bars_count=1201", trace.build(kind, bar_cap=1201))
        with self.assertRaisesRegex(ValueError, "only one chart-cap"):
            trace.build("wick", cap_wick_1200=True, bar_cap=1201)
        with self.assertRaisesRegex(ValueError, "cap must be positive"):
            trace.build("s5", bar_cap=0)


if __name__ == "__main__":
    unittest.main()
