import unittest

from research.net_campaign_evidence.cooldown_open_checks import fill_lookback


class ActualFillClockTests(unittest.TestCase):
    def setUp(self):
        self.bars = [{"index": i, "close_ms": i + 1, "open_ms": i, "min_cooldown": 8} for i in range(11)]
        self.market = {row["close_ms"]: {"time": row["open_ms"]} for row in self.bars}

    def check(self, at_index, *, side=1, logged=True):
        stamp = at_index + 1
        ledger = [{"known_ms": row["close_ms"], "entered": int(row["close_ms"] >= stamp), "phase": 0}
                  for row in self.bars]
        event = {"event": "SIM_ENTRY", "event_id": "fill", "plan_id": "p", "known_time": stamp,
                 "effective_time": at_index, "side": side}
        return fill_lookback({"bars": self.bars}, (ledger, [event] if logged else []), 11, 1, self.market)

    def test_eight_bars_does_not_clear_cooldown(self):
        self.assertEqual(self.check(2)["status"], "FAIL_SAME_SIDE_FILL_WITHIN_COOLDOWN")

    def test_nine_bars_and_opposite_side_clear_cooldown(self):
        self.assertEqual(self.check(1)["status"], "PASS_BY_COMPLETE_LOOKBACK")
        self.assertEqual(self.check(2, side=-1)["status"], "PASS_BY_COMPLETE_LOOKBACK")

    def test_fill_known_on_decision_close_is_not_available_to_candidate(self):
        self.assertEqual(self.check(10)["status"], "PASS_BY_COMPLETE_LOOKBACK")

    def test_missing_fill_event_does_not_clear_cooldown(self):
        self.assertEqual(self.check(2, logged=False)["status"], "UNKNOWN_RAW_FILL_LEDGER_MISMATCH")


if __name__ == "__main__":
    unittest.main()
