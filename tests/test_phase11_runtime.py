"""End-to-end foreground and CLI tests with an isolated synthetic feed."""
from contextlib import contextmanager, redirect_stdout
from dataclasses import asdict
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from qirvanta_ultimate.common import canonical
from qirvanta_ultimate.market import MarketTick
from qirvanta_ultimate.mt5 import MT5Unavailable
from qirvanta_ultimate.phase11 import RuntimeConfig, foreground, main
from qirvanta_ultimate.phase11_portfolios import PaperRoster
from test_ultimate_service import SOURCE, SYMBOL, TF, profile


START = 1_790_000_100_000


class ForegroundTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        scratch = Path(directory.name)
        self.config = scratch / "runtime.json"
        self.root = scratch / "run"
        self.raw = {
            "roster": {
                "roster_id": "synthetic-integration-test", "source_commit": "a" * 40,
                "evidence_class": "PROSPECTIVE_PAPER", "instruments": [{
                    "profile": asdict(profile()), "dataset_id": "SYNTHETIC-UNIT-TEST",
                    "contract_spec_source": "SYNTHETIC-UNIT-TEST", "timeframe_ms": TF,
                    "history_cutoff_ms": START - TF, "start_ms": START, "end_ms": START + TF,
                    "initial_cash": 10_000,
                }],
            },
            "market": {"source": SOURCE, "server": "TEST_SERVER", "mode": "DEMO_READ_ONLY"},
        }
        self.config.write_text(canonical(self.raw), encoding="utf-8")

    def invoke(self, action, *options, config=True):
        args = [action, "--root", str(self.root)]
        if config:
            args += ["--config", str(self.config)]
        stream = StringIO()
        with redirect_stdout(stream):
            code = main(args + list(options))
        return code, json.loads(stream.getvalue())

    def register(self):
        with patch("qirvanta_ultimate.phase11.now_ms", return_value=START - 1000), patch(
                "qirvanta_ultimate.phase11_portfolios.now_ms", return_value=START - 1000):
            code, result = self.invoke("register")
        self.assertEqual((code, result["status"]), (0, "REGISTERED"))

    def test_cli_blocked_foreground_persists_status_without_attaching(self):
        self.register()
        with patch("qirvanta_ultimate.phase11.environment_status", return_value={
                "sdk_discoverable": False, "running_terminal_path_count": 0}), patch(
                "qirvanta_ultimate.phase11.connect_readonly") as connect:
            code, result = self.invoke("serve", "--max-polls", "1")
            connect.assert_not_called()
        self.assertEqual((code, result["status"], result["reason"]),
                         (2, "BLOCKED_EXTERNAL", "METATRADER5_SDK_UNAVAILABLE"))
        _, result = self.invoke("status")
        self.assertEqual(result["market"]["symbols"][SYMBOL]["status"], "MISSING_DATA")
        self.assertEqual(result["outbox"]["LOCAL_ONLY"], 1)
        _, result = self.invoke("report", config=False)
        self.assertTrue(all(p["complete_campaigns"] == 0
                            for p in result["instruments"][SYMBOL]["portfolios"].values()))

    def test_mock_poll_persists_quote_gap_and_four_paper_audits(self):
        self.register()
        cfg = RuntimeConfig.read(self.config)
        current = [START + 10]

        class Reader:
            def symbol(self, symbol):
                return None

            def market_tick(self, symbol, *, contract):
                if current[0] == START + 1010:
                    raise MT5Unavailable("INVALID_MT5_MARKET_TICK")
                return MarketTick(symbol, "TEST_SERVER", 140.08, 140.09, None, current[0], .01)

        @contextmanager
        def connect():
            yield Reader()

        def clock():
            return current[0]

        def advance(_):
            current[0] += 1000

        with patch("qirvanta_ultimate.phase11_portfolios.now_ms", side_effect=clock):
            result = foreground(self.root, cfg, max_polls=3, clock=clock, sleeper=advance,
                                connect_factory=connect)
        self.assertEqual(result["accepted_quotes"], 2)
        self.assertEqual(result["market"]["events"], {"ACCEPTED": 2, "MISSING_DATA": 1})
        with PaperRoster(self.root / "portfolios") as roster:
            for journal in roster.journal_streams(SYMBOL).values():
                events = journal.events(kinds=("PAPER_DECISION",))
                self.assertEqual(len(events), 1)
                self.assertEqual(len(events[0]["data"]["specialists"]), 12)
                self.assertEqual(len(journal.events(kinds=("PAPER_QUOTE_DECISION",))), 1)
                self.assertEqual(len(journal.events(kinds=("PAPER_FEED_GAP",))), 2)
                self.assertFalse(journal.events(kinds=("PAPER_FILL",)))

    def test_runtime_configuration_is_frozen_and_execution_mode_rejected(self):
        self.register()
        self.raw["market"]["cadence_s"] = 1.5
        self.config.write_text(canonical(self.raw), encoding="utf-8")
        self.assertEqual(self.invoke("status")[0], 2)
        self.raw["market"]["mode"] = "DEMO_EXECUTION"
        self.config.write_text(canonical(self.raw), encoding="utf-8")
        self.assertEqual(self.invoke("serve", "--max-polls", "1")[0], 2)
