from dataclasses import replace
import unittest

from gold_pro.direct_trend_breakout import CONSOLIDATION, RETEST, STRUCTURAL
from gold_pro.precision_entry import assess
from gold_pro.replay import Costs


INTERVAL = 900_000
START = 1_790_000_000_000
LIGHT_COSTS = Costs(spread=.10, commission=.05)


def fixture(side=-1, *, origin_model=STRUCTURAL, age=2, intervening=98.7, break_close=98.5):
    rows = [
        (101.0, 101.4, 100.3, 100.6),
        (101.0, 101.3, break_close - .15, break_close),
        *((99.7, 99.9, min(intervening, 99.5) - .15, intervening),) * (age - 1),
        (99.85, 100.1, 98.4, 98.5),
    ]
    if side == 1:
        rows = [(200 - o, 200 - low, 200 - high, 200 - close) for o, high, low, close in rows]
    bars = [
        dict(time=START + i * INTERVAL, time_close=START + (i + 1) * INTERVAL,
             open=opening, high=high, low=low, close=close)
        for i, (opening, high, low, close) in enumerate(rows)
    ]

    def record(i, state, model=origin_model, **extra):
        retest = model == RETEST
        return dict(
            model=model, side=side, bar=i, time_ms=bars[i]["time_close"],
            watch_ms=bars[int(retest)]["time_close"], source_ms=bars[0]["time"],
            level=100, level_known_ms=bars[0]["time_close"],
            breakout_ms=bars[1]["time_close"] if state != "WATCH" else None,
            origin_model=origin_model if retest else None, prior_atr=4, close=bars[i]["close"],
            state=state, **extra,
        )

    def geometry(i, model=origin_model):
        close = bars[i]["close"]
        return record(i, "ENTRY CANDIDATE", model,
                      plan_id=f"DT|{model}|{side}|{bars[i]['time_close']}",
                      reference=close, stop=close - side * 4, risk=4,
                      targets=tuple(close + side * 4 * step for step in (1, 2, 3)),
                      obstacle=None, reasons=[])

    events = [record(0, "WATCH"), record(1, "BREAKOUT CONFIRMED"), geometry(1),
              record(1, "RETEST WATCH", RETEST),
              record(age + 1, "BREAKOUT CONFIRMED", RETEST), geometry(age + 1, RETEST)]
    return bars, events


class PrecisionEntryEngineTest(unittest.TestCase):
    def test_known_at_prefix_does_not_use_next_open(self):
        bars, tape = fixture()
        complete = assess(bars, tape, LIGHT_COSTS)
        self.assertEqual(assess(bars[:2], tape[:4], LIGHT_COSTS), complete[:1])
        immediate, retest = complete
        self.assertTrue(immediate.accepted)
        self.assertTrue(retest.accepted)
        self.assertEqual(immediate.detection_ms, bars[0]["time_close"])
        self.assertEqual(retest.origin_detection_ms, immediate.detection_ms)
        self.assertEqual(retest.origin_confirmation_ms, immediate.confirmation_ms)
        self.assertEqual(retest.detection_ms, immediate.confirmation_ms)
        self.assertEqual(immediate.next_executable_ms, bars[2]["time"])
        self.assertEqual(immediate.expires_ms, bars[2]["time_close"])
        moved = dict(bars[2], open=99.25)
        self.assertEqual(assess([*bars[:2], moved, *bars[3:]], tape, LIGHT_COSTS), complete)

    def test_retest_requires_a_strictly_intervening_separation(self):
        for side in (-1, 1):
            with self.subTest(side=side):
                bars, tape = fixture(side, age=1)
                immediate, retest = assess(bars, tape, LIGHT_COSTS)
                self.assertTrue(immediate.accepted)
                self.assertEqual(retest.direction, "BUY" if side == 1 else "SELL")
                self.assertEqual(retest.reasons, ("PE_RETEST_TOO_EARLY", "PE_RETEST_UNESTABLISHED"))
                self.assertIsNone(retest.retest_separation_atr)
                bars, tape = fixture(side, intervening=99.4)
                _, retest = assess(bars, tape, LIGHT_COSTS)
                self.assertEqual(retest.reasons, ("PE_RETEST_UNESTABLISHED",))
                self.assertAlmostEqual(retest.retest_separation_atr, .15)
                bars, tape = fixture(side, intervening=99.0)
                _, retest = assess(bars, tape, LIGHT_COSTS)
                self.assertEqual(retest.retest_age_bars, 2)
                self.assertEqual(retest.retest_separation_atr, .25)
                self.assertTrue(retest.accepted)

    def test_model_gates_leave_rejected_geometry_available(self):
        bars, tape = fixture(break_close=96.8)
        extension, _ = assess(bars, tape, LIGHT_COSTS)
        self.assertEqual(extension.reasons, ("PE_STRUCTURAL_MAX_EXTENSION",))
        self.assertEqual(extension.plan.reference, 96.8)
        bars, tape = fixture(origin_model=CONSOLIDATION, break_close=98.5)
        weak_body, retest = assess(bars, tape, LIGHT_COSTS)
        self.assertEqual(weak_body.reasons, ("PE_CONSOLIDATION_WEAK_BODY",))
        self.assertFalse(weak_body.accepted)
        self.assertTrue(retest.accepted)

    def test_reprices_room_and_preserves_overlap_as_archived_reason(self):
        bars, tape = fixture()
        tape[2] = dict(tape[2], state="REJECTED", reasons=["OVERLAP_SAME_BAR"], obstacle=94.65)
        cheap, _ = assess(bars, tape, LIGHT_COSTS)
        stressed, _ = assess(bars, tape, replace(LIGHT_COSTS, spread=.35))
        self.assertEqual(cheap.shadow_reasons, ("OVERLAP_SAME_BAR",))
        self.assertTrue(cheap.accepted)
        self.assertEqual(stressed.risk_rejection_reason, "PE_INSUFFICIENT_NET_ROOM")
        self.assertEqual(stressed.original_reasons, ())
        self.assertEqual(stressed.plan, cheap.plan)

    def test_missing_mismatched_and_expired_shadow_lineage_is_rejected(self):
        bars, tape = fixture()
        with self.assertRaisesRegex(ValueError, "missing its immediate same-close geometry"):
            assess(bars, [*tape[:2], *tape[3:]], LIGHT_COSTS)
        wrong_id = dict(tape[2], plan_id="unrelated")
        with self.assertRaisesRegex(ValueError, "plan identity"):
            assess(bars, [*tape[:2], wrong_id, *tape[3:]], LIGHT_COSTS)
        invalidation = dict(tape[3], bar=2, time_ms=bars[2]["time_close"], state="INVALIDATED")
        with self.assertRaisesRegex(ValueError, "no active known-at origin"):
            assess(bars, [*tape[:4], invalidation, *tape[4:]], LIGHT_COSTS)
        gap = dict(bars[-1], time=bars[-1]["time"] + INTERVAL, time_close=bars[-1]["time_close"] + INTERVAL)
        delayed = [dict(event, time_ms=gap["time_close"]) for event in tape[-2:]]
        with self.assertRaisesRegex(ValueError, "contiguous shadow lifecycle"):
            assess([*bars[:-1], gap], [*tape[:4], *delayed], LIGHT_COSTS)


if __name__ == "__main__":
    unittest.main()
