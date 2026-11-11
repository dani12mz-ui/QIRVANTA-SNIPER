"""Exercise causal boundaries in the FAST campaign diagnosis."""

import unittest
from types import SimpleNamespace

from gold_precision.diagnose import _invalidation, _rejected_audit
from gold_pro.replay import Outcome, Part, Plan
from unified.tools.evaluate_v21_impulse import HOUR


class DiagnosticTest(unittest.TestCase):
    def test_invalidation_requires_a_completed_bar_before_first_stop(self):
        bars = [{"time": i * 300_000, "time_close": (i + 1) * 300_000, "close": 100.}
                for i in range(7)]
        plan = Plan("a", "BREAKOUT", 1, 0, 101., 98., (104., 107., 110.), 2., 48)
        stop = Part(5, 98., 1., "STOP", .35, -3.35)
        outcome = Outcome(plan, "CLOSED", 5, 1, 101., (stop,))
        features = [SimpleNamespace(bos=0, spike=False) for _ in bars]
        protocol = {"anchor_failure_atr": .1}

        features[5].bos = -1
        bars[5]["close"] = 97.
        after_stop = _invalidation(outcome, 100., bars, features, protocol)
        self.assertIsNone(after_stop["first_thesis_invalidation_close_utc"])
        self.assertFalse(after_stop["invalidation_before_original_stop"])

        bars[3]["close"] = 99.5
        before_stop = _invalidation(outcome, 100., bars, features, protocol)
        self.assertEqual(2, before_stop["known_bars_before_original_stop"])
        self.assertTrue(before_stop["next_open_precedes_stop_bar"])

    def test_rejected_excursion_excludes_rejection_bar_and_other_hours(self):
        bars = [{"time": i * 300_000, "time_close": (i + 1) * 300_000,
                 "open": 100., "high": 100.25, "low": 99.9}
                for i in range(13)]
        bars[2]["high"] = bars[12]["high"] = 110.
        features = [SimpleNamespace(atr=1.) for _ in bars]
        reject = {"state": "RISK_REJECTED", "reason": "HTF_CONFLICT",
                  "all_reasons": ["HTF_CONFLICT"], "side": 1, "bar": 2,
                  "time_ms": bars[2]["time_close"], "plan_id": "a", "model": "BREAKOUT"}
        independent = [{"start": 0, "end": HOUR, "side": 1, "last_bar": 11}]
        recorded = [{"time_ms": 0, "side": 1, "filled": False}]
        protocol = {"rejected_event_excursion_atr": 1.}
        without = _rejected_audit([reject], [], bars, features, independent, recorded, protocol)
        self.assertEqual(0, without["risk"]["later_event_excursion_at_least_1_atr"])

        bars[4]["high"] = 101.5
        with_later = _rejected_audit([reject], [], bars, features, independent, recorded, protocol)
        self.assertEqual(1, with_later["risk"]["later_event_excursion_at_least_1_atr"])
        self.assertFalse(with_later["large_moves"]["rejection_examples"][0]["event_had_same_side_fast_fill"])


if __name__ == "__main__":
    unittest.main()
