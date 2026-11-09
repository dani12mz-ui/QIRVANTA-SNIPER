"""Synthetic tests of Phase 11 portfolio isolation and causal execution."""
from dataclasses import asdict, replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from qirvanta_ultimate.common import canonical, read_json
from qirvanta_ultimate.phase11_portfolios import PaperRoster, STRATEGIES, register_roster
from qirvanta_ultimate.phase11_signals import ChampionSignal, PRODUCTION_CORE
from qirvanta_ultimate.risk import Quote
from test_ultimate_service import SOURCE, SYMBOL, TF, bars, calendar, profile, row


class PortfolioTests(TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "roster"
        self.bars = bars()
        # A real S5 detector fixture: downside break of the prior range, then reclaim.
        self.bars[-2] = row(818, 140.70, 140.72, 139.70, 139.75)
        self.bars[-1] = row(819, 139.77, 140.12, 139.76, 140.09)
        self.at = self.bars[-1]["time_close"]
        self.entry = self.bars[-1]["close"]
        self.config = {"roster_id": "test-roster", "source_commit": "a" * 40,
                       "evidence_class": "SYNTHETIC_TEST", "instruments": [{
                           "profile": asdict(profile()), "dataset_id": "SYNTHETIC_BROKER_BARS",
                           "contract_spec_source": "SYNTHETIC_CONTRACT",
                           "timeframe_ms": TF, "history_cutoff_ms": self.at - TF,
                           "start_ms": self.at - 1000, "end_ms": self.at + TF, "initial_cash": 10_000,
                       }]}
        self.news = calendar(self.at)

    def create(self):
        register_roster(self.path, self.config, at_ms=self.at - 2000)
        return PaperRoster(self.path)

    def step(self, roster, offset=0, bid=None, ask=None, **overrides):
        kwargs = dict(source=SOURCE, bars=self.bars, calendar=self.news, session_open=True, at_ms=self.at + offset)
        kwargs.update(overrides)
        quote = Quote(SYMBOL, self.entry - .01 if bid is None else bid,
                      self.entry if ask is None else ask, self.at + offset, "TEST_SERVER")
        return roster.process(SYMBOL, quote, **kwargs)["portfolios"]

    def test_real_s5_detector_opens_only_its_own_wallet_and_groups_partials(self):
        with self.create() as roster:
            first = self.step(roster)
            self.assertEqual(tuple(first), STRATEGIES)
            self.assertEqual(first["S5"]["decision"], "BUY_CANDIDATE")
            streams = roster.journal_streams(SYMBOL)
            self.assertEqual(streams["S5"].state()["active"]["state"], "PENDING")
            self.assertTrue(all(j.state()["active"] is None for s, j in streams.items() if s != "S5"))
        with PaperRoster(self.path) as roster:
            self.assertTrue(all(result["status"] == "DUPLICATE" for result in self.step(roster).values()))
            self.step(roster, 1000)
            streams = roster.journal_streams(SYMBOL)
            wallet = streams["S5"]
            pos = wallet.state()["active"]
            self.assertEqual(pos["state"], "OPEN")
            self.assertAlmostEqual(pos["fill_price"], self.entry + .01)
            for i, target in enumerate(pos["targets"], start=2):
                self.step(roster, i * 1000, target, round(target + .01, 2))
            outcomes = roster.reports(at_ms=self.at + TF)["instruments"][SYMBOL]["portfolios"]
            self.assertEqual(outcomes["S5"]["complete_campaigns"], 1)
            self.assertEqual(outcomes["S5"]["target_parts"], {"TP1": 1, "TP2": 1, "TP3": 1})
            self.assertEqual(outcomes["S5"]["status"], "INSUFFICIENT_FORWARD_EVIDENCE")
            self.assertIn("NON_PROSPECTIVE_INPUT", outcomes["S5"]["forward_evidence_failures"])
            self.assertEqual(outcomes["CHAMPION"]["external_signal_status"], "BLOCKED_EXTERNAL")
            self.assertGreater(outcomes["S5"]["paid_fill_fees"], 0)
            self.assertEqual(outcomes["S3"]["paid_cash_equity"], 10_000)
            events = wallet.events(kinds=("PAPER_PENDING", "PAPER_FILL", "PAPER_PARTIAL", "PAPER_COMPLETE"))
            self.assertEqual(len({e["data"]["campaign_id"] for e in events}), 1)
            self.assertEqual(len({e["data"]["signal_id"] for e in events}), 1)
            self.assertEqual(wallet.verify()["status"], "LOCALLY_VERIFIED")

    def test_gap_cancels_pending_without_backfill_and_is_idempotent(self):
        with self.create() as roster:
            self.step(roster)
        with PaperRoster(self.path) as roster:
            self.step(roster, 1000, discontinuity=True)
            self.step(roster, 1000, discontinuity=True)
            ledger = roster.journal_streams(SYMBOL)["S5"]
            self.assertIsNone(ledger.state()["active"])
            self.assertEqual(ledger.events(kinds=("PAPER_MISSED",))[0]["data"]["reason"], "OBSERVATION_GAP")
            self.assertEqual(len(ledger.events(kinds=("PAPER_FEED_GAP",))), 1)
            self.assertFalse(ledger.events(kinds=("PAPER_FILL",)))

    def test_gap_stop_uses_next_quote_and_keeps_loss_and_uncertainty(self):
        with self.create() as roster:
            self.step(roster)
            self.step(roster, 1000)
            ledger = roster.journal_streams(SYMBOL)["S5"]
            stop = ledger.state()["active"]["stop"]
        with PaperRoster(self.path) as roster:
            self.step(roster, 31001, round(stop - .10, 2), round(stop - .09, 2),
                      calendar=None, bars=[])
            outcome = roster.reports(at_ms=self.at + TF)["instruments"][SYMBOL]["portfolios"]["S5"]
            self.assertEqual(outcome["metrics"]["losses"], 1)
            self.assertEqual(outcome["completed_with_uncertain_path"], 1)
            ledger = roster.journal_streams(SYMBOL)["S5"]
            exits = ledger.events(kinds=("PAPER_PARTIAL",))
            self.assertEqual({event["at_ms"] for event in exits}, {self.at + 31001})
            self.assertTrue(all(abs(event["data"]["price"] - (stop - .11)) < 1e-8 for event in exits))
            self.assertEqual(outcome["stops_before_tp1"], 1)

    def test_session_spread_news_and_missing_data_fail_closed(self):
        with self.create() as roster:
            closed = self.step(roster, 0, session_open=False)
            self.assertEqual(closed["S5"]["decision"], "RISK_BLOCKED")
            spread = self.step(roster, 1000, self.entry - .08)
            self.assertEqual(spread["S5"]["decision"], "RISK_BLOCKED")
            news = self.step(roster, 2000, calendar=replace(self.news, source="https://other.invalid/events"))
            self.assertEqual(news["S5"]["decision"], "DATA_UNAVAILABLE")
            self.step(roster, 3000)
            self.step(roster, 4000, bars=[])
            ledger = roster.journal_streams(SYMBOL)["S5"]
            self.assertIsNone(ledger.state()["active"])
            self.assertFalse(ledger.events(kinds=("PAPER_FILL",)))
            self.assertEqual(ledger.events(kinds=("PAPER_MISSED",))[0]["data"]["reason"],
                             "FILL_RISK_RECHECK_BLOCKED")

    def test_external_champion_uses_exact_eligible_levels_and_separate_ledger(self):
        self.config["instruments"][0]["champion_core_sha256"] = PRODUCTION_CORE
        signal = ChampionSignal("event", "pine-plan", SYMBOL, TF, 1, self.at, self.at, self.at + TF,
                                self.entry, 139.60, (140.59, 141.09, 141.59), PRODUCTION_CORE,
                                .275, 1, "TEST:GOLD", self.at, self.entry - .01, self.entry)
        with self.create() as roster:
            self.step(roster, 0, champion_signal=replace(signal, timeframe_ms=TF * 2))
            streams = roster.journal_streams(SYMBOL)
            self.assertIsNone(streams["CHAMPION"].state()["active"])
            result = self.step(roster, 1000, champion_signal=signal)
            self.assertEqual(result["CHAMPION"]["decision"], "BUY_CANDIDATE")
            champion = streams["CHAMPION"].state()["active"]
            self.assertEqual(champion["source_plan_id"], "pine-plan")
            self.assertEqual(champion["targets"], list(signal.targets))
            self.assertNotEqual(champion["campaign_id"], streams["S5"].state()["active"]["campaign_id"])
            self.step(roster, 2000, champion_signal=signal)
            self.assertEqual(streams["CHAMPION"].state()["active"]["state"], "OPEN")

    def test_manifest_and_forward_thresholds_fail_closed(self):
        self.config["instruments"][0]["min_campaigns"] = 99
        with self.assertRaises(ValueError):
            self.create()
        self.config["instruments"][0]["min_campaigns"] = 100
        with self.create() as roster:
            with self.assertRaises(FileExistsError):
                register_roster(self.path, self.config, at_ms=self.at - 2000)
            self.assertEqual(roster.reports(at_ms=self.at)["instruments"][SYMBOL]["portfolios"]["S3"]["metrics"]["n"], 0)
        p = self.path / "roster.json"
        obj = read_json(p)
        obj["manifest"]["source_commit"] = "b" * 40
        p.write_text(canonical(obj), encoding="utf-8")
        with self.assertRaises(ValueError):
            PaperRoster(self.path)
