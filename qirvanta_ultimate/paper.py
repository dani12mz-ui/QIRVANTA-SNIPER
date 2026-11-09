"""Quote-driven paper execution and prospective protocol enforcement."""
from dataclasses import asdict, dataclass
from collections import Counter
from datetime import datetime, timezone
from math import isclose
from statistics import mean

from research.strategies.library import FAMILIES
from research.validation.statistics import block_bootstrap, cash_metrics, wilson

from .common import digest, finite, identifier, milliseconds, sha256
from .notifications import record_notification, record_paper_fill
from .risk import ManagementPolicy, TradePlan, manage_quote


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
    strategy_id: str | None = None
    max_gap_ms: int | None = None
    profit_lock_r: float = 0.
    atr_trail_multiplier: float | None = None

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
                or any(family not in FAMILIES[:9] + ("CHAMPION",) for family in self.families)
                or ("CHAMPION" in self.families and self.strategy_id != "CHAMPION")):
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
        if self.evidence_class not in ("PROSPECTIVE_DECLARED_SOURCE", "SYNTHETIC_TEST",
                                       "PROSPECTIVE_PAPER", "HISTORICAL_REPLAY"):
            raise ValueError("Unknown evidence class")
        if self.strategy_id is not None:
            identifier(self.strategy_id)
        if self.max_gap_ms is not None and (type(self.max_gap_ms) is not int or self.max_gap_ms < 1000):
            raise ValueError("Invalid feed-gap threshold")
        if not 0 <= finite(self.profit_lock_r) < 1:
            raise ValueError("Invalid post-TP1 profit lock")
        if self.atr_trail_multiplier is not None and finite(self.atr_trail_multiplier, positive=True) < 1:
            raise ValueError("ATR trail must be at least one ATR")


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
    pos = state["active"]
    regime = decision["specialists"].get("regime", {}).get("facts", {})
    pos.update(campaign_id=digest([p["protocol_id"], mgmt["plan_id"]]),
               strategy_id=p.get("strategy_id") or mgmt["family"],
               symbol=p["symbol"], regime=regime.get("regime"), regime_direction=regime.get("direction"),
               initial_stop=mgmt["stop"], fees=0.0, path_uncertain=False)
    journal.append("PAPER_PENDING", now_ms, {**_identity(pos), "trace_id": decision["trace_id"],
                                            "entry_kind": entry_kind, "lots": lots,
                                            "reference": pos["entry"], "initial_stop": pos["stop"],
                                            "targets": pos["targets"], "expires_ms": pos["expires_ms"],
                                            "issued_ms": pos["issued_ms"], "remaining": pos["remaining"],
                                            **decision.get("portfolio", {})})


def _identity(position):
    keys = ("plan_id", "campaign_id", "strategy_id", "signal_id", "source_plan_id", "side", "family",
            "symbol", "regime", "regime_direction")
    return {key: position[key] for key in keys if key in position}


def _miss(journal, state, pos, at_ms, reason):
    journal.append("PAPER_MISSED", at_ms, {**_identity(pos), "reason": reason})
    state["active"] = None


def _part(journal, state, position, i, price, reason, quote_ms, *, adverse_slip=0.):
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
    position["fees"] = position.get("fees", 0.) + fee
    state["cash"] += cash
    journal.append("PAPER_PARTIAL", quote_ms, {**_identity(position), "component": i + 1,
                                              "quantity": quantity, "price": price, "fee": fee,
                                              "cash_delta": cash, "reason": reason,
                                              "adverse_slippage_price": adverse_slip,
                                              "remaining": position["remaining"]})
    if reason.startswith("TP"):
        record_paper_fill(journal, position, quote_ms, reason, price, quantity)


def _advance(journal, state, quote, *, entry_allowed, session_open, discontinuity=False, atr=None):
    last = state["last_quote_ms"]
    if last is not None and quote.at_ms <= last:
        raise ValueError("Paper quotes must advance strictly")
    state["last_quote_ms"] = quote.at_ms
    pos = state["active"]
    max_gap = state["protocol"].get("max_gap_ms")
    if discontinuity or (last is not None and max_gap is not None and quote.at_ms - last > max_gap):
        state["gap_quote_ms"] = quote.at_ms
        journal.append("PAPER_FEED_GAP", quote.at_ms, {"previous_quote_ms": last,
                       "current_quote_ms": quote.at_ms, "position_state": pos["state"] if pos else None})
        if pos and pos["state"] == "PENDING":
            _miss(journal, state, pos, quote.at_ms, "OBSERVATION_GAP")
            return
        if pos:
            pos["path_uncertain"] = True
    if not pos:
        return
    if pos["state"] == "OPEN" and not session_open:
        if state["protocol"].get("strategy_id") is not None:
            pos["path_uncertain"] = True
        return
    side = pos["side"]
    closing = quote.bid if side == 1 else quote.ask
    cost = pos["costs"]
    slip = cost["tick_size"] * cost["slippage_ticks"] * side
    if pos["state"] == "PENDING":
        if quote.at_ms <= pos["submitted_ms"]:
            return
        if quote.at_ms >= pos["expires_ms"] or not entry_allowed:
            reason = ("UNFILLED_LIMIT" if quote.at_ms >= pos["expires_ms"] and pos["entry_kind"] == "LIMIT"
                      else "ENTRY_EXPIRED" if quote.at_ms >= pos["expires_ms"] else "FILL_RISK_RECHECK_BLOCKED")
            _miss(journal, state, pos, quote.at_ms, reason)
            return
        executable = quote.ask if side == 1 else quote.bid
        if pos["entry_kind"] == "LIMIT" and (executable - pos["entry"]) * side > 0:
            return
        fill = executable + slip if pos["entry_kind"] == "MARKET_NEXT_QUOTE" else pos["entry"]
        if (closing - pos["stop"]) * side <= 0 or (pos["targets"][0] - fill) * side <= 0:
            _miss(journal, state, pos, quote.at_ms, "GAP_INVALIDATION")
            return
        fee = pos["lots"] * cost["fee_per_lot_per_fill"]
        risk_per_lot = (fill - pos["stop"] + slip) * side / cost["tick_size"] * cost["loss_per_tick_lot"]
        risk_cash = pos["lots"] * risk_per_lot + 2 * fee
        if risk_cash > pos["estimated_loss_cash"] + 1e-9:
            _miss(journal, state, pos, quote.at_ms, "GAP_RISK_CAP")
            state["cooldown_until_ms"] = quote.at_ms + state["protocol"]["timeframe_ms"]
            return
        pos.update(state="OPEN", fill_price=fill, fill_ms=quote.at_ms, cash_paid=-fee,
                   initial_cash_risk=risk_cash, excursion_min=0.0, excursion_max=0.0,
                   fees=fee, tp_hits=[], entry_slippage=abs(slip) if pos["entry_kind"] == "MARKET_NEXT_QUOTE" else 0.)
        state["cash"] -= fee
        journal.append("PAPER_FILL", quote.at_ms, {**_identity(pos), "price": fill,
                                                  "quantity": pos["lots"], "fee": fee, "cash_delta": -fee,
                                                  "bid": quote.bid, "ask": quote.ask,
                                                  "entry_slippage": pos["entry_slippage"],
                                                  "initial_cash_risk": risk_cash, "costs": cost})
        record_paper_fill(journal, pos, quote.at_ms, "ENTRY", fill, pos["lots"])
        return
    movement = (closing - pos["fill_price"]) * side
    pos["excursion_min"] = min(pos["excursion_min"], movement)
    pos["excursion_max"] = max(pos["excursion_max"], movement)
    stop = (closing - pos["stop"]) * side <= 0
    time_stop = quote.at_ms - pos["fill_ms"] >= state["protocol"]["max_hold_ms"]
    p = state["protocol"]
    preview = None
    if p.get("strategy_id") is not None and not stop and not time_stop:
        plan = TradePlan(pos["plan_id"], quote.symbol, side, pos["entry"], pos["initial_stop"],
                         pos["targets"], pos["issued_ms"], pos["expires_ms"])
        management = ManagementPolicy(p["max_hold_ms"], profit_lock_r=p.get("profit_lock_r", 0.),
                                      atr_trail_multiplier=p.get("atr_trail_multiplier"))
        preview = manage_quote(plan, quote, fill_price=pos["fill_price"], fill_at_ms=pos["fill_ms"],
                               current_stop=pos["stop"], next_target=next(i for i, n in enumerate(pos["remaining"]) if n > 0),
                               previous_quote_at_ms=last, tick_size=cost["tick_size"], policy=management,
                               now_ms=quote.at_ms, max_quote_age_ms=p["max_quote_age_ms"],
                               expected_server=quote.server, atr=atr)
    if stop or time_stop:
        reason = "STOP" if stop else "TIME_STOP"
        if stop:
            state["cooldown_until_ms"] = quote.at_ms + state["protocol"]["timeframe_ms"]
        quantity = sum(pos["remaining"])
        for i in range(3):
            _part(journal, state, pos, i, closing - slip, reason, quote.at_ms, adverse_slip=abs(slip))
        record_paper_fill(journal, pos, quote.at_ms, reason, closing - slip, quantity)
    else:
        for i, target in enumerate(pos["targets"]):
            if (closing - target) * side >= 0 and pos["remaining"][i] > 0:
                _part(journal, state, pos, i, target, f"TP{i + 1}", quote.at_ms)
                pos.setdefault("tp_hits", []).append(i + 1)
                if i == 0 and preview is None:
                    old = pos["stop"]
                    pos["stop"] = max(old, pos["fill_price"]) if side == 1 else min(old, pos["fill_price"])
                    journal.append("PAPER_STOP_MOVED", quote.at_ms, {**_identity(pos), "previous_stop": old,
                                   "next_stop": pos["stop"], "reason": "TP1_PRICE_BREAKEVEN"})
        if preview is not None and preview.next_stop is not None:
            old = pos["stop"]
            pos["stop"] = preview.next_stop
            journal.append("PAPER_STOP_MOVED", quote.at_ms, {**_identity(pos), "previous_stop": old,
                           "next_stop": pos["stop"], "reason": "FROZEN_PROFIT_LOCK_OR_ATR_TRAIL"})
        reason = "TARGETS_COMPLETE"
    if not any(pos["remaining"]):
        journal.append("PAPER_COMPLETE", quote.at_ms,
                       {**_identity(pos), "cash_net": pos["cash_paid"],
                        "net_r": pos["cash_paid"] / pos["initial_cash_risk"],
                        "initial_cash_risk": pos["initial_cash_risk"], "fees": pos.get("fees"),
                        "path_uncertain": pos.get("path_uncertain", False), "reason": reason,
                        "tp_hits": pos.get("tp_hits", []), "fill_ms": pos["fill_ms"],
                        "duration_ms": quote.at_ms - pos["fill_ms"],
                        "observed_quote_mae": -pos["excursion_min"], "observed_quote_mfe": pos["excursion_max"]})
        state["active"] = None


def _mark_equity(state, quote, session_open):
    if not session_open:
        return
    pos = state["active"]
    equity = state["cash"]
    if pos and pos["state"] == "OPEN":
        cost = pos["costs"]
        closing = quote.bid if pos["side"] == 1 else quote.ask
        delta = (closing - pos["fill_price"]) * pos["side"] - cost["tick_size"] * cost["slippage_ticks"]
        rate = cost["profit_per_tick_lot"] if delta > 0 else cost["loss_per_tick_lot"]
        equity += sum(pos["remaining"]) * (delta / cost["tick_size"] * rate - cost["fee_per_lot_per_fill"])
    mark = state.setdefault("observed_equity", {"peak": state["protocol"]["initial_cash"],
                            "peak_ms": state["registered_ms"], "max_drawdown": 0., "max_drawdown_ms": 0})
    mark["cash"] = equity
    mark["unrealized_after_exit_costs"] = equity - state["cash"]
    mark["at_ms"] = quote.at_ms
    if equity >= mark["peak"]:
        mark.update(peak=equity, peak_ms=quote.at_ms)
    else:
        mark["max_drawdown"] = max(mark["max_drawdown"], mark["peak"] - equity)
        mark["max_drawdown_ms"] = max(mark["max_drawdown_ms"], quote.at_ms - mark["peak_ms"])


def paper_step(journal, request_id, quote, *, now_ms, implementation_hash, profile_hash,
               evaluate, recheck, entry_kind="MARKET_NEXT_QUOTE", input_hash=None, session_open=True,
               clock=None, discontinuity=False, atr=None):
    """Evaluate an internal analysis callback and commit quote + fills atomically.

    Callback inputs are an owned snapshot of committed state and the current
    quote. The caller never supplies a serialized risk approval.
    """
    if type(session_open) is not bool or type(discontinuity) is not bool:
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
        if protocol.get("strategy_id") is not None:
            wire["discontinuity"] = discontinuity
        if not journal.accept(request_id, wire, now_ms):
            return {"status": "DUPLICATE", "broker_orders": False}
        _quote_ok(quote, protocol, now_ms)
        active = state["active"]
        allowed = not active or active["state"] != "PENDING" or recheck(active.copy(), quote, state.copy(), now_ms) is True
        _advance(journal, state, quote, entry_allowed=allowed and session_open, session_open=session_open,
                 discontinuity=discontinuity, atr=atr)
        decision, costs = evaluate(state.copy(), quote, now_ms)
        close_ms = decision["bar_close_ms"]
        if (decision["symbol"] != protocol["symbol"] or decision["timeframe_ms"] != protocol["timeframe_ms"]
                or decision["as_of_ms"] != now_ms or (close_ms is not None and close_ms > quote.at_ms)
                or (close_ms is None and decision["state"] != "DATA_UNAVAILABLE")
                or decision["specialists"]["audit"]["facts"].get("source") != protocol["source"]):
            raise ValueError("Decision differs from frozen point-in-time protocol")
        role_statuses = [[name, detail["status"], detail["facts"].get("reason")]
                         for name, detail in decision["specialists"].items()]
        plan_id = decision["specialists"].get("management", {}).get("facts", {}).get("plan_id")
        shape = [close_ms, decision["state"], decision["reason"], plan_id, role_statuses]
        old_shape = state.get("decision_shape")
        if protocol.get("strategy_id") is None or old_shape != shape:
            journal.append("PAPER_DECISION", now_ms, decision)
            state["decision_shape"] = shape
        else:
            journal.append("PAPER_QUOTE_DECISION", now_ms, {
                "trace_id": decision["trace_id"], "quote_at_ms": quote.at_ms,
                "bar_close_ms": close_ms, "state": decision["state"], "reason": decision["reason"],
            })
        if protocol.get("strategy_id") is not None:
            _mark_equity(state, quote, session_open)
            state["last_decision"] = {"state": decision["state"], "reason": decision["reason"],
                                      "trace_id": decision["trace_id"], "at_ms": now_ms,
                                      "bar_close_ms": decision["bar_close_ms"]}
            health = state.setdefault("specialist_health", {})
            for name, detail in decision["specialists"].items():
                success = health.get(name, {}).get("last_success_ms")
                health[name] = {"status": detail["status"], "trace_id": decision["trace_id"],
                                "facts": detail["facts"], "at_ms": now_ms,
                                "reason": detail["facts"].get("reason"),
                                "latency_ms": decision.get("latency_ms", {}).get(name),
                                "last_success_ms": now_ms if detail["status"] == "OK" else success}
            counts = state.setdefault("decision_counts", {})
            category = decision["state"]
            counts[category] = counts.get(category, 0) + 1
            bar = decision["bar_close_ms"]
            if bar is not None and bar != state.get("last_assessed_bar_ms"):
                state["last_assessed_bar_ms"] = bar
                state["closed_bar_observations"] = state.get("closed_bar_observations", 0) + 1
        record_notification(journal, state, decision, costs.tick_size)
        _submit(journal, state, decision, costs=costs, now_ms=now_ms, entry_kind=entry_kind)
        journal.set_state(state, now_ms)
        return {"status": "RECORDED", "trace_id": decision["trace_id"], "decision": decision["state"],
                "broker_orders": False}


def _report(journal, at_ms):
    state = journal.state()
    if not state:
        return {"status": "NO_PAPER_PROTOCOL", "broker_orders": False}
    p = state["protocol"]
    events = journal.events(kinds=("PAPER_FILL", "PAPER_PARTIAL", "PAPER_COMPLETE", "PAPER_OPERATOR_PAUSE",
                                   "PAPER_MISSED", "PAPER_PENDING", "PAPER_FEED_GAP", "PAPER_SKIPPED"))
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
    result = {"status": verdict, "protocol_id": p["protocol_id"], "metrics": metrics,
            "win_rate_ci95": wilson(sum(v > 0 for v in values), len(values)),
            "net_r": confidence, "paid_cash_equity": equity, "paid_cash_drawdown": maximum_dd,
            "observed_drawdown_duration_ms": max_duration, "calendar": groups,
            "target_parts": {f"TP{i}": sum(e["kind"] == "PAPER_PARTIAL" and e["data"]["reason"] == f"TP{i}"
                                          for e in events) for i in (1, 2, 3)},
            "missed": sum(e["kind"] == "PAPER_MISSED" for e in events),
            "outstanding": outstanding, "window_ended": ended,
            "operator_intervention": paused, "evidence_class": p["evidence_class"],
            "automatic_promotion": False, "broker_orders": False}
    if p.get("strategy_id") is None:
        return result
    hit_rates = {f"TP{i}": (sum(i in e["data"].get("tp_hits", ()) for e in fills) / len(fills) if fills else None)
                 for i in (1, 2, 3)}
    gap_count = sum(e["kind"] == "PAPER_FEED_GAP" for e in events)
    uncertain = sum(e["data"].get("path_uncertain") is True for e in fills)
    mark = state.get("observed_equity", {})
    position = state["active"]
    active = None
    if position:
        active = {**_identity(position),
                  **{key: position[key] for key in ("state", "entry", "initial_stop", "stop", "targets",
                                                     "lots", "remaining", "submitted_ms", "issued_ms",
                                                     "expires_ms", "cash_paid", "path_uncertain")},
                  "fill_price": position.get("fill_price"), "fill_ms": position.get("fill_ms"),
                  "open_quantity": sum(position["remaining"]) if position["state"] == "OPEN" else 0.,
                  "initial_cash_risk": position.get("initial_cash_risk"),
                  "reserved_loss_cash": position["estimated_loss_cash"] if position["state"] == "PENDING" else None,
                  "unrealized_after_exit_costs": mark.get("unrealized_after_exit_costs") if position["state"] == "OPEN" else None,
                  "mark_at_ms": mark.get("at_ms") if position["state"] == "OPEN" else None}
    dd = max(maximum_dd, mark.get("max_drawdown", 0.))
    evidence_failures = []
    if p["evidence_class"] != "PROSPECTIVE_PAPER":
        evidence_failures.append("NON_PROSPECTIVE_INPUT")
    if not ended:
        evidence_failures.append("WINDOW_OPEN")
    if outstanding:
        evidence_failures.append("RIGHT_CENSORED_POSITION")
    if uncertain:
        evidence_failures.append("CAMPAIGN_PATH_UNCERTAIN")
    if paused:
        evidence_failures.append("OPERATOR_INTERVENTION")
    if len(fills) < p["min_campaigns"]:
        evidence_failures.append("TOO_FEW_COMPLETE_CAMPAIGNS")
    if confidence["ci95"][0] is None or confidence["ci95"][0] <= 0:
        evidence_failures.append("EXPECTANCY_INTERVAL_NOT_POSITIVE")
    result["status"] = (
        "REJECT" if failure or dd > p["max_drawdown_fraction"] * p["initial_cash"]
        else "INSUFFICIENT_FORWARD_EVIDENCE" if evidence_failures else "CONTINUE_PAPER_TEST")
    result.update(strategy_id=p["strategy_id"], complete_campaigns=len(fills),
                  entry_fills=sum(e["kind"] == "PAPER_FILL" for e in events),
                  pending_submissions=sum(e["kind"] == "PAPER_PENDING" for e in events),
                  paid_fill_fees=sum(e["data"]["fee"] for e in events if e["kind"] in ("PAPER_FILL", "PAPER_PARTIAL")),
                  observed_equity=mark, observed_equity_drawdown_fraction=dd / p["initial_cash"],
                  observed_equity_mark_age_ms=max(0, at_ms - mark["at_ms"]) if at_ms is not None and mark else None,
                  active_campaign=active,
                  feed_gap_observations=gap_count, completed_with_uncertain_path=uncertain,
                  campaign_tp_hit_rates=hit_rates,
                  stops_before_tp1=sum(e["data"].get("reason") == "STOP" and not e["data"].get("tp_hits") for e in fills),
                  average_hold_ms=mean(e["data"]["duration_ms"] for e in fills) if fills else None,
                  exit_reasons=dict(Counter(e["data"].get("reason", "UNSPECIFIED") for e in fills)),
                  missed_reasons=dict(Counter(e["data"]["reason"] for e in events if e["kind"] == "PAPER_MISSED")),
                  skipped_reasons=dict(Counter(e["data"]["reason"] for e in events if e["kind"] == "PAPER_SKIPPED")),
                  buy_metrics=cash_metrics([e["data"]["cash_net"] for e in fills if e["data"].get("side") == 1]),
                  sell_metrics=cash_metrics([e["data"]["cash_net"] for e in fills if e["data"].get("side") == -1]),
                  regime_metrics={key: cash_metrics([e["data"]["cash_net"] for e in fills
                                                    if e["data"].get("regime") == key])
                                  for key in sorted({e["data"]["regime"] for e in fills if e["data"].get("regime")})},
                  decision_counts=state.get("decision_counts", {}),
                  closed_bar_observations=state.get("closed_bar_observations", 0),
                  specialist_health=state.get("specialist_health", {}),
                  latest_decision=state.get("last_decision"), forward_evidence_failures=evidence_failures,
                  start_ms=p["start_ms"], end_ms=p["end_ms"], min_campaigns=p["min_campaigns"])
    return result


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
