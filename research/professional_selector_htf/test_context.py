"""Check known-time availability and distinguish a missing context from MIXED."""

from decimal import Decimal
from unittest import TestCase

from research.professional_selector_htf.native_qa import cross_frame, known_side, source_qa


class ConfirmedContextTests(TestCase):
    def test_only_completed_and_fresh_source_can_be_classified(self):
        value = (10_000_000, Decimal("4000"), Decimal("3990"), Decimal("3980"))
        self.assertIsNone(known_side(value, value[0] - 1, 7_200_000))
        self.assertEqual(known_side(value, value[0] + 7_200_000, 7_200_000), 1)
        self.assertIsNone(known_side(value, value[0] + 7_200_001, 7_200_000))

    def test_same_close_with_missing_ema_is_unknown(self):
        start = 10_000_000
        self.assertEqual(known_side((start, Decimal("4000"), Decimal("4001"), Decimal("3990")),
                                    start, 7_200_000), 0)
        self.assertIsNone(known_side((start, Decimal("4000"), None, Decimal("3990")), start, 7_200_000))

    def test_same_clock_does_not_prove_same_source_values(self):
        stamp = 10_000_000
        a = (stamp, Decimal("4000"), Decimal("3990"), Decimal("3980"), 1)
        b = (stamp, Decimal("4000"), Decimal("4010"), Decimal("4020"), -1)
        contexts = {5: {stamp: {"canonical": [a, a]}}, 15: {stamp: {"canonical": [a, b]}}, 30: {}}
        with self.assertRaisesRegex(ValueError, "same-source H4 difference"):
            cross_frame(contexts)

    def test_original_pine_and_seal_are_pinned(self):
        self.assertFalse(source_qa()["protected_source_changed"])
