"""Independent pressure calculations and native evidence regressions."""

import csv
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from gold_pro import pressure_qa as qa
from gold_pro.rc1_qa import Context, EvidenceError, Export, SYMBOL


START = 1_794_000_000_000
MINUTE = 60_000
D = Decimal


def make_bar(index, *, frame=5, close=12, high=12, low=10, volume=2, provider="tick"):
    opening = START + index * frame * MINUTE
    closing = opening + frame * MINUTE
    return qa.Bar(index, START, opening, closing, tuple(map(D, (low, high, low, close))),
                  None if volume is None else D(volume), provider)


def context(bar, frame):
    opening, high, low, close = bar.ohlc
    return {
        "symbol": SYMBOL, "timeframe": str(frame), "index": bar.index,
        "first_open_ms": bar.first_open, "open_ms": bar.opening, "close_ms": bar.closing,
        "open": float(opening), "high": float(high), "low": float(low), "close": float(close),
        "volume": None if bar.volume is None else float(bar.volume), "volume_type": bar.volume_type,
    }


def sample(chart_bar, source, *, chart=5, frame=5, mode="OHLC", lookback=1,
           buy=100, reason="OHLC_SELECTED", weighted=False, compare=False):
    relation = "HTF" if frame > chart else "LTF" if frame < chart else "LOCAL"
    return {
        "chart": str(chart), "frame": str(frame), "index": chart_bar.index,
        "decision_ms": chart_bar.closing, "chart_open_ms": chart_bar.opening,
        "source_ms": None if source is None else source.closing,
        "buy": buy, "sell": None if buy is None else 100 - buy, "mode": mode, "reason": reason,
        "lookback": lookback, "weighted": weighted, "comparison": compare, "relation": relation,
    }


def end(bar, frame):
    return {"symbol": SYMBOL, "timeframe": str(frame), "index": bar.index, "close_ms": bar.closing}


class PressureCalculationTest(TestCase):
    def test_confirmed_ohlc_warmup_and_flat_candle(self):
        bars = [
            make_bar(0, close=10),
            make_bar(1, close=10, high=10),
            make_bar(2, close=12),
        ]
        self.assertEqual(qa.reference(bars[:2], 3, False), ("WARMUP", None, None, "INSUFFICIENT_HISTORY"))
        self.assertEqual(qa.reference(bars, 3, False), ("OHLC", D(50), D(50), "OHLC_SELECTED"))
        bars.append(make_bar(3, close=12))
        self.assertEqual(qa.reference(bars[:3], 3, False)[1:3], (50, 50))
        self.assertEqual(qa.reference(bars, 2, False)[1:3], (100, 0))

    def test_tick_volume_and_whole_window_fallback(self):
        rising = make_bar(0, close=12, volume=9)
        falling = make_bar(1, close=10, volume=1)
        self.assertEqual(qa.reference([rising, falling], 2, True), ("TICK", 90, 10, "TICK_WEIGHTED"))
        self.assertEqual(qa.reference([rising, replace(falling, volume=D(0))], 2, True)[1:3], (100, 0))
        fallback = [
            ([rising, replace(falling, volume=None)], "INVALID_TICK_VOLUME"),
            ([rising, replace(falling, volume=D(-1))], "INVALID_TICK_VOLUME"),
            ([rising, replace(falling, volume_type="base")], "NON_TICK_SOURCE"),
            ([replace(rising, volume=D(0)), replace(falling, volume=D(0))], "ZERO_TICK_VOLUME"),
        ]
        for bars, reason in fallback:
            with self.subTest(reason=reason):
                self.assertEqual(qa.reference(bars, 2, True), ("FALLBACK", 50, 50, reason))

    def test_rejects_decisions_or_sources_that_were_not_yet_available(self):
        chart_bar = make_bar(1)
        hour_bar = make_bar(0, frame=60)
        htf = sample(chart_bar, hour_bar, frame=60, compare=True)
        with self.assertRaisesRegex(EvidenceError, "HTF source was unavailable at chart open"):
            qa.sample_context(htf, {chart_bar.index: chart_bar}, 5)
        ltf_chart = make_bar(1, frame=15)
        ltf = sample(ltf_chart, make_bar(2), chart=15, compare=True)
        with self.assertRaisesRegex(EvidenceError, "lower source falls outside"):
            qa.sample_context(ltf, {ltf_chart.index: ltf_chart}, 15)
        local = sample(chart_bar, chart_bar)
        local["decision_ms"] -= 1
        with self.assertRaisesRegex(EvidenceError, "confirmed chart close"):
            qa.sample_context(local, {chart_bar.index: chart_bar}, 5)

    def test_verifies_available_direct_source_and_rejects_stale_or_wrong_value(self):
        source = [make_bar(i) for i in range(6)]
        closes = [bar.closing for bar in source]
        chart_bar = make_bar(1, frame=15)
        row = sample(chart_bar, source[-1], chart=15, compare=True)
        self.assertEqual(qa.check_sample(row, chart_bar, "LTF", source, closes), "CALCULATION_VERIFIED")
        with self.assertRaisesRegex(EvidenceError, "latest independently observed confirmed bar"):
            qa.check_sample(dict(row, source_ms=closes[-2]), chart_bar, "LTF", source, closes)
        with self.assertRaisesRegex(EvidenceError, "independent OHLCV calculation"):
            qa.check_sample(dict(row, buy=75, sell=25), chart_bar, "LTF", source, closes)
        row = sample(chart_bar, None, chart=15, mode="UNAVAILABLE", reason="SOURCE_UNAVAILABLE",
                     buy=None, compare=True)
        self.assertEqual(qa.check_sample(row, chart_bar, "LTF", source, closes), "SOURCE_NOT_MAPPED")

    def test_preserves_observation_edges_and_reports_unseen_warmup(self):
        source = [make_bar(i, frame=60) for i in range(2, 5)]
        edge = make_bar(12 * 2 + 1)
        prior = make_bar(1, frame=60)
        closes = [bar.closing for bar in source]
        row = sample(edge, prior, frame=60, lookback=2, compare=True)
        self.assertEqual(qa.check_sample(row, edge, "HTF", source, closes), "SOURCE_NOT_OBSERVED")
        chart_bar = make_bar(12 * 3)
        row = sample(chart_bar, source[0], frame=60, lookback=2, compare=True)
        self.assertEqual(qa.check_sample(row, chart_bar, "HTF", source, closes), "SOURCE_WARMUP_NOT_OBSERVED")
        row = sample(make_bar(12 * 5), source[-1], frame=60, lookback=2, compare=True)
        self.assertEqual(qa.check_sample(row, make_bar(12 * 5), "HTF", source, closes), "CALCULATION_VERIFIED")


class NativePressureEvidenceTest(TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)

    def write(self, name, bars, samples, *, frame=5, complete=True):
        path = self.folder / name
        records = [("PR_CTX", context(bar, frame)) for bar in bars]
        records.extend(("PR_SAMPLE", row) for row in samples)
        if complete:
            records.append(("PR_END", end(bars[-1], frame)))
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(("Date", "Message"))
            for tag, row in records:
                writer.writerow(("2026-11-05T12:00:00Z", f"{tag}|{json.dumps(row)}"))
        return path

    def test_native_pressure_export_checks_context_counts_and_end_marker(self):
        bars = [make_bar(0, close=10, volume=1), make_bar(1, close=12, volume=3)]
        samples = [
            sample(bars[0], bars[0], mode="WARMUP", lookback=2, buy=None,
                   reason="INSUFFICIENT_HISTORY", weighted=True),
            sample(bars[1], bars[1], mode="TICK", lookback=2, buy=75, reason="TICK_WEIGHTED", weighted=True),
        ]
        rc1_context = [Context(bar.first_open, bar.index, bar.opening, bar.closing, bar.ohlc, (None, None))
                       for bar in bars]
        path = self.write("pressure_tick_M5.csv", bars, samples)
        run = qa.read_pressure(path, 5)
        result = qa.pressure_checks(run, {}, expected_weighted=True, rc1_context=rc1_context)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["frame_counts"]["5"]["verification"],
                         {"CALCULATION_VERIFIED": 1, "WARMUP_VERIFIED": 1})
        with self.assertRaisesRegex(EvidenceError, "differs from the native RC2 context"):
            qa.pressure_checks(run, {}, expected_weighted=True, rc1_context=rc1_context[1:])
        self.assertEqual(qa.pressure_checks(replace(run, complete=False), {}, expected_weighted=True)["status"],
                         "PARTIAL")
        with self.assertRaisesRegex(EvidenceError, "missing per-bar"):
            qa.pressure_checks(replace(run, rows=run.rows[1:]), {}, expected_weighted=True)

    def test_rejects_duplicate_samples_and_incorrect_weighting_provenance(self):
        bar = make_bar(0, volume=None)
        row = sample(bar, bar, mode="FALLBACK", reason="INVALID_TICK_VOLUME", weighted=True)
        path = self.write("pressure_tick_M5.csv", [bar], [row, row])
        with self.assertRaisesRegex(EvidenceError, "duplicate sample"):
            qa.pressure_checks(qa.read_pressure(path, 5), {}, expected_weighted=True)
        path = self.write("pressure_tick_M5.csv", [bar], [dict(row, reason="ZERO_TICK_VOLUME")])
        with self.assertRaisesRegex(EvidenceError, "independently calculated mode and reason"):
            qa.pressure_checks(qa.read_pressure(path, 5), {}, expected_weighted=True)

    def test_report_does_not_imply_native_parity_from_pressure_sample(self):
        bars = [make_bar(i) for i in range(2)]
        self.write("pressure_tick_M5.csv", bars, [
            sample(bar, bar, mode="TICK", reason="TICK_WEIGHTED", weighted=True) for bar in bars
        ])
        evidence = qa.report(self.folder)
        self.assertEqual(evidence["status"], "PARTIALLY_COMPLETE")
        self.assertEqual(evidence["frames"]["M5"]["pressure"]["tick"]["status"], "VERIFIED")
        self.assertEqual(evidence["frames"]["M5"]["parity"]["indicator"]["status"], "MISSING_EVIDENCE")

    def test_repeat_preserves_confirmed_rows_on_the_same_context(self):
        bars = [make_bar(i) for i in range(2)]
        rows = [sample(bar, bar) for bar in bars]
        original = qa.read_pressure(self.write("feature_indicator_M5.csv", bars, rows), 5)
        repeat = qa.read_pressure(self.write("pressure_repeat_M5.csv", bars, rows), 5)
        self.assertEqual(qa.pressure_repeat(original, repeat)["status"], "MATCH")
        shifted = replace(repeat, bars=tuple(replace(b, index=b.index + 2, first_open=b.first_open - 600_000)
                                            for b in repeat.bars),
                          rows=tuple(dict(row, index=row["index"] + 2) for row in repeat.rows))
        self.assertEqual(qa.pressure_repeat(original, shifted)["status"], "MATCH_SHIFTED_WARMUP")
        changed = replace(repeat, rows=(rows[0], dict(rows[1], buy=50, sell=50)))
        self.assertEqual(qa.pressure_repeat(original, changed)["status"], "MISMATCH")
        self.assertEqual(qa.pressure_repeat(original, replace(repeat, bars=repeat.bars[1:]))["status"],
                         "NONIDENTICAL_CONTEXT")

    def test_audit_parity_exposes_strategy_api_and_detects_a_pnl_mismatch(self):
        bar = make_bar(0)
        ctx = Context(bar.first_open, bar.index, bar.opening, bar.closing, bar.ohlc, (None, None))
        run = Export((ctx,), (), (), ({"event": "NATIVE_EXIT_1", "cash_pnl": D(5)},), (), {
            "end_marker_present": True, "starts_at_first_loaded_bar": True,
        })
        summary = {"grossprofit": D(8), "grossloss": D(-2), "netprofit": D(6)}
        journal = {"RC2_AUDIT_SUMMARY": [summary], "RC2_NATIVE_PART": [{"source_id": "fill"}],
                   "RC2_NATIVE_ALERT": []}
        matched = qa.native_parity((run, journal), (run, journal), "audit")
        self.assertEqual(matched["status"], "MATCH")
        self.assertEqual(matched["native_profit_factor"], 4)
        self.assertEqual(matched["exported_partial_cash_pnl_usd"], 5)
        changed = dict(journal, RC2_AUDIT_SUMMARY=[dict(summary, netprofit=D(7))])
        mismatch = qa.native_parity((run, journal), (run, changed), "audit")
        self.assertEqual((mismatch["status"], mismatch["stream"]), ("MISMATCH", "RC2_AUDIT_SUMMARY"))

    def test_shared_alert_match_does_not_pass_when_the_loaded_warmup_differs(self):
        bar = make_bar(2)
        ctx = Context(START, 2, bar.opening, bar.closing, bar.ohlc, (None, None))
        row = {"confirmed_ms": bar.closing, "state": "READY"}
        original = Export((ctx,), (), (row,), (), (row,), {"end_marker_present": True})
        shifted = replace(original, context=(replace(ctx, first_open=START + 5 * MINUTE, index=1),))
        journal = {"RC2_ALERT": [{"events": [row]}]}
        result = qa.native_parity((original, journal), (shifted, journal), "indicator")
        self.assertEqual(result["status"], "NONIDENTICAL_CONTEXT")
        overlap = result["shared_window_diagnostic"]
        self.assertEqual(overlap["status"], "MATCH")
        self.assertEqual(overlap["rows"]["RC2_ALERT"], 1)
        different = {"RC2_ALERT": [{"events": [dict(row, state="WATCH")]}]}
        result = qa.native_parity((original, journal), (shifted, different), "indicator")
        self.assertEqual(result["shared_window_diagnostic"]["status"], "MISMATCH")
