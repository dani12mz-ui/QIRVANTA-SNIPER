import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from qirvanta_ultimate.phase11_signals import PRODUCTION_CORE, SignalInbox, SignalPolicy, gateway_signature
from qirvanta_ultimate.risk import Quote


CLOSE = 1_790_000_100_000
TF = 900_000
RECEIPT = CLOSE + 1000
SECRET = b"synthetic-gateway-key-for-unit-tests-only"
POLICY = SignalPolicy("OANDA:XAUUSD", "XAUUSD.s", "TEST-MT5", 15)


def native_event(family=2, **changes):
    created = CLOSE - 10 if family != 2 else CLOSE
    plan = f"OANDA:XAUUSD|15|{family}|1|{created}|{CLOSE}"
    values = dict(schema="qirvanta.event.v1", core_sha256=PRODUCTION_CORE,
                  event_id=f"{plan}|NATIVE|NATIVE_SUBMITTED|{CLOSE}", event="NATIVE_SUBMITTED",
                  book="NATIVE", plan_id=plan, symbol="OANDA:XAUUSD", timeframe="15",
                  family=family, side=1, bar_open_time=CLOSE - TF, bar_close_time=CLOSE,
                  effective_time=CLOSE, known_time=CLOSE, created_time=created,
                  reference=2500, initial_sl=2498, tp1=2502, tp2=2504, tp3=2506,
                  atr_at_signal=3, advisory_only=True)
    return dict(values, **changes)


def body(*events):
    return json.dumps({"qirvanta": "advisory_only", "events": events}).encode()


def sample_quote(*_):
    return Quote("XAUUSD.s", 2500, 2500.05, RECEIPT - 10, "TEST-MT5")


class TradingViewIntakeTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "inbox.sqlite"

    def ingest(self, inbox, data, *, receipt=RECEIPT, observed=None, quote_at=sample_quote, signature=None):
        return inbox.ingest(data, signature or gateway_signature(SECRET, receipt, data), secret=SECRET,
                            received_ms=receipt, observed_ms=receipt if observed is None else observed,
                            quote_at=quote_at)[0]["status"]

    def eligible(self, inbox, at=RECEIPT):
        return inbox.eligible("XAUUSD.s", TF, at_ms=at, bar_close_ms=CLOSE)

    def test_authenticated_receipt_restart_and_expiry(self):
        data = body(native_event())
        with SignalInbox(self.path, [POLICY]) as inbox:
            self.assertEqual(self.ingest(inbox, data), "ADVISORY_READY")
            self.assertIsNone(self.eligible(inbox, RECEIPT - 1))
            self.assertEqual(self.eligible(inbox).quote_at_ms, RECEIPT - 10)
        with SignalInbox(self.path, [POLICY]) as inbox:
            self.assertEqual(self.ingest(inbox, data, receipt=RECEIPT + 1), "DUPLICATE_EVENT")
            self.assertEqual(self.eligible(inbox, RECEIPT + 1).received_ms, RECEIPT)
            self.assertIsNone(self.eligible(inbox, CLOSE + TF))
            self.assertEqual(inbox.journal.events(kinds=("SIGNAL_RECEIPT",))[0]["data"]["delivery_delay_ms"], 1000)

    def test_tampered_body_and_signed_receipt(self):
        initial = body(native_event())
        signature = gateway_signature(SECRET, RECEIPT, initial)
        with SignalInbox(self.path, [POLICY]) as inbox:
            for data, receipt in ((initial + b" ", RECEIPT), (initial, RECEIPT + 1)):
                with self.assertRaisesRegex(ValueError, "Untrusted"):
                    self.ingest(inbox, data, signature=signature, receipt=receipt)
            self.assertIsNone(self.eligible(inbox))

    def test_wrong_mapping_hash_bar_levels_and_malformed_json(self):
        invalid = (body(native_event(symbol="OTHER:XAUUSD")), body(native_event(core_sha256="0" * 64)),
                   body(native_event(known_time=CLOSE - 1)), body(native_event(initial_sl=2501)),
                   body(native_event(side=True)), body(native_event(tp1=float("inf"))),
                   b'{"qirvanta":"advisory_only","events":[],"events":[]}')
        with SignalInbox(self.path, [POLICY]) as inbox:
            for data in invalid:
                with self.subTest(data=data[:60]), self.assertRaises(ValueError):
                    self.ingest(inbox, data)
            self.assertEqual(inbox.journal.events(kinds=("INTAKE",)), [])

    def test_missing_receipt_quote_is_not_rearmed_by_a_retransmission(self):
        with SignalInbox(self.path, [POLICY]) as inbox:
            data = body(native_event())
            self.assertEqual(self.ingest(inbox, data, quote_at=lambda *_: None), "DATA_UNAVAILABLE")
            self.assertEqual(self.ingest(inbox, data, receipt=RECEIPT + 1), "DUPLICATE_EVENT")
            self.assertIsNone(self.eligible(inbox, RECEIPT + 1))

    def test_invalid_quote_and_stale_or_future_delivery(self):
        with SignalInbox(self.path, [POLICY]) as inbox:
            self.assertEqual(self.ingest(inbox, body(native_event(1)), receipt=CLOSE + 30_001),
                             "SIGNAL_EXPIRED")
            self.assertEqual(self.ingest(inbox, body(native_event(2)), receipt=CLOSE - 1),
                             "SIGNAL_EXPIRED")
            self.assertEqual(self.ingest(inbox, body(native_event(3)), quote_at=lambda *_: Quote(
                "XAUUSD.s", 2501, 2500, RECEIPT, "TEST-MT5")), "DATA_UNAVAILABLE")
            with self.assertRaisesRegex(ValueError, "future"):
                self.ingest(inbox, body(native_event(1)), observed=RECEIPT - 1)

    def test_native_fill_does_not_create_plan(self):
        with SignalInbox(self.path, [POLICY]) as inbox:
            self.assertEqual(self.ingest(inbox, body(native_event(event="NATIVE_ENTRY"))),
                             "NO_ADMISSION_EVENT")
            self.assertIsNone(self.eligible(inbox))

    def test_conflicting_admissions_quarantine(self):
        with SignalInbox(self.path, [POLICY]) as inbox:
            self.ingest(inbox, body(native_event()))
            self.assertEqual(self.ingest(inbox, body(native_event(tp1=2503))), "CONFLICTING_ADMISSION")
            self.assertIsNone(self.eligible(inbox))
            self.ingest(inbox, body(native_event(3)))
            self.assertIsNone(self.eligible(inbox))

    def test_immutable_policy(self):
        with SignalInbox(self.path, [POLICY]):
            pass
        with self.assertRaisesRegex(ValueError, "policy changed"):
            with SignalInbox(self.path, []):
                pass


if __name__ == "__main__":
    unittest.main()
