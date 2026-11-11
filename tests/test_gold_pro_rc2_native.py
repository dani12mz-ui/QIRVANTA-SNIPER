"""RC2 native order, stop-policy, and audit-event regression checks."""

import csv
from dataclasses import replace
from decimal import Decimal
import io
import json
import re
import sys
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from gold_pro.rc1_qa import Context, EvidenceError, Export
from gold_pro.rc2_qa import MODES, NATIVE_SCHEMA, batches, fresh_comparison, native_expected, shared
from unified.tools import build_gold_pro_rc1 as rc1

with patch.dict(sys.modules, {"build_gold_pro_rc1": rc1}):
    from unified.tools import build_gold_pro_rc2 as rc2


ORDER = re.compile(r"^\s*strategy\.(entry|order|exit|close|close_all|cancel|cancel_all)\(", re.M)
POSTLUDE = "// Read-only audit postlude."


def order_calls(source):
    return [line.strip() for line in source.splitlines() if ORDER.match(line)]


class Rc2NativeSourceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        files = rc2.assemble()
        cls.indicator = files["QIRVANTA_GOLD_PRO_RC2.pine"]
        cls.audit = files["QIRVANTA_GOLD_PRO_RC2_AUDIT.pine"]
        cls.audit_adapter, cls.audit_observer = cls.audit.split(POSTLUDE, 1)
        cls.reference = rc1.pinned(rc1.RELEASE / "QIRVANTA_GOLD_PRO_RC1_AUDIT.pine", rc2.RC1_AUDIT)

    def test_order_commands_and_execution_properties_match_rc1(self):
        reference_calls = order_calls(self.reference)
        self.assertTrue(reference_calls)
        self.assertEqual(order_calls(self.audit_adapter), reference_calls)
        self.assertEqual(order_calls(self.audit_observer), [])
        self.assertEqual(order_calls(self.indicator), [])
        self.assertNotIn("RC2_NATIVE_ALERT|", self.indicator)

        def properties(source):
            declaration = next(line for line in source.splitlines() if line.startswith("strategy("))
            return declaration.split("overlay=", 1)[1]

        self.assertEqual(properties(self.audit), properties(self.reference))

    def test_fixed_stop_default_and_experimental_changes_are_separate(self):
        self.assertIn('const string RC2_AUDIT_FIXED = "RC2 Fixed SL"', self.audit_adapter)
        self.assertIn('input.string(RC2_AUDIT_FIXED, "Audit stop policy"', self.audit_adapter)
        self.assertIn("rc2Parity = rc2AuditLane == RC2_AUDIT_S5", self.audit_adapter)
        self.assertIn("rc2AuditMovesStop = rc2AuditLane == RC2_AUDIT_BE or rc2AuditLane == RC2_AUDIT_LOCK",
                      self.audit_adapter)
        self.assertIn('if note == "TP1" and not na(nativeRisk) and tvUsesBaselineLock and rc2Parity',
                      self.audit_adapter)
        self.assertIn('if (tvManagement or rc2AuditMovesStop) and note == "TP1"', self.audit_adapter)
        self.assertIn("if rc2AuditMovesStop and nativeTvTP1 and not nativeTimeOrder and not na(nativeRisk)",
                      self.audit_adapter)
        self.assertIn("f_tvTighten(nativeStop, protective, nativePlan.side)", self.audit_adapter)

    def test_lifecycle_observer_uses_available_events_and_stable_leg_keys(self):
        observer = self.audit_observer
        self.assertIn('e.strategyKey == "S5" and e.book == "NATIVE"', observer)
        self.assertIn("strategy.closedtrades.entry_id(j) == e.planId", observer)
        self.assertIn('e.planId + "|RC2|" + rc2AuditLane + "|" + state + (leg == "" ? "" : "|" + leg)',
                      observer)
        self.assertIn("scheduled ? e.knownTime : e.effectiveTime", observer)
        self.assertIn('e.riskBasis == "INCOMPLETE_TRADE_LOG"', observer)
        self.assertIn('anomaly := "TP_AFTER_SL"', observer)
        self.assertIn("array.includes(rc2AuditKeys, key)", observer)
        self.assertIn(',"simulated":true,"applies_from_next_tick":', observer)
        self.assertIn("alert(message, alert.freq_once_per_bar_close)", observer)


class PinnedNativeLifecycleTest(unittest.TestCase):
    def test_rc1_observed_parts_close_once_with_no_tp_after_sl(self):
        evidence = rc1.RELEASE / "evidence" / "native-logs.zip"
        expected = {5: (9, 8, 5), 15: (2, 2, 1), 30: (0, 0, 0)}
        with ZipFile(evidence) as archive:
            for timeframe, counts in expected.items():
                with self.subTest(timeframe=timeframe):
                    with archive.open(f"strict/audit_M{timeframe}.csv") as data:
                        rows = csv.DictReader(io.TextIOWrapper(data, encoding="utf-8-sig"))
                        events = [json.loads(row["Message"].split("|", 1)[1]) for row in rows
                                  if row["Message"].startswith("RC1_NATIVE|")]
                    active = {}
                    entered = set()
                    closed = set()
                    scheduled = 0
                    for event in events:
                        kind, plan_id = event["event"], event["plan_id"]
                        if kind == "NATIVE_ENTRY":
                            self.assertNotIn(plan_id, entered)
                            self.assertFalse(active)
                            entered.add(plan_id)
                            active[plan_id] = {"stop": event["initial_sl"], "sl_seen": False, "quantity": 0.0}
                        elif kind.startswith("NATIVE_EXIT_"):
                            position = active[plan_id]
                            if event["reason"] in {"TP1", "TP2", "TP3"}:
                                self.assertFalse(position["sl_seen"])
                            if event["reason"] == "SL":
                                position["sl_seen"] = True
                            position["quantity"] += event["quantity_fraction"]
                        elif kind == "NATIVE_STOP_SCHEDULED":
                            position = active[plan_id]
                            self.assertGreater((event["price"] - position["stop"]) * event["side"], 0)
                            position["stop"] = event["price"]
                            self.assertIsNone(event["effective_time"])
                            scheduled += 1
                        elif kind == "NATIVE_CLOSED":
                            self.assertNotIn(plan_id, closed)
                            self.assertAlmostEqual(active.pop(plan_id)["quantity"], 1.0, places=6)
                            closed.add(plan_id)
                    self.assertEqual((len(entered), len(closed), scheduled), counts)


class Rc2NativeExportTest(unittest.TestCase):
    START = 1_793_854_800_000

    def context(self, index, htf=None):
        opening = self.START + index * 300_000
        prices = tuple(map(Decimal, (101, 102, 100, 101)))
        return Context(self.START, index, opening, opening + 300_000, prices,
                       htf if htf is not None else (self.START, self.START))

    @staticmethod
    def run_with(context, *, core=(), ready=(), native=()):
        return Export(tuple(context), core, ready, native, (), {"end_marker_present": True})

    def test_exact_context_accepts_shift_only_and_detects_an_interior_gap(self):
        original = [self.context(i) for i in range(5)]
        moved = [replace(row, first_open=original[1].opening, index=row.index - 1)
                 for row in original[1:]]
        strict = shared(self.run_with(original), self.run_with(moved), ("core", "ready"))
        self.assertEqual(strict["status"], "MATCH")
        self.assertEqual(strict["rows"]["context"], 4)
        self.assertTrue(strict["loaded_warmup_origins_differ"])
        missing = [moved[0], replace(moved[2], index=1), replace(moved[3], index=2)]
        self.assertEqual(shared(self.run_with(original), self.run_with(missing), ())["status"], "MISMATCH")

    def test_fresh_probe_retains_nonactionable_rows_but_fails_ready_difference(self):
        full = [self.context(0, (self.START, None)), self.context(1)]
        sparse = [self.context(0, (None, None)), self.context(1)]
        watch = {"event_id": "watch", "event": "WATCH", "side": -1, "known_time": full[-1].closing, "price": 102}
        left = self.run_with(full, core=(watch,))
        right = self.run_with(sparse)
        result = fresh_comparison(left, right, ("core", "ready"))
        self.assertEqual(result["status"], "FRESH_PROBE_DISCREPANCY")
        self.assertEqual(result["htf_left_edge_availability_differences"], 1)
        self.assertEqual(result["unmatched_observations"]["left_only"], [watch])
        confirmation = {"confirmed_ms": full[-1].closing, "event_id": "ready"}
        self.assertEqual(fresh_comparison(replace(left, ready=(confirmation,)), right, ("core", "ready"))["status"],
                         "MISMATCH")

    def test_audit_stop_update_applies_after_the_observed_target(self):
        def event(kind, price, *, reason="", effective=1, quantity=Decimal("0.33333333")):
            return {"event": kind, "plan_id": "plan", "event_id": kind, "side": 1, "price": price,
                    "initial_sl": 98, "reason": reason, "effective_time": effective, "quantity_fraction": quantity,
                    "risk_basis": "ACTUAL_INITIAL_DISTANCE"}

        entry = event("NATIVE_ENTRY", 100)
        tp = event("NATIVE_EXIT_0", 102, reason="TP1")
        stop = event("NATIVE_STOP_SCHEDULED", 100, effective=None)
        sl = event("NATIVE_EXIT_1", 100, reason="SL")
        second_sl = event("NATIVE_EXIT_2", 100, reason="SL")
        close = event("NATIVE_CLOSED", 100)
        parts = {record["event_id"]: {"exit_order_id": f"plan|T{leg}"}
                 for leg, record in enumerate((tp, sl, second_sl), 1)}
        run = self.run_with((), native=(entry, tp, stop, sl, second_sl, close))
        levels = [(state, sl) for _, state, _, _, sl in native_expected(run, parts, MODES["be"])]
        self.assertEqual(levels, [("ENTRY", 98), ("TP1", 98), ("BE", 100), ("SL", 100), ("SL", 100), ("EXIT", 100)])
        with self.assertRaisesRegex(EvidenceError, "retroactive scheduled stop"):
            list(native_expected(run, parts, MODES["fixed"]))
        bad = replace(run, native=(entry, tp, stop, sl, {**second_sl, "reason": "TP3"}, close))
        with self.assertRaisesRegex(EvidenceError, "TP after SL"):
            list(native_expected(bad, parts, MODES["be"]))

    def test_alert_batch_rejects_repeated_keys_even_on_later_bars(self):
        repeated = [{"schema": NATIVE_SCHEMA, "events": [{"event_id": "same", "known_ms": known}]}
                    for known in (self.START, self.START + 300_000)]
        with self.assertRaisesRegex(EvidenceError, "duplicate event"):
            list(batches(repeated, NATIVE_SCHEMA, "known_ms"))


if __name__ == "__main__":
    unittest.main()
