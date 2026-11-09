"""Authenticated local reconciliation of TradingView plan submissions."""
from dataclasses import asdict, dataclass
import hashlib
import hmac

from .common import decode_json, digest, finite, identifier, milliseconds, sha256
from .journal import Journal
from .risk import Quote
from .service import now_ms


PRODUCTION_CORE = "9afd1f74af1f487d0069a70e017b57d73a9fb079c5090de6466d0b6f8c32acc1"
MAX_BODY_BYTES = 65_536
ADMISSION = "NATIVE_SUBMITTED"


@dataclass(frozen=True)
class SignalPolicy:
    chart_symbol: str
    broker_symbol: str
    server: str
    minutes: int
    max_delivery_ms: int = 30_000
    max_quote_age_ms: int = 5_000

    def __post_init__(self):
        for value in (self.chart_symbol, self.broker_symbol, self.server):
            identifier(value)
            if "|" in value:
                raise ValueError("Invalid chart mapping")
        if (type(self.minutes) is not int or self.minutes not in (1, 5, 15, 30, 60, 240)
                or type(self.max_delivery_ms) is not int or not 0 < self.max_delivery_ms <= 30_000
                or type(self.max_quote_age_ms) is not int or not 0 < self.max_quote_age_ms <= 5_000):
            raise ValueError("Invalid chart timing policy")

    @property
    def timeframe_ms(self):
        return self.minutes * 60_000


@dataclass(frozen=True)
class ChampionSignal:
    event_id: str
    plan_id: str
    symbol: str
    timeframe_ms: int
    side: int
    bar_close_ms: int
    received_ms: int
    expires_ms: int
    reference: float
    stop: float
    targets: tuple
    core_sha256: str
    atr_at_signal: float
    family: int
    chart_symbol: str
    quote_at_ms: int
    receipt_bid: float
    receipt_ask: float


def gateway_signature(secret, received_ms, body):
    """Detached signature for a trusted gateway that authenticated the sender."""
    if not isinstance(secret, bytes) or len(secret) < 32:
        raise ValueError("Gateway HMAC key must contain at least 32 bytes")
    milliseconds(received_ms)
    if not isinstance(body, bytes) or not 0 < len(body) <= MAX_BODY_BYTES:
        raise ValueError("Invalid TradingView body size")
    prefix = f"qirvanta.tv-ingress.v1\n{received_ms}\n".encode("ascii")
    return hmac.new(secret, prefix + body, hashlib.sha256).hexdigest()


def _parse(event, policy):
    family, side = event.get("family"), event.get("side")
    if (event.get("schema") != "qirvanta.event.v1" or event.get("book") != "NATIVE"
            or event.get("advisory_only") is not True or event.get("core_sha256") != PRODUCTION_CORE
            or type(family) is not int or family not in (1, 2, 3)
            or type(side) is not int or side not in (-1, 1)):
        raise ValueError("Invalid production TradingView admission")
    opened, closed, created = (milliseconds(event.get(field)) for field in
                               ("bar_open_time", "bar_close_time", "created_time"))
    if (closed - opened != policy.timeframe_ms or created > closed
            or milliseconds(event.get("known_time")) != closed
            or milliseconds(event.get("effective_time")) != closed):
        raise ValueError("Invalid TradingView bar-close evidence")
    plan_id = f"{policy.chart_symbol}|{policy.minutes}|{family}|{side}|{created}|{closed}"
    event_id = f"{plan_id}|NATIVE|{ADMISSION}|{closed}"
    if event.get("plan_id") != plan_id or event.get("event_id") != event_id:
        raise ValueError("Invalid TradingView event identity")
    levels = tuple(finite(event.get(field), positive=True) for field in
                   ("initial_sl", "reference", "tp1", "tp2", "tp3"))
    if any((right - left) * side <= 0 for left, right in zip(levels, levels[1:])):
        raise ValueError("Invalid TradingView plan geometry")
    atr = finite(event.get("atr_at_signal"), positive=True)
    return dict(event_id=event_id, plan_id=plan_id, symbol=policy.broker_symbol,
                timeframe_ms=policy.timeframe_ms, side=side, bar_close_ms=closed,
                expires_ms=closed + policy.timeframe_ms, reference=levels[1], stop=levels[0],
                targets=levels[2:], core_sha256=PRODUCTION_CORE, atr_at_signal=atr, family=family,
                chart_symbol=policy.chart_symbol)


def _receipt_quote(quote, policy, received_ms):
    if (not isinstance(quote, Quote) or quote.symbol != policy.broker_symbol
            or quote.server != policy.server or type(quote.at_ms) is not int
            or not 0 <= received_ms - quote.at_ms <= policy.max_quote_age_ms):
        return False
    try:
        return 0 < finite(quote.bid) <= finite(quote.ask)
    except ValueError:
        return False


class SignalInbox:
    def __init__(self, path, policies):
        policies = tuple(policies)
        self.policies = {(p.chart_symbol, str(p.minutes)): p for p in policies}
        mappings = {(p.broker_symbol, p.minutes) for p in policies}
        if len(self.policies) != len(policies) or len(mappings) != len(policies):
            raise ValueError("Duplicate or ambiguous chart mappings")
        self.path = path
        self.policy_hash = digest([asdict(p) for _, p in sorted(self.policies.items())])

    def __enter__(self):
        self.journal = Journal(self.path)
        try:
            with self.journal.transaction():
                state = self.journal.state()
                if state is None:
                    self.journal.set_state({"signal_policy_sha256": self.policy_hash}, now_ms())
                elif state != {"signal_policy_sha256": self.policy_hash}:
                    raise ValueError("Signal inbox policy changed")
        except BaseException:
            self.journal.__exit__()
            raise
        return self

    def __exit__(self, *args):
        self.journal.__exit__(*args)

    def ingest(self, body, signature, *, secret, received_ms, observed_ms, quote_at):
        if not hmac.compare_digest(sha256(signature), gateway_signature(secret, received_ms, body)):
            raise ValueError("Untrusted TradingView gateway signature")
        if not 0 <= milliseconds(observed_ms) - received_ms <= 30_000:
            raise ValueError("Gateway receipt is future or too old for reconciliation")
        envelope = decode_json(body)
        if not isinstance(envelope, dict) or envelope.get("qirvanta") != "advisory_only":
            raise ValueError("Invalid TradingView alert wrapper")
        events = envelope.get("events")
        if not isinstance(events, list) or not 0 < len(events) <= 64:
            raise ValueError("Invalid TradingView event batch")
        admissions = []
        for event in events:
            if not isinstance(event, dict) or not isinstance(event.get("event"), str):
                raise ValueError("Invalid TradingView event record")
            if event["event"] != ADMISSION:
                continue
            chart, timeframe = event.get("symbol"), event.get("timeframe")
            if not isinstance(chart, str) or not isinstance(timeframe, str):
                raise ValueError("Invalid TradingView chart identity")
            policy = self.policies.get((chart, timeframe))
            if policy is None:
                raise ValueError("Unregistered TradingView chart")
            admissions.append((_parse(event, policy), policy))
        if not admissions:
            return [{"status": "NO_ADMISSION_EVENT"}]
        results = []
        with self.journal.transaction():
            for payload, policy in admissions:
                event_id = payload["event_id"]
                try:
                    first = self.journal.accept("tv:" + digest(event_id), payload, observed_ms)
                except ValueError:
                    status = "CONFLICTING_ADMISSION"
                    self.journal.append(status, observed_ms, {"event_id": event_id})
                else:
                    status = (self._record(payload, policy, received_ms, observed_ms, quote_at)
                              if first else "DUPLICATE_EVENT")
                results.append({"event_id": event_id, "status": status})
        return results

    def _record(self, payload, policy, received_ms, observed_ms, quote_at):
        lag = received_ms - payload["bar_close_ms"]
        status = "SIGNAL_EXPIRED"
        quote = None
        if 0 <= lag <= policy.max_delivery_ms and observed_ms < payload["expires_ms"]:
            quote = quote_at(payload["symbol"], received_ms)
            if _receipt_quote(quote, policy, received_ms) and quote.at_ms >= payload["bar_close_ms"]:
                status = "ADVISORY_READY"
            else:
                status = "DATA_UNAVAILABLE"
        receipt = dict(payload, received_ms=received_ms)
        self.journal.append("SIGNAL_RECEIPT", observed_ms, {
            "status": status, "delivery_delay_ms": lag, "signal": receipt,
            "receipt_quote": asdict(quote) if status == "ADVISORY_READY" else None,
        })
        return status

    def eligible(self, symbol, timeframe_ms, *, at_ms, bar_close_ms):
        """Resolve the one timely, uncontested plan known at the decision clock."""
        evidence = self.journal.events(since_ms=bar_close_ms,
                                       kinds=("SIGNAL_RECEIPT", "CONFLICTING_ADMISSION"))
        blocked, eligible = set(), []
        for row in evidence:
            if row["at_ms"] > at_ms:
                continue
            data = row["data"]
            if row["kind"] == "CONFLICTING_ADMISSION":
                blocked.add(data["event_id"])
                continue
            signal = data["signal"]
            if (signal["symbol"], signal["timeframe_ms"], signal["bar_close_ms"]) != (
                    symbol, timeframe_ms, bar_close_ms):
                continue
            eligible.append(data)
        if len(eligible) != 1:
            return None
        receipt = eligible[0]
        signal = receipt["signal"]
        if (receipt["status"] != "ADVISORY_READY" or signal["event_id"] in blocked
                or not signal["received_ms"] <= at_ms < signal["expires_ms"]):
            return None
        quote = receipt["receipt_quote"]
        return ChampionSignal(**dict(signal, targets=tuple(signal["targets"])),
                              quote_at_ms=quote["at_ms"], receipt_bid=quote["bid"],
                              receipt_ask=quote["ask"])
