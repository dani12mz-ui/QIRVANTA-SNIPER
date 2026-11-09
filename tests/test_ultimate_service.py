"""Deterministic contracts and a synthetic full local-paper lifecycle."""
from dataclasses import asdict, replace
from contextlib import closing
import csv
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from qirvanta_ultimate.calendar import ArchivedCalendar, CalendarSnapshot, ReleaseRevision, assess_news
from qirvanta_ultimate.__main__ import _spool_batch
from qirvanta_ultimate.common import digest, read_json
from qirvanta_ultimate.intelligence import (AnalysisContext, AnalysisPolicy, SPECIALISTS, analyze, observe)
from qirvanta_ultimate.journal import Journal
from qirvanta_ultimate.paper import PaperProtocol, register, report, set_manual_pause
from qirvanta_ultimate.risk import Quote, RiskDecision
from qirvanta_ultimate.service import PaperProfile, create_protocol, implementation_hash, process_packet


BASE = 1767225600000
TF = 900000
SOURCE = "SYNTHETIC_LOCAL_FIXTURE"
SYMBOL = "TEST.GOLD"
URL = "https://fixture.invalid/calendar"


def row(i, open_, high, low, close):
    return {"time": BASE + i * TF, "time_close": BASE + (i + 1) * TF,
            "open": open_, "high": high, "low": low, "close": close}


def bars():
    result = []
    for i in range(820):
        c = round(100 + i * .05, 2)
        result.append(row(i, round(c - .03, 2), round(c + .1, 2), round(c - .1, 2), c))
    c = result[-1]["close"]
    result[-2] = row(818, c - .08, c, c - .68, c - .58)
    result[-1] = row(819, c - .12, c + .17, c - .15, c + .15)
    return result


def frame():
    data = bars()
    return observe(data, symbol=SYMBOL, source=SOURCE, source_hash="0" * 64,
                   as_of_ms=data[-1]["time_close"], families=("S1",), mintick=.01)


def calendar(asof, revisions=(), scope=("US_MAJOR_RELEASES",)):
    return CalendarSnapshot("TEST_CALENDAR", URL, asof, asof - 3600000,
                            asof + 3600000, ("USD",), tuple(revisions), scope)


def profile():
    return PaperProfile(source=SOURCE, symbol=SYMBOL, quote_server="TEST_SERVER",
                        account_currency="USD", contract_size=100, point=.01, tick_size=.01,
                        loss_per_tick_lot=1, profit_per_tick_lot=1, min_lot=.01, max_lot=1,
                        lot_step=.01, margin_per_lot=100, stops_level_points=1,
                        fee_per_lot_per_fill=.25, max_spread=.03, max_entry_deviation=.05,
                        risk_fraction=.005, calendar_source=URL, slippage_ticks=1)


def draft(close_ms):
    return {"protocol_id": "synthetic-service-test", "timeframe_ms": TF, "families": ("S1",),
            "history_cutoff_ms": close_ms - TF, "start_ms": close_ms - 1000,
            "end_ms": close_ms + 4 * TF, "initial_cash": 10000}


class CalendarTests(unittest.TestCase):
    def test_calendar_bitemporal_revisions(self):
        release = BASE + 10000
        before = ReleaseRevision("cpi", release, BASE, "US", "USD", "CPI", "HIGH",
                                 "America/New_York", URL, consensus=2., consensus_observed_ms=BASE)
        after = replace(before, observed_ms=release + 100, actual=3.)
        provider = ArchivedCalendar((calendar(BASE, (before,)), calendar(release + 100, (before, after))))
        self.assertIsNone(provider.snapshot(BASE).revisions[-1].surprise)
        self.assertEqual(provider.snapshot(release + 100).revisions[-1].surprise, 1.)
        self.assertEqual(assess_news(provider, as_of_ms=BASE).status, "EVENT_WINDOW")

    def test_future_actual_and_late_forecast_rejected(self):
        fields = ("ppi", BASE + 10000, BASE, "US", "USD", "PPI", "HIGH", "UTC", URL)
        with self.assertRaises(ValueError):
            ReleaseRevision(*fields, actual=1.)
        with self.assertRaises(ValueError):
            ReleaseRevision(*fields, consensus=2., consensus_observed_ms=BASE + 20000)

    def test_absent_stale_and_scope_insufficiency(self):
        provider = ArchivedCalendar((calendar(BASE, scope=("BLS_SELECTED",)),))
        self.assertTrue(assess_news(None, as_of_ms=BASE).blocked)
        self.assertEqual(assess_news(provider, as_of_ms=BASE).status, "NEWS_DATA_UNAVAILABLE")
        self.assertEqual(assess_news(provider, as_of_ms=BASE, required_scope=("BLS_SELECTED",)).status,
                         "CLEAR_IN_DECLARED_COVERAGE")
        self.assertTrue(assess_news(provider, as_of_ms=BASE + 7200000).blocked)
        self.assertEqual(assess_news(provider, as_of_ms=BASE, manual_pause=True).status, "MANUAL_PAUSE")

    def test_cancellation_and_future_snapshot_visibility(self):
        evt = ReleaseRevision("jobs", BASE + 60000, BASE, "US", "USD", "Employment",
                              "UNKNOWN", "UTC", URL)
        cancelled = replace(evt, observed_ms=BASE + 10000, cancelled=True)
        provider = ArchivedCalendar((calendar(BASE, (evt,)), calendar(BASE + 10000, (evt, cancelled))))
        self.assertIsNone(provider.snapshot(BASE - 1))
        self.assertTrue(assess_news(provider, as_of_ms=BASE).blocked)
        self.assertFalse(assess_news(provider, as_of_ms=BASE + 10000).blocked)


class SpecialistTests(unittest.TestCase):
    def setUp(self):
        self.market = frame()
        self.at = self.market.as_of_ms
        close = self.market.bar["close"]
        self.quote = Quote(SYMBOL, close - .01, close, self.at, "TEST_SERVER")
        self.news = ArchivedCalendar((calendar(self.at),))
        self.policy = AnalysisPolicy(research_candidates=True)
        self.allow = lambda *_: RiskDecision(True, "TEST_VETO_OPEN", .3, 100., target_lots=(.1, .1, .1))

    def analyze(self, market=None, **kw):
        defaults = {"quote": self.quote, "news_provider": self.news, "guardian": self.allow}
        defaults.update(kw)
        return analyze(AnalysisContext(market or self.market, self.policy, **defaults))

    def test_actual_distinct_roles_causal_candidate_and_no_model_claim(self):
        first = self.analyze()
        second = self.analyze()
        self.assertEqual(first["trace_id"], second["trace_id"])
        self.assertEqual(len(SPECIALISTS), len({f for _, f in SPECIALISTS}))
        self.assertEqual(len(first["specialists"]), 12)
        self.assertEqual(first["state"], "BUY_CANDIDATE")
        self.assertIsNone(first["inference_provider"])
        self.assertFalse(first["broker_orders"])
        self.assertEqual(first["specialists"]["management"]["facts"]["family"], "S1")

    def test_future_bar_does_not_change_prefix(self):
        prior = self.market
        data = bars() + [row(820, 200, 222, 188, 217)]
        changed = observe(data, symbol=SYMBOL, source=SOURCE, source_hash="0" * 64,
                          as_of_ms=self.at, families=("S1",), mintick=.01)
        self.assertEqual(prior, changed)

    def test_conflicting_buy_sell_suggestions(self):
        signal = next(d for d in self.market.candidates if d["status"] == "CONFIRMED")
        market = replace(self.market, candidates=(signal, {**signal, "side": -1, "signal_id": "other"}))
        result = self.analyze(market)
        self.assertEqual(result["state"], "WAIT_CONFIRMATION")
        self.assertEqual(result["reason"], "ANALYTICAL_DISAGREEMENT")

    def test_no_calendar_no_h4_stale_quote_and_hard_veto(self):
        self.assertEqual(self.analyze(news_provider=None)["state"], "DATA_UNAVAILABLE")
        features = {**self.market.features, "h4": {"bias": None, "available_at": None}}
        self.assertEqual(self.analyze(replace(self.market, features=features))["state"], "DATA_UNAVAILABLE")
        self.assertEqual(self.analyze(quote=replace(self.quote, at_ms=self.at - 1))["state"], "DATA_UNAVAILABLE")
        self.assertEqual(self.analyze(guardian=lambda *_: RiskDecision(False, "LOSS_LIMIT"))["state"], "RISK_BLOCKED")

    def test_timeout_and_late_result_cannot_release_veto(self):
        event = threading.Event()
        policy = replace(self.policy, timeout_ms=50)
        try:
            result = analyze(AnalysisContext(self.market, policy, self.quote, self.news,
                                             guardian=lambda *_: event.wait(2)))
        finally:
            event.set()
        self.assertEqual(result["state"], "DATA_UNAVAILABLE")
        self.assertEqual(result["specialists"]["risk"]["facts"]["reason"], "SPECIALIST_TIMEOUT")

    def test_model_unavailable_and_regime_change_audited(self):
        result = self.analyze(model_service=object())
        self.assertEqual(result["specialists"]["audit"]["facts"]["inference"], "UNAVAILABLE")
        altered = replace(self.market, features={**self.market.features, "regime": "RANGE"})
        self.assertEqual(self.analyze(altered)["specialists"]["regime"]["facts"]["regime"], "RANGE")


class PaperLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.db = self.folder / "paper.sqlite"
        self.profile = profile()
        m = frame()
        self.at = m.as_of_ms
        self.entry = m.bar["close"]
        filename = self.folder / "bars.csv"
        data = bars()
        with filename.open("w", newline="", encoding="utf-8") as out:
            writer = csv.DictWriter(out, fieldnames=data[0])
            writer.writeheader()
            writer.writerows(data)
        self.data_hash = hashlib.sha256(filename.read_bytes()).hexdigest()
        filename.rename(self.folder / (self.data_hash + ".csv"))
        snapshot = {"schema": "qirvanta.calendar.v1", **asdict(calendar(self.at))}
        blob = json.dumps(snapshot).encode()
        self.news_hash = hashlib.sha256(blob).hexdigest()
        (self.folder / (self.news_hash + ".json")).write_bytes(blob)
        self.register_fixture()

    def register_fixture(self):
        config = PaperProtocol(**draft(self.at), source=self.profile.source, symbol=self.profile.symbol,
                               profile_hash=digest(asdict(self.profile)), implementation_hash=implementation_hash(),
                               evidence_class="SYNTHETIC_TEST")
        with Journal(self.db) as journal:
            register(journal, config, now_ms=self.at - 2000)

    def packet(self, at, bid, ask, **overrides):
        return {"source": SOURCE, "session_open": True, "bars_sha256": self.data_hash,
                "calendar_sha256": self.news_hash,
                "quote": asdict(Quote(SYMBOL, bid, ask, at, "TEST_SERVER")), **overrides}

    def step(self, journal, at, bid, ask, **kw):
        return process_packet(journal, self.profile, self.packet(at, bid, ask, **kw),
                              bars_root=self.folder, calendar_root=self.folder, at_ms=at)

    def test_pending_fill_three_parts_and_restart(self):
        with Journal(self.db) as journal:
            self.assertEqual(self.step(journal, self.at, self.entry - .01, self.entry)["decision"], "BUY_CANDIDATE")
            self.assertEqual(journal.state()["active"]["state"], "PENDING")
        with Journal(self.db) as journal:
            replay = self.step(journal, self.at, self.entry - .01, self.entry)
            self.assertEqual(replay["status"], "DUPLICATE")
            self.step(journal, self.at + 1000, self.entry - .01, self.entry)
            position = journal.state()["active"]
            self.assertEqual(position["state"], "OPEN")
            self.assertAlmostEqual(position["fill_price"], self.entry + .01)
            for i, target in enumerate(position["targets"], start=2):
                self.step(journal, self.at + 1000 * i, target, target + .01)
            result = report(journal)
            self.assertEqual(result["metrics"]["n"], 1)
            self.assertGreater(result["metrics"]["net_cash"], 0)
            self.assertEqual(result["target_parts"], {"TP1": 1, "TP2": 1, "TP3": 1})
            self.assertEqual(result["status"], "INSUFFICIENT_EVIDENCE")
            self.assertEqual(result["evidence_class"], "SYNTHETIC_TEST")
            self.assertAlmostEqual(result["paid_cash_equity"], journal.state()["cash"])
            self.assertFalse(result["broker_orders"])
            self.assertEqual(journal.verify()["status"], "LOCALLY_VERIFIED")
            notes = [e["data"]["text"] for e in journal.events() if e["kind"] == "LOCAL_NOTIFICATION"]
            self.assertTrue(any("Симулиран вход" in line for line in notes))
            self.assertEqual(sum("Симулиран TP" in line for line in notes), 3)

    def test_reject_changed_duplicate_and_missing_calendar_cancels_pending(self):
        with Journal(self.db) as journal:
            self.step(journal, self.at, self.entry - .01, self.entry)
            with self.assertRaises(ValueError):
                self.step(journal, self.at, self.entry - .02, self.entry)
            result = self.step(journal, self.at + 1000, self.entry - .01, self.entry, calendar_sha256=None)
            self.assertEqual(result["decision"], "DATA_UNAVAILABLE")
            self.assertIsNone(journal.state()["active"])
            self.assertEqual(report(journal)["missed"], 1)

    def test_open_stop_survives_missing_features_and_operator_pause(self):
        with Journal(self.db) as journal:
            self.step(journal, self.at, self.entry - .01, self.entry)
            self.step(journal, self.at + 1000, self.entry - .01, self.entry)
            self.assertEqual(journal.state()["active"]["state"], "OPEN")
            stop = journal.state()["active"]["stop"]
            set_manual_pause(journal, True, at_ms=self.at + 1500)
            self.step(journal, self.at + 2000, stop - .04, stop - .03, bars_sha256="a" * 64)
            state = journal.state()
            self.assertIsNone(state["active"])
            self.assertEqual(state["cooldown_until_ms"], self.at + 2000 + TF)
            outcome = report(journal)
            self.assertEqual(outcome["metrics"]["losses"], 1)
            self.assertTrue(outcome["operator_intervention"])

    def test_invalid_quote_scope_and_profile_drift_roll_back(self):
        with Journal(self.db) as journal:
            initial = journal.verify()["events"]
            with self.assertRaises(ValueError):
                self.step(journal, self.at - 10000, self.entry - .01, self.entry)
            with self.assertRaises(ValueError):
                process_packet(journal, replace(self.profile, max_spread=.01),
                               self.packet(self.at, self.entry - .01, self.entry),
                               bars_root=self.folder, calendar_root=self.folder, at_ms=self.at)
            self.assertEqual(journal.verify()["events"], initial)

    def test_journal_detects_tampered_index(self):
        with closing(sqlite3.connect(self.db)) as connection:
            with connection:
                connection.execute("INSERT INTO intake VALUES ('tampered', 'hash')")
        with self.assertRaises(ValueError):
            Journal(self.db)

    def test_no_broker_execution_profile_or_historical_registration(self):
        with self.assertRaises(ValueError):
            replace(self.profile, mode="DEMO_EXECUTION")
        with Journal(self.folder / "other.sqlite") as journal:
            with self.assertRaises(ValueError):
                create_protocol(journal, self.profile, draft(self.at), at_ms=self.at)
            self.assertIsNone(journal.state())

    def test_unfilled_limit_retains_missed_outcome(self):
        self.db = self.folder / "limit.sqlite"
        self.profile = replace(self.profile, entry_kind="LIMIT")
        self.register_fixture()
        with Journal(self.db) as journal:
            self.step(journal, self.at, self.entry - .01, self.entry)
            self.step(journal, self.at + 1000, self.entry, self.entry + .01)
            self.assertEqual(journal.state()["active"]["state"], "PENDING")
            self.step(journal, self.at + TF, self.entry, self.entry + .01)
            self.assertIsNone(journal.state()["active"])
            misses = [e for e in journal.events() if e["kind"] == "PAPER_MISSED"]
            self.assertEqual([e["data"]["reason"] for e in misses], ["UNFILLED_LIMIT"])

    def test_partial_at_tp1_then_break_even_remainder(self):
        with Journal(self.db) as journal:
            self.step(journal, self.at, self.entry - .01, self.entry)
            self.step(journal, self.at + 1000, self.entry - .01, self.entry)
            position = journal.state()["active"]
            tp1 = position["targets"][0]
            self.step(journal, self.at + 2000, tp1, tp1 + .01)
            self.assertEqual(journal.state()["active"]["stop"], position["fill_price"])
            self.step(journal, self.at + 3000, position["fill_price"], position["fill_price"] + .01)
            parts = [e["data"] for e in journal.events() if e["kind"] == "PAPER_PARTIAL"]
            self.assertEqual([p["reason"] for p in parts], ["TP1", "STOP", "STOP"])
            self.assertAlmostEqual(sum(p["quantity"] for p in parts), position["lots"])

    def test_closed_session_does_not_execute_quote_and_end_is_censored(self):
        with Journal(self.db) as journal:
            self.step(journal, self.at, self.entry - .01, self.entry)
            self.step(journal, self.at + 1000, self.entry - .01, self.entry)
            stop = journal.state()["active"]["stop"]
            self.step(journal, self.at + 2000, stop - .04, stop - .03, session_open=False)
            self.assertEqual(journal.state()["active"]["state"], "OPEN")
            outcome = report(journal, at_ms=self.at + 4 * TF)
            self.assertEqual(outcome["outstanding"], "OPEN_RIGHT_CENSORED")
            self.assertEqual(outcome["metrics"]["n"], 0)

    def test_spool_sorts_by_quote_not_filename_and_deduplicates_invalid(self):
        folder = self.folder / "spool"
        folder.mkdir()
        for file, offset in (("a.json", 1000), ("z.json", 0)):
            packet = self.packet(self.at + offset, self.entry - .01, self.entry)
            (folder / file).write_text(json.dumps(packet), encoding="utf-8")
        (folder / "malformed.json").write_text("invalid", encoding="utf-8")
        with Journal(self.db) as journal:
            seen, valid, errors = _spool_batch(journal, folder, set())
            self.assertEqual([p[0] for p in valid], [self.at, self.at + 1000])
            self.assertEqual(errors, 0)
            self.assertEqual(_spool_batch(journal, folder, seen)[1], [])
            _spool_batch(journal, folder, set())
            self.assertEqual(sum(e["kind"] == "INTAKE_REJECTED" for e in journal.events()), 1)
            with patch.object(Path, "open", side_effect=FileNotFoundError):
                _, unavailable, errors = _spool_batch(journal, folder, set())
            self.assertEqual(unavailable, [])
            self.assertEqual(errors, 3)

    def test_drawdown_duration_includes_recovery_event(self):
        with Journal(self.db) as journal, journal.transaction():
            journal.append("PAPER_FILL", self.at, {"cash_delta": 2})
            journal.append("PAPER_PARTIAL", self.at + 1000, {"cash_delta": -1, "reason": "STOP"})
            journal.append("PAPER_PARTIAL", self.at + 9000, {"cash_delta": 2, "reason": "TP1"})
            result = report(journal)
            self.assertEqual(result["paid_cash_drawdown"], 1)
            self.assertEqual(result["observed_drawdown_duration_ms"], 9000)


class InputBoundaryTests(unittest.TestCase):
    def test_duplicate_json_fields_and_non_finite_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            filename = Path(folder) / "input.json"
            for data in ('{"mode":"PAPER","mode":"DEMO_EXECUTION"}', '{"value":NaN}'):
                filename.write_text(data, encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_json(filename)


if __name__ == "__main__":
    unittest.main()
