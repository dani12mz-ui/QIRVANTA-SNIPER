"""Quote-driven paper execution and prospective protocol enforcement."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from math import isclose
from statistics import mean

from research.strategies.library import FAMILIES
from research.validation.statistics import block_bootstrap, cash_metrics, wilson

from .common import digest, finite, identifier, milliseconds, sha256
from .notifications import record_notification, record_paper_fill


@dataclass(frozen=True)
class PaperProtocol:
    protocol_id: str
    source: str
    symbol: str
    timeframe_ms: int
    families: tuple[str, ...]
    implementation_hash: str
    profile_hash: str
    history_cutoff_ms: int
    start_ms: int
    end_ms: int
    initial_cash: float
    min_campaigns: int = 100
    min_profit_factor: float = 1.1
    min_net_r: float = .1
    max_drawdown_fraction: float = .1
    max_quote_age_ms: int = 5000
    max_hold_ms: int = 43200000
    evidence_class: str = "PROSPECTIVE_DECLARED_SOURCE"

    def __post_init__(self):
        for name in (self.protocol_id, self.source, self.symbol):
            identifier(name)
        for pin in (self.implementation_hash, self.profile_hash):
            sha256(pin)
        for time in (self.history_cutoff_ms, self.start_ms, self.end_ms):
            milliseconds(time)
        if self.timeframe_ms not in (300000, 900000, 1800000) or not self.history_cutoff_ms < self.start_ms < self.end_ms:
            raise ValueError("Invalid prospective protocol interval")
        if (not self.families or len(set(self.families)) != len(self.families)
                or any(family not in FAMILIES[:9] for family in self.families)):
            raise ValueError("Specify distinct experimental families")
        for value in (self.initial_cash, self.min_profit_factor, self.min_net_r, self.max_drawdown_fraction):
            finite(value, positive=True)
        if self.min_profit_factor <= 1 or self.max_drawdown_fraction >= 1:
            raise ValueError("Invalid promotion thresholds")
        if type(self.min_campaigns) is not int or self.min_campaigns < 30:
            raise ValueError("Invalid minimum campaign count")
        for value in (self.max_hold_ms, self.max_quote_age_ms):
            if type(value) is not int or value <= 0:
                raise ValueError("Invalid execution latency policy")
        if self.evidence_class not in ("PROSPECTIVE_DECLARED_SOURCE", "SYNTHETIC_TEST"):
            raise ValueError("Unknown evidence class")


def register(journal, protocol, *, now_ms):
    milliseconds(now_ms)
    if not now_ms < protocol.start_ms:
        raise ValueError("Register prospectively, before the first eligible observation")
    with journal.transaction():
        if journal.state() is not None:
            raise ValueError("A journal supports one immutable paper protocol")
        state = {"protocol": asdict(protocol), "registered_ms": now_ms, "active": None,
                 "cash": protocol.initial_cash, "last_quote_ms": None}
        journal.append("PAPER_REGISTERED", now_ms, {"protocol_hash": digest(state["protocol"]),
                                                  "evidence_class": protocol.evidence_class})
        journal.set_state(state, now_ms)


@dataclass(frozen=True)
class PaperCosts:
    tick_size: float
    loss_per_tick_lot: float
    profit_per_tick_lot: float
    fee_per_lot_per_fill: float
    slippage_ticks: int

    def __post_init__(self):
        for value in (self.tick_size, self.loss_per_tick_lot, self.profit_per_tick_lot):
            finite(value, positive=True)
        finite(self.fee_per_lot_per_fill)
        if self.fee_per_lot_per_fill < 0 or type(self.slippage_ticks) is not int or self.slippage_ticks < 0:
            raise ValueError("Invalid paper costs")


def _quote_ok(quote, protocol, now_ms):
    milliseconds(quote.at_ms)
    milliseconds(now_ms)
    finite(quote.bid, positive=True)
    finite(quote.ask, positive=True)
    if (quote.symbol != protocol["symbol"] or quote.bid > quote.ask
            or not 0 <= now_ms - quote.at_ms <= protocol["max_quote_age_ms"]
            or not protocol["start_ms"] <= quote.at_ms <= now_ms < protocol["end_ms"]):
        raise ValueError("Paper quote is crossed, stale, future, outside protocol or wrong symbol")


def _submit(journal, state, decision, *, costs, now_ms, entry_kind):
    p = state["protocol"]
    if decision["state"] not in ("BUY_CANDIDATE", "SELL_CANDIDATE"):
        return
    if state["active"] is not None:
        journal.append("PAPER_SKIPPED", now_ms, {"reason": "EXPOSURE_RESERVED", "trace_id": decision["trace_id"]})
        return
    mgmt = decision["specialists"]["management"]["facts"]
    risk = decision["specialists"]["risk"]
    if risk["status"] != "OK" or mgmt["family"] not in p["families"]:
        raise ValueError("Paper admission requires a qualifying guardian result and frozen family")
    if mgmt["issued_ms"] < p["start_ms"] or now_ms >= mgmt["expires_ms"]:
        journal.append("PAPER_SKIPPED", now_ms, {"reason": "PLAN_OUTSIDE_PROTOCOL_OR_EXPIRED",
                                                 "trace_id": decision["trace_id"]})
        return
    if entry_kind not in ("MARKET_NEXT_QUOTE", "LIMIT"):
        raise ValueError("Unsupported paper entry kind")
    if not journal.accept("plan:" + mgmt["plan_id"], mgmt, now_ms):
        return
    sizes = tuple(risk["facts"]["target_lots"])
    lots = risk["facts"]["lots"]
    if len(sizes) != 3 or any(finite(q, positive=True) <= 0 for q in sizes) or not isclose(sum(sizes), lots):
        raise ValueError("Invalid guardian quantity allocation")
    state["active"] = {**mgmt, "state": "PENDING", "submitted_ms": now_ms, "entry_kind": entry_kind,
                       "remaining": list(sizes), "lots": lots, "cash_paid": 0.0,
                       "trace_id": decision["trace_id"],
                       "costs": asdict(costs), "estimated_loss_cash": risk["facts"]["estimated_loss_cash"]}
    journal.append("PAPER_PENDING", now_ms, {"plan_id": mgmt["plan_id"], "trace_id": decision["trace_id"],
                                            "entry_kind": entry_kind, "lots": lots})


def _part(journal, state, position, i, price, reason, quote_ms):
    finite(price, positive=True)
    quantity = position["remaining"][i]
    if quantity <= 0:
        return
    cost = position["costs"]
    delta = (price - position["fill_price"]) * position["side"]
    multiplier = cost["profit_per_tick_lot"] if delta > 0 else cost["loss_per_tick_lot"]
    fee = cost["fee_per_lot_per_fill"] * quantity
    cash = delta / cost["tick_size"] * multiplier * quantity - fee
    position["remaining"][i] = 0.0
    position["cash_paid"] += cash
    state["cash"] += cash
    journal.append("PAPER_PARTIAL", quote_ms, {"plan_id": position["plan_id"], "component": i + 1,
                                              "quantity": quantity, "price": price, "fee": fee,
                                              "cash_delta": cash, "reason": reason})
    if reason.startswith("TP"):
        record_paper_fill(journal, position, quote_ms, reason, price, quantity)


def _advance(journal, state, quote, *, entry_allowed, session_open):
    last = state["last_quote_ms"]
    if last is not None and quote.at_ms <= last:
        raise ValueError("Paper quotes must advance strictly")
    state["last_quote_ms"] = quote.at_ms
    pos = state["active"]
    if not pos:
        return
    if pos["state"] == "OPEN" and not session_open:
        return
    side = pos["side"]
    closing = quote.bid if side == 1 else quote.ask
    cost = pos["costs"]
    slip = cost["tick_size"] * cost["slippage_ticks"] * side
    if pos["state"] == "PENDING":
        if quote.at_ms <= pos["submitted_ms"]:
            return
        if quote.at_ms >= pos["expires_ms"] or not entry_allowed:
            journal.append("PAPER_MISSED", quote.at_ms, {"plan_id": pos["plan_id"],
                           "reason": "UNFILLED_LIMIT" if quote.at_ms >= pos["expires_ms"] and pos["entry_kind"] == "LIMIT"
                           else "ENTRY_EXPIRED" if quote.at_ms >= pos["expires_ms"] else "FILL_RISK_RECHECK_BLOCKED"})
            state["active"] = None
            return
        executable = quote.ask if side == 1 else quote.bid
        if pos["entry_kind"] == "LIMIT" and (executable - pos["entry"]) * side > 0:
            return
        fill = executable + slip if pos["entry_kind"] == "MARKET_NEXT_QUOTE" else pos["entry"]
        if (closing - pos["stop"]) * side <= 0 or (pos["targets"][0] - fill) * side <= 0:
            journal.append("PAPER_MISSED", quote.at_ms, {"plan_id": pos["plan_id"], "reason": "GAP_INVALIDATION"})
            state["active"] = None
            return
        fee = pos["lots"] * cost["fee_per_lot_per_fill"]
        risk_per_lot = (fill - pos["stop"] + slip) * side / cost["tick_size"] * cost["loss_per_tick_lot"]
        risk_cash = pos["lots"] * risk_per_lot + 2 * fee
        if risk_cash > pos["estimated_loss_cash"] + 1e-9:
            journal.append("PAPER_MISSED", quote.at_ms, {"plan_id": pos["plan_id"], "reason": "GAP_RISK_CAP"})
            state["active"] = None
            state["cooldown_until_ms"] = quote.at_ms + state["protocol"]["timeframe_ms"]
            return
        pos.update(state="OPEN", fill_price=fill, fill_ms=quote.at_ms, cash_paid=-fee,
                   initial_cash_risk=risk_cash, excursion_min=0.0, excursion_max=0.0)
        state["cash"] -= fee
        journal.append("PAPER_FILL", quote.at_ms, {"plan_id": pos["plan_id"], "price": fill,
                                                  "quantity": pos["lots"], "fee": fee, "cash_delta": -fee})
        record_paper_fill(journal, pos, quote.at_ms, "ENTRY", fill, pos["lots"])
        return
    movement = (closing - pos["fill_price"]) * side
    pos["excursion_min"] = min(pos["excursion_min"], movement)
    pos["excursion_max"] = max(pos["excursion_max"], movement)
    stop = (closing - pos["stop"]) * side <= 0
    time_stop = quote.at_ms - pos["fill_ms"] >= state["protocol"]["max_hold_ms"]
    if stop or time_stop:
        reason = "STOP" if stop else "TIME_STOP"
        if stop:
            state["cooldown_until_ms"] = quote.at_ms + state["protocol"]["timeframe_ms"]
        quantity = sum(pos["remaining"])
        for i in range(3):
            _part(journal, state, pos, i, closing - slip, reason, quote.at_ms)
        record_paper_fill(journal, pos, quote.at_ms, reason, closing - slip, quantity)
    else:
        for i, target in enumerate(pos["targets"]):
            if (closing - target) * side >= 0 and pos["remaining"][i] > 0:
                _part(journal, state, pos, i, target, f"TP{i + 1}", quote.at_ms)
                if i == 0:
                    pos["stop"] = pos["fill_price"]
    if not any(pos["remaining"]):
        journal.append("PAPER_COMPLETE", quote.at_ms,
                       {"plan_id": pos["plan_id"], "family": pos["family"], "cash_net": pos["cash_paid"],
                        "net_r": pos["cash_paid"] / pos["initial_cash_risk"],
                        "duration_ms": quote.at_ms - pos["fill_ms"],
                        "observed_quote_mae": -pos["excursion_min"], "observed_quote_mfe": pos["excursion_max"]})
        state["active"] = None


def paper_step(journal, request_id, quote, *, now_ms, implementation_hash, profile_hash,
               evaluate, recheck, entry_kind="MARKET_NEXT_QUOTE", input_hash=None, session_open=True,
               clock=None):
    """Evaluate an internal analysis callback and commit quote + fills atomically.

    Callback inputs are an owned snapshot of committed state and the current
    quote. The caller never supplies a serialized risk approval.
    """
    if type(session_open) is not bool:
        raise ValueError("Unknown paper session state")
    with journal.transaction():
        if clock is not None:
            now_ms = clock()
        state = journal.state()
        if state is None:
            raise ValueError("Register a paper protocol first")
        protocol = state["protocol"]
        if (implementation_hash, profile_hash) != (protocol["implementation_hash"], protocol["profile_hash"]):
            raise ValueError("Paper source or profile drift")
        wire = {"quote": asdict(quote), "input_hash": input_hash, "session_open": session_open}
        if not journal.accept(request_id, wire, now_ms):
            return {"status": "DUPLICATE", "broker_orders": False}
        _quote_ok(quote, protocol, now_ms)
        active = state["active"]
        allowed = not active or active["state"] != "PENDING" or recheck(active.copy(), quote, state.copy(), now_ms) is True
        _advance(journal, state, quote, entry_allowed=allowed and session_open, session_open=session_open)
        decision, costs = evaluate(state.copy(), quote, now_ms)
        close_ms = decision["bar_close_ms"]
        if (decision["symbol"] != protocol["symbol"] or decision["timeframe_ms"] != protocol["timeframe_ms"]
                or decision["as_of_ms"] != now_ms or (close_ms is not None and close_ms > quote.at_ms)
                or (close_ms is None and decision["state"] != "DATA_UNAVAILABLE")
                or decision["specialists"]["audit"]["facts"].get("source") != protocol["source"]):
            raise ValueError("Decision differs from frozen point-in-time protocol")
        journal.append("PAPER_DECISION", now_ms, decision)
        record_notification(journal, state, decision, costs.tick_size)
        _submit(journal, state, decision, costs=costs, now_ms=now_ms, entry_kind=entry_kind)
        journal.set_state(state, now_ms)
        return {"status": "RECORDED", "trace_id": decision["trace_id"], "decision": decision["state"],
                "broker_orders": False}


def _report(journal, at_ms):
    events = journal.events()
    state = journal.state()
    if not state:
        return {"status": "NO_PAPER_PROTOCOL", "broker_orders": False}
    p = state["protocol"]
    fills = [e for e in events if e["kind"] == "PAPER_COMPLETE"]
    values = [e["data"]["cash_net"] for e in fills]
    returns = [e["data"]["net_r"] for e in fills]
    equity, peak, peak_time = p["initial_cash"], p["initial_cash"], state["registered_ms"]
    maximum_dd, max_duration = 0.0, 0
    groups = {"days": {}, "weeks": {}}
    for event in events:
        value = event["data"].get("cash_delta")
        if value is None:
            continue
        previously_underwater = equity < peak
        equity += value
        at = datetime.fromtimestamp(event["at_ms"] / 1000, timezone.utc)
        year, week, _ = at.isocalendar()
        for group, key in (("days", at.date().isoformat()), ("weeks", f"{year}-W{week:02d}")):
            groups[group][key] = groups[group].get(key, 0.0) + value
        if equity >= peak:
            if previously_underwater:
                max_duration = max(max_duration, event["at_ms"] - peak_time)
            peak, peak_time = equity, event["at_ms"]
        else:
            maximum_dd = max(maximum_dd, peak - equity)
            max_duration = max(max_duration, event["at_ms"] - peak_time)
    metrics = cash_metrics(values)
    winners, losers = [v for v in values if v > 0], [v for v in values if v < 0]
    metrics.update(average_winner=mean(winners) if winners else None,
                   average_loser=mean(losers) if losers else None)
    confidence = block_bootstrap(returns)
    pf = metrics["profit_factor"]
    if equity < peak and state["last_quote_ms"] is not None:
        max_duration = max(max_duration, state["last_quote_ms"] - peak_time)
    failure = (maximum_dd > p["max_drawdown_fraction"] * p["initial_cash"]
               or (len(values) >= p["min_campaigns"] and (mean(returns) < p["min_net_r"]
                                                        or pf is None or pf < p["min_profit_factor"])))
    verdict = "REJECT" if failure else "INSUFFICIENT_EVIDENCE"
    paused = any(e["kind"] == "PAPER_OPERATOR_PAUSE" and e["at_ms"] < p["end_ms"] for e in events)
    if (not failure and not paused and len(values) >= p["min_campaigns"]
            and confidence["ci95"][0] is not None and confidence["ci95"][0] > 0):
        verdict = "CONTINUE_PAPER_TEST"
    outstanding = None if state["active"] is None else state["active"]["state"]
    ended = at_ms is not None and at_ms >= p["end_ms"]
    if ended and outstanding is not None:
        outstanding = "OPEN_RIGHT_CENSORED" if outstanding == "OPEN" else "UNFILLED_AT_WINDOW_END"
    return {"status": verdict, "protocol_id": p["protocol_id"], "metrics": metrics,
            "win_rate_ci95": wilson(sum(v > 0 for v in values), len(values)),
            "net_r": confidence, "paid_cash_equity": equity, "paid_cash_drawdown": maximum_dd,
            "observed_drawdown_duration_ms": max_duration, "calendar": groups,
            "target_parts": {f"TP{i}": sum(e["kind"] == "PAPER_PARTIAL" and e["data"]["reason"] == f"TP{i}"
                                          for e in events) for i in (1, 2, 3)},
            "missed": sum(e["kind"] == "PAPER_MISSED" for e in events),
            "outstanding": outstanding, "window_ended": ended,
            "operator_intervention": paused, "evidence_class": p["evidence_class"],
            "automatic_promotion": False, "broker_orders": False}


def report(journal, *, at_ms=None):
    if journal.db.in_transaction:
        return _report(journal, at_ms)
    journal.db.execute("BEGIN")
    try:
        return _report(journal, at_ms)
    finally:
        journal.db.execute("ROLLBACK")


def set_manual_pause(journal, paused, *, at_ms):
    if type(paused) is not bool:
        raise ValueError("Invalid pause command")
    with journal.transaction():
        state = journal.state()
        if state is None:
            raise ValueError("Register a paper protocol first")
        state["operator_paused"] = paused
        journal.append("PAPER_OPERATOR_PAUSE", at_ms, {"paused": paused})
        journal.set_state(state, at_ms)
