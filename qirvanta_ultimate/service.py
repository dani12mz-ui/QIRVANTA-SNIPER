"""Local spool controller for the decision pipeline and paper journal."""
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import csv
import hashlib
from io import StringIO
from pathlib import Path
from time import time_ns

from research.data.loader import validate_bars
from research.risk.calculator import BrokerSpec

from .calendar import ArchivedCalendar, CalendarSnapshot, assess_news, source_host
from .common import decode_json, digest, finite, identifier, read_json, sha256
from .intelligence import AnalysisContext, AnalysisPolicy, analyze, observe
from .paper import PaperCosts, PaperProtocol, paper_step, register
from .risk import Quote, RiskPolicy, RiskState, TradePlan, assess_plan


def now_ms():
    return time_ns() // 1000000


ROOT = Path(__file__).resolve().parents[1]
ECONOMIC_SOURCES = (
    "research/data/loader.py", "research/features/causal.py", "research/strategies/library.py",
    "research/risk/calculator.py", "research/validation/statistics.py",
)


def implementation_hash():
    service = sorted(Path(__file__).parent.glob("*.py"))
    relevant = [path for path in service if path.name != "mt5.py"]
    relevant.extend(ROOT / name for name in ECONOMIC_SOURCES)
    return digest([(path.relative_to(ROOT).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
                   for path in relevant])


@dataclass(frozen=True)
class PaperProfile:
    source: str
    symbol: str
    quote_server: str
    account_currency: str
    contract_size: float
    point: float
    tick_size: float
    loss_per_tick_lot: float
    profit_per_tick_lot: float
    min_lot: float
    max_lot: float
    lot_step: float
    margin_per_lot: float
    stops_level_points: float
    fee_per_lot_per_fill: float
    max_spread: float
    max_entry_deviation: float
    risk_fraction: float
    calendar_source: str | None = None
    manual_pause: bool = False
    slippage_ticks: int = 2
    entry_kind: str = "MARKET_NEXT_QUOTE"
    mode: str = "PAPER"
    freeze_level_points: float = 0

    def __post_init__(self):
        for value in (self.source, self.symbol, self.quote_server, self.account_currency):
            identifier(value)
        for value in (self.contract_size, self.point, self.min_lot, self.max_lot,
                      self.lot_step, self.margin_per_lot, self.max_spread,
                      self.max_entry_deviation, self.risk_fraction):
            finite(value, positive=True)
        finite(self.stops_level_points)
        finite(self.freeze_level_points)
        if (min(self.stops_level_points, self.freeze_level_points) < 0
                or self.max_lot < self.min_lot or not 0 < self.risk_fraction <= .01
                or self.entry_kind not in ("MARKET_NEXT_QUOTE", "LIMIT") or self.mode != "PAPER"
                or type(self.manual_pause) is not bool):
            raise ValueError("Invalid local paper profile")
        if self.calendar_source is not None:
            source_host(self.calendar_source)
        self.costs()

    def costs(self):
        return PaperCosts(self.tick_size, self.loss_per_tick_lot, self.profit_per_tick_lot,
                          self.fee_per_lot_per_fill, self.slippage_ticks)

    def paper_specification(self, at_ms):
        # These are frozen paper assumptions for a local wallet only.
        return BrokerSpec(symbol=self.symbol, server=self.quote_server, account_currency=self.account_currency,
                          verified=True, evidence="PAPER_PROFILE_ASSUMPTIONS", verified_at_ms=at_ms,
                          contract_size=self.contract_size, point=self.point, tick_size=self.tick_size,
                          tick_value_loss_account_ccy=self.loss_per_tick_lot, volume_min=self.min_lot,
                          volume_max=self.max_lot, volume_step=self.lot_step,
                          stops_level_points=self.stops_level_points, freeze_level_points=self.freeze_level_points,
                          commission_round_trip_per_lot=2 * self.fee_per_lot_per_fill,
                          currency_conversion_verified=True)


def create_protocol(journal, profile, draft, *, at_ms):
    if not isinstance(draft, dict) or draft.get("evidence_class", "PROSPECTIVE_DECLARED_SOURCE") != "PROSPECTIVE_DECLARED_SOURCE":
        raise ValueError("Expected a prospective local paper draft")
    fields = dict(draft)
    fields["families"] = tuple(fields["families"])
    protocol = PaperProtocol(source=profile.source, symbol=profile.symbol,
                             profile_hash=digest(asdict(profile)), implementation_hash=implementation_hash(),
                             **fields)
    register(journal, protocol, now_ms=at_ms)
    return {"protocol_id": protocol.protocol_id, "protocol_hash": digest(asdict(protocol)),
            "status": "REGISTERED", "start_ms": protocol.start_ms}


def _verified(path, expected, limit=16777216):
    sha256(expected)
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Local artifact exceeds size limit")
    if hashlib.sha256(data).hexdigest() != expected:
        raise ValueError("Local observation hash mismatch")
    return data


def verified_bars(path, expected):
    data = _verified(Path(path), expected)
    rows = csv.DictReader(StringIO(data.decode("utf-8-sig"), newline=""))
    columns = ("open", "high", "low", "close")
    needed = set(columns) | {"time", "time_close"}
    fields = rows.fieldnames
    if fields is None or len(set(fields)) != len(fields) or not needed.issubset(fields):
        raise ValueError("Missing or duplicated OHLC CSV field")
    bars = []
    for row in rows:
        if None in row or any(row[k] is None for k in needed):
            raise ValueError("Malformed OHLC row")
        bar = {name: float(row[name]) for name in columns}
        bar.update(time=int(row["time"]), time_close=int(row["time_close"]))
        bars.append(bar)
    validate_bars(bars)
    return bars


def _artifact(root, expected, suffix):
    root = Path(root).resolve()
    path = (root / (sha256(expected) + suffix)).resolve()
    if not path.is_relative_to(root):
        raise ValueError("Local artifact is outside the configured directory")
    return path


def _calendar(root, expected, approved_source):
    if root is None or expected is None or approved_source is None:
        return None
    try:
        path = _artifact(root, expected, ".json")
        result = CalendarSnapshot.from_object(decode_json(_verified(path, expected, 1048576)))
        if result.source != approved_source:
            return None
        return ArchivedCalendar((result,))
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _risk_state(journal, profile, state, at_ms, *, recheck=False):
    active = state["active"]
    occupied = active is not None and not recheck
    now = datetime.fromtimestamp(at_ms / 1000, timezone.utc)
    cash_events = (e for e in journal.events(since_ms=at_ms - 7 * 86400000,
                                             kinds=("PAPER_FILL", "PAPER_PARTIAL")) if e["at_ms"] <= at_ms)
    day, week = 0.0, 0.0
    for event in cash_events:
        dt = datetime.fromtimestamp(event["at_ms"] / 1000, timezone.utc)
        if dt.isocalendar()[:2] == now.isocalendar()[:2]:
            week += event["data"]["cash_delta"]
        if dt.date() == now.date():
            day += event["data"]["cash_delta"]
    cash = state["cash"]
    reserved = active["estimated_loss_cash"] if occupied else 0.0
    free_margin = max(0.0, cash - (active["lots"] * profile.margin_per_lot if occupied else 0))
    return RiskState(cash, free_margin, profile.account_currency, reserved, max(0., -day),
                     max(0., -week), int(occupied), state.get("cooldown_until_ms", 0))


def _guardian(profile, state, plan, quote, now, *, session_open, risk, overrides=None):
    spec = profile.paper_specification(now)
    policy = RiskPolicy(max_spread=profile.max_spread, max_entry_deviation=profile.max_entry_deviation,
                        risk_fraction=profile.risk_fraction, slippage_ticks_each_way=profile.slippage_ticks,
                        max_quote_age_ms=state["protocol"]["max_quote_age_ms"], **(overrides or {}))
    proposed = TradePlan(plan["plan_id"], profile.symbol, plan["side"], plan["entry"], plan["stop"],
                         tuple(plan["targets"]), plan["issued_ms"], plan["expires_ms"])
    return assess_plan(proposed, quote, spec, risk, policy, now_ms=now,
                       margin_for_lots=lambda lots: profile.margin_per_lot * lots,
                       tick_value_profit_account_ccy=profile.profit_per_tick_lot,
                       manual_pause=profile.manual_pause or state.get("operator_paused", False),
                       session_open=session_open)


def _unavailable(profile, protocol, now):
    decision = {"symbol": profile.symbol, "timeframe_ms": protocol["timeframe_ms"], "as_of_ms": now,
                "bar_close_ms": None, "state": "DATA_UNAVAILABLE", "reason": "BAR_ARTIFACT_UNAVAILABLE",
                "inference_provider": None, "broker_orders": False,
                "specialists": {"audit": {"status": "UNAVAILABLE", "facts": {"source": profile.source}}}}
    decision["trace_id"] = digest(decision)
    return decision


def process_packet(journal, profile, packet, *, bars_root, calendar_root=None, at_ms=None):
    if set(packet) - {"quote", "source", "session_open", "bars_sha256", "calendar_sha256"}:
        raise ValueError("Unexpected quote packet fields")
    quote = Quote(**packet["quote"])
    if packet["source"] != profile.source or quote.server != profile.quote_server:
        raise ValueError("Paper packet source mismatch")
    if type(packet["session_open"]) is not bool:
        raise ValueError("Explicit local session state required")
    now = now_ms() if at_ms is None else at_ms
    bars_hash = sha256(packet["bars_sha256"])
    news = _calendar(calendar_root, packet.get("calendar_sha256"), profile.calendar_source)

    def evaluate(state, q, as_of):
        protocol = state["protocol"]
        try:
            bars = verified_bars(_artifact(bars_root, bars_hash, ".csv"), bars_hash)
            frame = observe(bars, symbol=q.symbol, source=profile.source, source_hash=bars_hash,
                            as_of_ms=q.at_ms, families=tuple(protocol["families"]), mintick=profile.tick_size)
            if frame.timeframe_ms != protocol["timeframe_ms"]:
                raise ValueError("Wrong data timeframe")
        except (OSError, ValueError, KeyError):
            return _unavailable(profile, protocol, as_of), profile.costs()
        frame = replace(frame, as_of_ms=as_of)
        policy = AnalysisPolicy(research_candidates=True, max_quote_age_ms=protocol["max_quote_age_ms"],
                                max_bar_age_ms=protocol["timeframe_ms"])
        current_risk = _risk_state(journal, profile, state, as_of)
        guardian = lambda plan, qq, t: _guardian(profile, state, plan, qq, t,
                                                session_open=packet["session_open"], risk=current_risk)
        pause = profile.manual_pause or state.get("operator_paused", False)
        return analyze(AnalysisContext(frame, policy, q, news, pause, guardian)), profile.costs()

    def recheck(plan, q, state, as_of):
        pause = profile.manual_pause or state.get("operator_paused", False)
        risk = assess_news(news, as_of_ms=as_of, manual_pause=pause)
        if risk.blocked:
            return False
        current_risk = _risk_state(journal, profile, state, as_of, recheck=True)
        verified = _guardian(profile, state, plan, q, as_of, risk=current_risk,
                             session_open=packet["session_open"])
        return verified.allowed and verified.lots >= plan["lots"]

    return paper_step(journal, f"quote:{quote.at_ms}", quote, now_ms=now,
                      implementation_hash=implementation_hash(), profile_hash=digest(asdict(profile)),
                      evaluate=evaluate, recheck=recheck, entry_kind=profile.entry_kind,
                      input_hash=digest(packet), session_open=packet["session_open"],
                      clock=now_ms if at_ms is None else None)


def load_profile(path):
    return PaperProfile(**read_json(path))
