"""Regressions for RC1 export comparison and causal alert validation."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from gold_pro.rc1_qa import (
    ALERT_SCHEMA, CORE_FIELDS, CORE_SHA, EvidenceError, compare_frame, compare_visual_export, read_export, report,
)


START = 1_793_872_800_000
PLAN = {"entry": 4090, "sl": 4100, "tp1": 4080, "tp2": 4070, "tp3": 4060}


def event(timeframe, index, kind, *, book="CORE"):
    interval = timeframe * 60_000
    known = START + (index + 1) * interval
    plan_id = f"S5|OANDA:XAUUSD|{timeframe}|2|-1|{START}|{START + 2 * interval}"
    return {
        **dict.fromkeys(CORE_FIELDS),
        "schema": "qirvanta.event.v1", "core_sha256": CORE_SHA,
        "event_id": f"{plan_id}|{book}|{kind}|{known}", "event": kind,
        "book": book, "plan_id": plan_id, "strategy_key": "S5", "family": 2, "side": -1,
        "symbol": "OANDA:XAUUSD", "timeframe": str(timeframe), "advisory_only": True,
        "bar_open_time": known - interval, "bar_close_time": known, "known_time": known,
        "effective_time": known if book == "CORE" else known - interval,
        "reference": PLAN["entry"], "initial_sl": PLAN["sl"],
        "tp1": PLAN["tp1"], "tp2": PLAN["tp2"], "tp3": PLAN["tp3"], "reason": "Потвърждение",
        "price": PLAN["entry"], "quantity_fraction": 1 if book == "NATIVE" else None,
    }


def payload(source, state):
    known = source["known_time"]
    stamp = datetime.fromtimestamp(known / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "model": "S5", "symbol": source["symbol"], "timeframe": source["timeframe"], "direction": "SELL",
        "state": state, "confirmed_ms": known, "confirmation_utc": stamp, "reason": source["reason"],
        "event_id": f'{source["event_id"]}|RC1|{state}', "source_id": source["event_id"], "provisional": state == "WATCH",
        **(PLAN if state == "READY" else dict.fromkeys(PLAN)),
    }


def log(tag, row):
    return tag + "|" + json.dumps(row, ensure_ascii=False)


def run(timeframe, role, *, change_high=False, native_kind="NATIVE_ENTRY", end=True, bad_alert=None):
    interval = timeframe * 60_000
    watch = event(timeframe, 0, "WATCH")
    confirmed = event(timeframe, 1, "CONFIRMED")
    rejected = event(timeframe, 2, "REJECTED")
    ready = payload(confirmed, "READY")
    messages = [
        f"RC1_CTX|1|OANDA:XAUUSD|{timeframe}|{START - interval}|{i + 1}|{START + i * interval}|"
        f"{START + (i + 1) * interval}|4095|{4101 if change_high else 4100}|4080|4090|{START}|null"
        for i in range(3)
    ]
    messages += [log("RC1_CORE", row) for row in (watch, confirmed, rejected)]
    messages.append(log("RC1_READY", ready))
    if role == "indicator":
        alerts = [payload(confirmed, "CONFIRMED"), ready.copy()]
        if bad_alert is not None:
            bad_alert(alerts)
        messages += [log("RC1_ALERT", {"schema": ALERT_SCHEMA, "events": entries})
                     for entries in ([payload(watch, "WATCH")], alerts)]
    else:
        messages.append(log("RC1_NATIVE", event(timeframe, 2, native_kind, book="NATIVE")))
    if end:
        messages.append(f"RC1_END|1|OANDA:XAUUSD|{timeframe}|3|{START + 3 * interval}")
    return messages


def write_export(path, messages):
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("Date", "Message"))
        writer.writerow(("2026-11-05T12:00:00Z", "\n".join(messages)))


class NativeGoldProRC1Test(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)

    def write(self, role, timeframe=5, **options):
        path = self.folder / f"{role}_M{timeframe}.csv"
        write_export(path, run(timeframe, role, **options))
        return path

    def test_equal_ordered_native_exports_over_bounded_windows(self):
        for timeframe in (5, 15, 30):
            for role in ("indicator", "audit", "reference"):
                self.write(role, timeframe)
        result = report(self.folder)
        self.assertEqual(result["status"], "PINE_LOG_PARITY_VERIFIED")
        frame = result["frames"]["M5"]
        self.assertEqual(frame["audit_vs_reference"]["coverage"], "BOUNDED_WINDOW")
        self.assertEqual(frame["indicator_vs_audit"]["rows"], {"core": 3, "ready": 1})
        self.assertEqual(frame["alert_payloads"], {"status": "PAYLOADS_VERIFIED", "rows": 3})
        self.assertEqual(frame["native_execution_examples"], 1)

    def test_changed_candle_blocks_signal_comparison(self):
        self.write("indicator")
        self.write("audit", change_high=True)
        comparison = compare_frame(self.folder, 5)["indicator_vs_audit"]
        self.assertEqual((comparison["status"], comparison["stream"], comparison["row"]), ("MISMATCH", "context", 1))

    def test_changed_native_event_blocks_parity_only_for_strategy_pair(self):
        self.write("indicator")
        self.write("audit", native_kind="NATIVE_NOT_FILLED")
        self.write("reference")
        frame = compare_frame(self.folder, 5)
        self.assertEqual(frame["indicator_vs_audit"]["status"], "MATCH")
        self.assertEqual(frame["audit_vs_reference"]["stream"], "native")
        self.assertEqual(report(self.folder)["status"], "PARITY_FAILED")

    def test_unfinished_and_missing_exports_are_never_verified(self):
        self.write("indicator", end=False)
        self.write("audit")
        result = report(self.folder)
        self.assertEqual(result["status"], "PARTIALLY_COMPLETE")
        self.assertEqual(result["frames"]["M5"]["indicator_vs_audit"]["status"], "MATCHED_EXCERPT")
        self.assertEqual(result["frames"]["M5"]["audit_vs_reference"]["status"], "MISSING_EVIDENCE")

    def test_stale_prices_and_lookahead_confirmation_are_rejected(self):
        variants = [
            (lambda rows: rows[0].update(entry=4100), "non-READY event contains plan prices"),
            (lambda rows: rows[1].update(tp1=4081), "plan levels differ"),
            (lambda rows: rows[1].update(confirmed_ms=START), "confirmation timestamp differs"),
            (lambda rows: rows[0].update(confirmation_utc="2026-11-03T00:00:00Z"), "UTC timestamp does not match"),
        ]
        for alter, reason in variants:
            with self.subTest(reason=reason):
                path = self.write("indicator", bad_alert=alter)
                with self.assertRaisesRegex(EvidenceError, reason):
                    read_export(path, 5, "indicator")

    def test_duplicate_alerts_and_rejected_setup_alerts_are_rejected(self):
        cases = [
            (lambda rows: rows.append(rows[0]), "duplicate alert event ID"),
            (lambda rows: rows.append(payload(event(5, 2, "REJECTED"), "INVALIDATED")),
             "state or provisional flag differs"),
        ]
        for alter, reason in cases:
            with self.subTest(reason=reason):
                path = self.write("indicator", bad_alert=alter)
                with self.assertRaisesRegex(EvidenceError, reason):
                    read_export(path, 5, "indicator")

    def test_missing_context_bar_or_incomplete_native_ledger_is_rejected(self):
        path = self.folder / "indicator_M5.csv"
        messages = run(5, "indicator")
        del messages[1]
        write_export(path, messages)
        with self.assertRaisesRegex(EvidenceError, "missing, repeated or out-of-order bars"):
            read_export(path, 5, "indicator")
        path = self.write("audit", native_kind="NATIVE_LEDGER_INCOMPLETE")
        with self.assertRaisesRegex(EvidenceError, "incomplete execution journal"):
            read_export(path, 5, "audit")

    def test_future_htf_timestamp_is_rejected(self):
        path = self.folder / "indicator_M5.csv"
        messages = run(5, "indicator")
        first = messages[0].split("|")
        first[-1] = str(START + 300_000)
        messages[0] = "|".join(first)
        write_export(path, messages)
        with self.assertRaisesRegex(EvidenceError, "HTF data was unavailable"):
            read_export(path, 5, "indicator")

    def test_final_visual_recheck_normalizes_only_the_shifted_overlap(self):
        final_dir = self.folder / "final"
        final_dir.mkdir()
        for timeframe in (5, 15, 30):
            interval = timeframe * 60_000
            strict = self.write("indicator", timeframe)
            final = final_dir / strict.name
            rows = run(timeframe, "indicator")
            for i in range(3):
                bar = rows[i].split("|")
                bar[4], bar[5] = str(START), str(i)
                rows[i] = "|".join(bar)
            rows[-1] = f"RC1_END|1|OANDA:XAUUSD|{timeframe}|2|{START + 3 * interval}"
            write_export(final, rows)
        checks = report(self.folder, visual_recheck_dir=final_dir)["visual_recheck"]
        for timeframe in (5, 15, 30):
            match = checks[f"M{timeframe}"]
            self.assertEqual(match["status"], "MATCH")
            self.assertEqual(match["rows"], {"context": 3, "core": 3, "ready": 1, "alerts": 3})
            self.assertEqual(match["final_minus_strict_bar_index"], -1)
            self.assertEqual(match["strict"]["chart_first_open_ms"], START - timeframe * 60_000)
            self.assertEqual(match["final"]["chart_first_open_ms"], START)
        rows[0] = rows[0].replace("|4100|4080|", "|4101|4080|")
        write_export(final, rows)
        mismatch = compare_visual_export(strict, final, 30)
        self.assertEqual((mismatch["status"], mismatch["stream"], mismatch["row"]), ("MISMATCH", "context", 1))
        self.assertEqual(report(self.folder, visual_recheck_dir=final_dir)["status"], "PARITY_FAILED")


if __name__ == "__main__":
    unittest.main()
