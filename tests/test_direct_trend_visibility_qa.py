import unittest

from gold_pro.rc1_qa import EvidenceError
from research.direct_trend_visibility import native_qa as qa


CLOSE = qa.ms("2026-11-11T12:00:00Z")
FRAME = 15


def observation(source, model, side, stage, *, known=CLOSE, expires=CLOSE + 900_000, research=False):
    return dict(source=source, model=model, side=side, stage=stage, known_ms=known,
                expires_ms=expires, reason="Confirmed source record", research=research)


def permission(state, source="S5 · RC2", visible=False, **extra):
    return dict(permission=state, permission_source=source, permission_known_ms=CLOSE,
                permission_reason="Known at close", plan_visible=visible, **extra)


class DirectTrendVisibilityNativeQaTest(unittest.TestCase):
    def test_opposing_s5_watch_and_shadow_confirmation_are_independent(self):
        sell = observation("DT STRUCTURE", "STRUCTURAL_BREAK", -1, "BREAKOUT CONFIRMED")
        buy = observation("S5", "S5_RECLAIM", 1, "WATCH")
        keys = [qa.signal_keys(signal, CLOSE) for signal in (sell, buy)]
        self.assertEqual(qa.direction_from_signals(keys), "CONFLICT")
        self.assertEqual(qa.direction_from_signals(keys[:1]), "SELL BREAKOUT CONFIRMED")
        self.assertEqual(qa.direction_from_signals(keys[1:]), "BUY WATCH")
        self.assertEqual(qa.direction_from_signals([]), "NONE")
        qa.verify_permissions(permission("WAIT"), CLOSE, set())

    def test_reject_future_expired_or_unproven_shadow_signal(self):
        confirmed = observation("DT RETEST", "RETEST_CONTINUATION", -1, "BREAKOUT CONFIRMED")
        key = qa.signal_keys(confirmed, CLOSE)
        events = {(CLOSE, -1): [{"model": "STRUCTURAL_BREAK", "state": "BREAKOUT CONFIRMED"}]}
        with self.assertRaisesRegex(EvidenceError, "lacks native event"):
            qa.require_shadow_origin(confirmed, key, events)
        retest_events = {(CLOSE, -1): [
            {"model": "RETEST_CONTINUATION", "state": "BREAKOUT CONFIRMED"},
            {"model": "RETEST_CONTINUATION", "state": "ENTRY CANDIDATE"},
        ]}
        with self.assertRaisesRegex(EvidenceError, "RESEARCH marker"):
            qa.require_shadow_origin(confirmed, key, retest_events)
        qa.require_shadow_origin(dict(confirmed, research=True), key, retest_events)
        with self.assertRaisesRegex(EvidenceError, "future or stale"):
            qa.signal_keys(dict(confirmed, known_ms=CLOSE + 1), CLOSE)
        with self.assertRaisesRegex(EvidenceError, "future or stale"):
            qa.signal_keys(dict(confirmed, known_ms=CLOSE - 2, expires_ms=CLOSE - 1), CLOSE)
        with self.assertRaisesRegex(EvidenceError, "future or stale"):
            qa.signal_keys(dict(confirmed, expires_ms=CLOSE), CLOSE)

    def test_original_linked_retest_invalidation_removes_confirmed_shadow(self):
        model = "STRUCTURAL_BREAK"
        signal = observation("DT STRUCTURE", model, -1, "BREAKOUT CONFIRMED", expires=CLOSE + 7 * 900_000)
        key = qa.signal_keys(signal, CLOSE)
        common = dict(side=-1, time_ms=CLOSE, watch_ms=CLOSE, breakout_ms=CLOSE, origin_model=model)
        events = [
            dict(common, state="BREAKOUT CONFIRMED", model=model),
            dict(common, state="RETEST WATCH", model="RETEST_CONTINUATION"),
            dict(common, state="INVALIDATED", model="RETEST_CONTINUATION", time_ms=CLOSE + 900_000),
        ]
        at_close, terminal = qa.shadow_transitions(events)
        qa.require_shadow_origin(signal, key, at_close, terminal, CLOSE, FRAME)
        with self.assertRaisesRegex(EvidenceError, "linked retest ended"):
            qa.require_shadow_origin(signal, key, at_close, terminal, CLOSE + 900_000, FRAME)

    def test_ready_and_occupancy_require_original_execution_evidence(self):
        with self.assertRaisesRegex(EvidenceError, "promoted"):
            qa.verify_permissions(permission("SELL READY", "DT SHADOW"), CLOSE, set())
        with self.assertRaisesRegex(EvidenceError, "without an actual READY"):
            qa.verify_permissions(permission("WAIT", visible=True), CLOSE, set())
        with self.assertRaisesRegex(EvidenceError, "no matching native S5 READY"):
            qa.verify_permissions(permission("SELL READY", visible=True), CLOSE, {(CLOSE, 1)})
        qa.verify_permissions(permission("BUY READY", visible=True), CLOSE, {(CLOSE, 1)})
        with self.assertRaisesRegex(EvidenceError, "no observed"):
            qa.verify_permissions(permission("POSITION OCCUPIED", "S5 RAW · PAPER", paper_phase=0), CLOSE, set())
        qa.verify_permissions(permission("POSITION OCCUPIED", "S5 RAW · PAPER", paper_phase=2), CLOSE, set())

    def test_historical_review_freezes_the_exact_known_snapshot(self):
        close_state = dict(direction="NONE", signals=[], **permission("WAIT"))
        review = dict(close_state, known_ms=CLOSE, as_of_ms=CLOSE, review=True)
        self.assertEqual(qa.review_evidence([review], {CLOSE: close_state})["status"], "MATCH")
        with self.assertRaisesRegex(EvidenceError, "differs from the state"):
            qa.review_evidence([dict(review, direction="SELL WATCH")], {CLOSE: close_state})

    def test_observational_alert_is_confirmed_separate_and_has_no_trade_fields(self):
        rows = [dict(symbol=qa.rc1.SYMBOL, frame=FRAME, side="SELL", known_ms=CLOSE,
                     source="DT SHADOW", observation_only=True)]
        states = {CLOSE: {}}
        events = {(CLOSE, -1): [{"state": "BREAKOUT CONFIRMED"}]}
        self.assertEqual(qa.alert_evidence(rows, FRAME, states, events)["status"], "INTENT_VERIFIED")
        with self.assertRaisesRegex(EvidenceError, "duplicate"):
            qa.alert_evidence(rows * 2, FRAME, states, events)
        with self.assertRaisesRegex(EvidenceError, "executable"):
            qa.alert_evidence([dict(rows[0], entry=4038.78)], FRAME, states, events)


if __name__ == "__main__":
    unittest.main()
