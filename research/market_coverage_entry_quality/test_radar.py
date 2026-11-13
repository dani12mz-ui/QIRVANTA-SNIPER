import unittest

from gold_pro.direct_trend_breakout import CONSOLIDATION, RETEST, STRUCTURAL

from .radar import HOUR, MINUTE, clock, detection, lifecycle, warning_clocks


def event(stamp, state, model=STRUCTURAL, side=1, origin=None, source=None, level=4000.):
    return {
        "time_ms": stamp, "state": state, "model": model, "side": side, "reason": state,
        "origin_model": STRUCTURAL if model == RETEST else None, "source_ms": source or MINUTE,
        "level": level, "level_known_ms": MINUTE, "breakout_ms": origin,
    }


class RadarTest(unittest.TestCase):
    def test_rejection_leaves_retest_active_and_no_extra_warning(self):
        stamps = [5 * MINUTE, 10 * MINUTE, 15 * MINUTE, 20 * MINUTE]
        bars = [{"time_close": stamp} for stamp in stamps]
        first, second, third, fourth = stamps
        events = [
            event(first, "WATCH"),
            event(first, "BREAKOUT CONFIRMED", origin=first),
            event(first, "REJECTED", origin=first),
            event(first, "RETEST WATCH", RETEST, origin=first),
            event(second, "BREAKOUT CONFIRMED", CONSOLIDATION, origin=second, source=first),
            event(second, "REJECTED", CONSOLIDATION, origin=second, source=first),
            event(third, "INVALIDATED", RETEST, origin=first),
            event(third, "BREAKOUT CONFIRMED", CONSOLIDATION, origin=third, source=second),
            event(third, "RETEST WATCH", RETEST, origin=third, source=second) | {"origin_model": CONSOLIDATION},
            event(fourth, "BREAKOUT CONFIRMED", RETEST, origin=third, source=second) | {"origin_model": CONSOLIDATION},
        ]
        snapshots, marked, _ = lifecycle(bars, events)
        self.assertEqual(snapshots[first], snapshots[second])
        self.assertEqual(snapshots[fourth], {})
        self.assertEqual(set(marked), {clock(events[4]), clock(events[-1])})
        warnings = warning_clocks([{"known_ms": first, "side": 1}], events, first, fourth)
        self.assertEqual(len(warnings), 4)
        self.assertIn("RC2 EARLY", warnings[0]["sources"])
        self.assertEqual(lifecycle(bars[:2], events[:6])[:2],
                         ({t: snapshots[t] for t in stamps[:2]}, {clock(events[4]): marked[clock(events[4])]}))

    def test_clock_assigns_same_side_event_to_own_hour_only(self):
        events = [event(55 * MINUTE, "WATCH"), event(HOUR + 5 * MINUTE, "BREAKOUT CONFIRMED"),
                  event(HOUR + 10 * MINUTE, "WATCH", side=-1)]
        hours = [{"start_ms": 0, "side": 1}, {"start_ms": HOUR, "side": 1}]
        warnings = warning_clocks([], events, 0, 2 * HOUR)
        matches, summary = detection(warnings, hours, {0: "DIRECTIONAL_BUY", HOUR: "DIRECTIONAL_BUY"})
        self.assertEqual(matches[0]["lead_minutes"], 55)
        self.assertIsNone(matches[HOUR]["first_pre_hour"])
        self.assertEqual(matches[HOUR]["lead_minutes"], 5)
        self.assertEqual(summary["outside_eligible_same_side_windows"], 1)


if __name__ == "__main__":
    unittest.main()
