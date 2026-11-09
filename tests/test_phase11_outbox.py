"""Local SQLite and provider-contract tests. All network access is mocked."""
import http.client
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qirvanta_ultimate.journal import Journal
from qirvanta_ultimate.phase11_outbox import HOST, LEASE_MS, Outbox, Telegram


NOW = 1_850_000_000_000
PROFILE = {"strategy": "S3", "symbol": "XAUUSD.s", "timeframe": "M15"}
ENV = {
    "QIRVANTA_TELEGRAM_ENABLED": "1",
    "QIRVANTA_TELEGRAM_BOT_TOKEN": "123456:" + "a" * 30,
    "QIRVANTA_TELEGRAM_CHAT_ID": "-123",
    "QIRVANTA_TELEGRAM_AUTHORIZED_CHAT_ID": "-123",
}
PROTOCOL = {"protocol_id": "fixture", "strategy_id": "S3", "symbol": "XAUUSD.s", "timeframe_ms": 900_000}


def response(connection, status=200, body=None):
    receipt = {"ok": True, "result": {"message_id": 42, "chat": {"id": -123}}}
    connection.getresponse.return_value.status = status
    connection.getresponse.return_value.read.return_value = json.dumps(
        receipt if body is None else body).encode()


class OutboxTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.path = root / "outbox.sqlite"
        self.journal_path = root / "journal.sqlite"
        self.network = self.enterContext(patch.object(http.client, "HTTPSConnection"))
        self.connection = self.network.return_value
        self.telegram = Telegram.from_env(ENV)

    def seed(self, journal, *, origin="PROSPECTIVE_PAPER"):
        with journal.transaction():
            journal.set_state({"protocol": PROTOCOL | {"evidence_class": origin}}, NOW)
            journal.append("LOCAL_NOTIFICATION", NOW, {"text": "Кандидат за продажба"})

    def queue(self, outbox):
        with Journal(self.journal_path) as journal:
            self.seed(journal)
            return outbox.sync_journal(journal, "fixture", **PROFILE, transport=self.telegram, external_after_ms=NOW)

    def test_local_dedup_and_reopen(self):
        with Outbox(self.path) as box:
            self.assertTrue(box.enqueue("event", "Информация", NOW))
            self.assertFalse(box.enqueue("event", "Информация", NOW))
            with self.assertRaisesRegex(ValueError, "conflicting"):
                box.enqueue("event", "Различно", NOW)
        with Outbox(self.path) as box:
            self.assertEqual(box.status()["LOCAL_ONLY"], 1)
            self.assertIsNone(box.deliver_one(self.telegram, NOW))
            self.assertEqual(box.messages()[0]["text"], "Информация")
        self.network.assert_not_called()

    def test_local_mirror_atomic_cursor(self):
        with Journal(self.journal_path) as journal, Outbox(self.path) as box:
            self.seed(journal)
            self.assertEqual(box.sync_journal(journal, "fixture", **PROFILE, limit=1),
                             {"scanned": 1, "added": 0, "sequence": 1})
            with journal.transaction():
                journal.append("PAPER_MISSED", NOW, {"reason": "ENTRY_EXPIRED"})
            with self.assertRaises(KeyError):
                box.sync_journal(journal, "fixture", **PROFILE)
            self.assertEqual(box.messages(), [])
            self.assertEqual(box.sync_journal(journal, "fixture", **PROFILE, limit=1)["added"], 1)
            with self.assertRaisesRegex(ValueError, "stream changed"):
                box.sync_journal(journal, "fixture", **(PROFILE | {"strategy": "S2"}))
            self.assertIn("PAPER · Симулиран режим", box.messages()[0]["text"])
        self.network.assert_not_called()

    def test_health_transition_dedup_survives_restart_and_preserves_recovery(self):
        with Outbox(self.path) as box:
            self.assertTrue(box.enqueue_transition("market:XAU", ["MISSING"], "lost", "Липсва връзка", NOW))
        with Outbox(self.path) as box:
            self.assertFalse(box.enqueue_transition("market:XAU", ["MISSING"], "again", "Липсва връзка", NOW + 1))
            self.assertTrue(box.enqueue_transition("market:XAU", ["ACCEPTED"], "back", "Връзката е възстановена",
                                                  NOW + 2))
            self.assertTrue(box.enqueue_transition("market:XAU", ["MISSING"], "new", "Липсва връзка", NOW + 3))
            self.assertEqual(box.status()["LOCAL_ONLY"], 3)
        self.network.assert_not_called()

    def test_live_policy_never_promotes_mirrored_or_synthetic_notice(self):
        with Journal(self.journal_path) as journal, Outbox(self.path) as box:
            self.seed(journal, origin="SYNTHETIC_TEST")
            with self.assertRaisesRegex(ValueError, "prospective"):
                box.sync_journal(journal, "fixture", **PROFILE, transport=self.telegram, external_after_ms=NOW)
            box.sync_journal(journal, "fixture", **PROFILE)
            with journal.transaction():
                journal.set_state({"protocol": PROTOCOL | {"evidence_class": "PROSPECTIVE_PAPER"}}, NOW)
            result = box.sync_journal(journal, "fixture", **PROFILE, transport=self.telegram, external_after_ms=NOW)
            self.assertEqual(result["added"], 0)
            self.assertEqual(box.status()["LOCAL_ONLY"], 1)
        self.network.assert_not_called()

    def test_stream_alias_cannot_promote_or_duplicate_local_notice(self):
        with Journal(self.journal_path) as journal, Outbox(self.path) as box:
            self.seed(journal)
            self.assertEqual(box.sync_journal(journal, "fixture", **PROFILE)["added"], 1)
            self.assertEqual(box.sync_journal(journal, "local-alias", **PROFILE)["added"], 0)
            with self.assertRaisesRegex(ValueError, "conflicting"):
                box.sync_journal(journal, "alias", **PROFILE, transport=self.telegram, external_after_ms=NOW)
            with self.assertRaisesRegex(ValueError, "registered strategy"):
                box.sync_journal(journal, "alias", **(PROFILE | {"symbol": "BTCUSD"}),
                                 transport=self.telegram, external_after_ms=NOW)
            self.assertEqual(box.status()["QUEUED"], 0)
        self.network.assert_not_called()

    def test_local_utc_summaries_use_complete_campaigns_and_keep_losses(self):
        end_ms = 1_793_836_800_000  # 2026-11-05 00:00:00Z
        with Journal(self.journal_path) as journal, Outbox(self.path) as box:
            self.seed(journal, origin="SYNTHETIC_TEST")
            with journal.transaction():
                journal.append("PAPER_COMPLETE", end_ms - 1, {"cash_net": -17.25})
                journal.append("PAPER_PARTIAL", end_ms - 1, {"cash_delta": 100})
                journal.append("PAPER_COMPLETE", end_ms, {"cash_net": 10})
            options = dict(PROFILE, period="day", end_date="2026-11-05", now_ms=NOW)
            result = box.summary(journal, **options)
            self.assertEqual(result["complete_campaigns"], 1)
            self.assertFalse(box.summary(journal, **options)["added"])
            self.assertIn("-17.25", box.messages()[0]["text"])
            with self.assertRaisesRegex(ValueError, "ending on Monday"):
                box.summary(journal, **(options | {"period": "week"}))
            result = box.summary(journal, **(options | {"period": "week", "end_date": "2026-11-09"}))
            self.assertEqual(result["complete_campaigns"], 2)
            self.assertIn("-7.25", box.messages()[0]["text"])
        self.network.assert_not_called()

    def test_missing_invalid_and_unapproved_configuration(self):
        for env in ({}, ENV | {"QIRVANTA_TELEGRAM_ENABLED": "0"},
                    ENV | {"QIRVANTA_TELEGRAM_BOT_TOKEN": "invalid"},
                    ENV | {"QIRVANTA_TELEGRAM_AUTHORIZED_CHAT_ID": "-456"},
                    ENV | {"QIRVANTA_TELEGRAM_CHAT_ID": "@someone"}):
            with self.assertRaises(ValueError) as error:
                Telegram.from_env(env)
            self.assertNotIn(ENV["QIRVANTA_TELEGRAM_BOT_TOKEN"], str(error.exception))
        self.network.assert_not_called()

    def test_acknowledged_delivery_is_persisted_and_unique(self):
        response(self.connection)
        with Outbox(self.path) as box:
            self.queue(box)
            self.assertEqual(box.deliver_one(self.telegram, NOW)["state"], "DELIVERED")
            self.assertIsNone(box.deliver_one(self.telegram, NOW))
            self.network.assert_called_once_with(HOST, timeout=5)
            args = self.connection.request.call_args.args
            self.assertEqual(args[0], "POST")
            self.assertEqual(json.loads(args[2])["chat_id"], -123)
            self.assertIn("Кандидат за продажба", json.loads(args[2])["text"])
        with Outbox(self.path) as box:
            self.assertEqual(box.messages()[0]["provider_id"], 42)
            self.assertNotIn("binding", box.messages()[0])

    def test_connection_failure_retry_backoff_and_success(self):
        response(self.connection)
        self.connection.connect.side_effect = [OSError("private"), None]
        with Outbox(self.path) as box:
            self.queue(box)
            self.assertEqual(box.deliver_one(self.telegram, NOW)["state"], "RETRY")
            self.connection.request.assert_not_called()
            self.assertIsNone(box.deliver_one(self.telegram, NOW + 999))
            self.assertEqual(box.deliver_one(self.telegram, NOW + 1000)["state"], "DELIVERED")
            self.assertEqual(box.messages()[0]["attempts"], 2)

    def test_rate_limit_retry_is_bounded(self):
        response(self.connection, 429, {"ok": False, "error_code": 429, "parameters": {"retry_after": 7}})
        with Outbox(self.path) as box:
            self.queue(box)
            for second in (0, 7, 14):
                at = NOW + second * 1000
                self.assertEqual(box.deliver_one(self.telegram, at)["reason"], "RATE_LIMITED")
                self.assertIsNone(box.deliver_one(self.telegram, at + 6999))
            self.assertEqual(box.deliver_one(self.telegram, NOW + 21000)["reason"], "RETRY_LIMIT")
            self.assertIsNone(box.deliver_one(self.telegram, NOW + 42000))
            self.assertEqual(self.connection.request.call_count, 4)

    def test_provider_rejects_invalid_credentials_without_retry(self):
        response(self.connection, 401, {"ok": False, "error_code": 401, "description": "private"})
        with Outbox(self.path) as box:
            self.queue(box)
            self.assertEqual(box.deliver_one(self.telegram, NOW)["reason"], "PROVIDER_REJECTED")
            self.assertNotIn("private", json.dumps(box.messages()))
            self.assertIsNone(box.deliver_one(self.telegram, NOW + 9999))

    def test_post_timeout_is_ambiguous_and_never_retried(self):
        self.connection.getresponse.side_effect = TimeoutError("private")
        with Outbox(self.path) as box:
            self.queue(box)
            self.assertEqual(box.deliver_one(self.telegram, NOW)["state"], "UNKNOWN")
            self.assertIsNone(box.deliver_one(self.telegram, NOW + 9999))
            self.connection.request.assert_called_once()

    def test_mismatched_acknowledgement_is_unknown(self):
        response(self.connection, 200, {"ok": True, "result": {"message_id": 42, "chat": {"id": -456}}})
        with Outbox(self.path) as box:
            self.queue(box)
            self.assertEqual(box.deliver_one(self.telegram, NOW)["reason"], "RECEIPT_MISMATCH")
            self.assertIsNone(box.messages()[0]["provider_id"])

    def test_interrupted_claim_stays_unknown_after_reopen(self):
        self.connection.getresponse.side_effect = KeyboardInterrupt()
        with Outbox(self.path) as box:
            self.queue(box)
            with self.assertRaises(KeyboardInterrupt):
                box.deliver_one(self.telegram, NOW)
            self.assertEqual(box.status()["IN_FLIGHT"], 1)
        with Outbox(self.path) as box:
            self.assertIsNone(box.deliver_one(self.telegram, NOW + LEASE_MS))
            self.assertEqual(box.messages()[0]["reason"], "INTERRUPTED_SEND")
            self.connection.request.assert_called_once()

    def test_changed_destination_and_other_database_fail_closed(self):
        new = Telegram.from_env(ENV | {"QIRVANTA_TELEGRAM_CHAT_ID": "-456",
                                      "QIRVANTA_TELEGRAM_AUTHORIZED_CHAT_ID": "-456"})
        with Outbox(self.path) as box:
            self.queue(box)
            self.assertEqual(box.deliver_one(new, NOW)["reason"], "DESTINATION_CHANGED")
        with self.assertRaisesRegex(ValueError, "another database"):
            Outbox(self.journal_path)
        self.network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
