import copy
import json
import unittest

from unified.tools.audit_premium_parity import comparison
from unified.tools.build_sniper_premium import OUTPUT, PRESENTATION, ROOT, build, verify_source


class PremiumRelease(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = OUTPUT.read_bytes()
        cls.archive = json.loads((ROOT / "docs/platform_evidence/ui_parity.audit.json").read_bytes())

    def test_generated_release_is_current_and_self_contained(self):
        self.assertEqual(self.source, build())
        manifest = verify_source(self.source)
        self.assertEqual(manifest["scope"], "STATIC_SOURCE_GUARD_NOT_NATIVE_PARITY")
        self.assertNotIn(b"\nimport ", self.source)

    def test_guard_rejects_financial_change_outside_display(self):
        changed = self.source.replace(b"commission_value=0.175", b"commission_value=0.150", 1)
        with self.assertRaisesRegex(ValueError, "Engine or alert bytes"):
            verify_source(changed)

    def test_guard_rejects_order_in_display_even_when_engine_is_unchanged(self):
        changed = self.source.replace(PRESENTATION, PRESENTATION + b"strategy.close_all()\n", 1)
        with self.assertRaisesRegex(ValueError, "Presentation may not submit orders"):
            verify_source(changed)

    def test_parity_compares_the_entry_not_just_equal_aggregate_performance(self):
        changed = copy.deepcopy(self.archive)
        changed["campaigns"][0]["entry_bar_index"] += 1
        self.assertEqual(comparison(self.archive, changed), ["campaigns"])

    def test_parity_rejects_the_same_result_on_different_bars(self):
        changed = copy.deepcopy(self.archive)
        changed["run"]["checksum2"] += 1
        self.assertEqual(comparison(self.archive, changed), ["run.checksum2"])


if __name__ == "__main__":
    unittest.main()
