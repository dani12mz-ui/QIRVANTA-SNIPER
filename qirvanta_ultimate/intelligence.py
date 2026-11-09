"""Causal market observation and twelve deterministic specialist roles."""
from dataclasses import asdict, dataclass
from queue import Empty, Queue
from threading import BoundedSemaphore, Thread
from time import perf_counter_ns

from research.data.loader import validate_bars
from research.features.causal import build_features
from research.strategies.library import CandidateLibrary, FAMILIES, validated_selection

from .calendar import assess_news
from .common import digest, finite, identifier, milliseconds, sha256


@dataclass(frozen=True)
class MarketObservation:
    symbol: str
    source: str
    source_hash: str
    timeframe_ms: int
    as_of_ms: int
    bar: dict
    previous_bar: dict | None
    features: dict
    previous_features: dict | None
    candidates: tuple[dict, ...]
    eligible_families: tuple[str, ...] = ()


def observe(bars, *, symbol, source, source_hash, as_of_ms, families=(),
            mintick=.001, validation_manifest=None):
    identifier(symbol)
    identifier(source)
    sha256(source_hash)
    milliseconds(as_of_ms)
    finite(mintick, positive=True)
    if len(set(families)) != len(families) or any(f not in FAMILIES[:9] for f in families):
        raise ValueError("Unknown or duplicated direct candidate family")
    closed = [bar for bar in bars if bar["time_close"] <= as_of_ms]
    timeframe = validate_bars(closed)
    if timeframe is None:
        raise ValueError("No closed bars available at cutoff")
    features = build_features(closed)
    library = CandidateLibrary(mintick=mintick)
    candidates = ()
    for i in range(len(closed)):
        candidates = tuple(library.evaluate(family, side, i, closed, features)
                           for family in families for side in (1, -1))
    routing, _ = validated_selection(validation_manifest, closed[0]["time"])
    eligible = tuple(routing.get(features[-1]["regime"], ()))
    return MarketObservation(symbol, source, source_hash, timeframe, as_of_ms, closed[-1],
                             closed[-2] if len(closed) > 1 else None, features[-1],
                             features[-2] if len(features) > 1 else None, candidates, eligible)


@dataclass(frozen=True)
class SpecialistResult:
    status: str
    facts: dict


@dataclass(frozen=True)
class AnalysisPolicy:
    max_bar_age_ms: int = 1800000
    max_quote_age_ms: int = 5000
    max_deviation_atr: float = .2
    timeout_ms: int = 250
    research_candidates: bool = False

    def __post_init__(self):
        for v in (self.max_bar_age_ms, self.max_quote_age_ms, self.timeout_ms):
            if type(v) is not int or v <= 0:
                raise ValueError("Invalid observation latency policy")
        finite(self.max_deviation_atr, positive=True)
        if type(self.research_candidates) is not bool:
            raise ValueError("Invalid experimental candidate policy")


@dataclass(frozen=True)
class AnalysisContext:
    market: MarketObservation
    policy: AnalysisPolicy
    quote: object = None
    news_provider: object = None
    manual_pause: bool = False
    guardian: object = None
    model_service: object = None


def _ready(status, **facts):
    return SpecialistResult(status, facts)


def market_regime(ctx, _):
    f = ctx.market.features
    return _ready("OK" if f["ready"] else "UNAVAILABLE",
                  regime=f["regime"], direction=f["direction"], efficiency=f["efficiency"])


def market_structure(ctx, _):
    prior = ctx.market.previous_features
    prev = ctx.market.previous_bar
    if not prior or not prev or prior["pivot_high"] is None or prior["pivot_low"] is None:
        return _ready("UNAVAILABLE", reason="CONFIRMED_SWINGS_MISSING")
    close = ctx.market.bar["close"]
    hi, lo = prior["pivot_high"], prior["pivot_low"]
    side = 1 if prev["close"] <= hi < close else -1 if prev["close"] >= lo > close else 0
    label = "NONE" if not side else "CHOCH_PROXY" if prior["direction"] == -side else "BOS_PROXY"
    return _ready("OK", event=label, side=side, upper=hi, lower=lo)


def liquidity(ctx, _):
    f, b = ctx.market.previous_features, ctx.market.bar
    if not f or f["range20_high"] is None or f["range20_low"] is None:
        return _ready("UNAVAILABLE", reason="PRIOR_RANGE_MISSING")
    return _ready("OK", lower_sweep=b["low"] < f["range20_low"] < b["close"],
                  upper_sweep=b["high"] > f["range20_high"] > b["close"],
                  evidence="OHLC_PROXY")


def momentum(ctx, _):
    f, prev = ctx.market.features, ctx.market.previous_features
    if not prev or f["rsi"] is None or prev["rsi"] is None:
        return _ready("UNAVAILABLE", reason="RSI_WARMUP")
    return _ready("OK", rsi=f["rsi"], rsi_delta=f["rsi"] - prev["rsi"],
                  signed_body=ctx.market.bar["close"] - ctx.market.bar["open"])


def volatility(ctx, _):
    f = ctx.market.features
    if not f["atr"] or f["atr_ratio"] is None:
        return _ready("UNAVAILABLE", reason="ATR_WARMUP")
    shock = f["range"] > 3 * f["atr"]
    return _ready("SHOCK" if shock else "OK", atr=f["atr"], atr_ratio=f["atr_ratio"], shock=shock)


def multi_timeframe(ctx, _):
    f, at = ctx.market.features, ctx.market.bar["time_close"]
    h1, h4 = f["h1"], f["h4"]
    for row, interval in ((h1, 3600000), (h4, 14400000)):
        stamp = row["available_at"]
        if (row["bias"] not in (-1, 0, 1) or stamp is None or stamp % interval != 0
                or not 0 <= at - stamp < interval):
            return _ready("UNAVAILABLE", reason="COMPLETED_HTF_MISSING_OR_STALE")
    return _ready("DISAGREEMENT" if h1["bias"] * h4["bias"] < 0 else "OK",
                  h1=h1["bias"], h4=h4["bias"], h1_available_ms=h1["available_at"],
                  h4_available_ms=h4["available_at"])


def macro_news(ctx, _):
    return SpecialistResult("OK", asdict(assess_news(ctx.news_provider, as_of_ms=ctx.market.as_of_ms,
                                                    manual_pause=ctx.manual_pause)))


def strategy(ctx, _):
    candidates = ctx.market.candidates
    confirmed = [d for d in candidates if d["status"] == "CONFIRMED"]
    if len({d["side"] for d in confirmed}) > 1:
        return _ready("DISAGREEMENT", conflicting_ids=[d["signal_id"] for d in confirmed])
    eligible = [d for d in confirmed if d["family"] in ctx.market.eligible_families]
    pool = eligible or (confirmed if ctx.policy.research_candidates else [])
    if len(pool) > 1:
        return _ready("DISAGREEMENT", competing_ids=[d["signal_id"] for d in pool])
    if not pool:
        if confirmed:
            return _ready("UNVALIDATED", experimental_ids=[d["signal_id"] for d in confirmed])
        if any("SETUP_EXPIRED" in d["reasons"] for d in candidates):
            return _ready("EXPIRED", reason="SETUP_EXPIRED")
        if any("STRUCTURAL_INVALIDATION" in d["reasons"] for d in candidates):
            return _ready("INVALIDATED", candidate_count=len(candidates))
        return _ready("WATCH" if any(d["status"] == "WATCH" for d in candidates) else "NO_SETUP",
                      candidate_count=len(candidates))
    d = pool[0]
    if d["family"] == "CHAMPION":
        return _ready("OK", signal_id=d["signal_id"], side=d["side"], family=d["family"],
                      status_basis="AUTHENTICATED_TRADINGVIEW_NATIVE_SUBMISSION",
                      entry=d["reference"], stop=d["initial_stop"], issued_ms=d["confirmation_time"],
                      targets=d["targets"], expires_ms=d["expires_ms"], source_plan_id=d["source_plan_id"],
                      atr_at_signal=d["atr_at_signal"])
    return _ready("OK", signal_id=d["signal_id"], side=d["side"], family=d["family"],
                  status_basis="REGISTERED_EXPERIMENT" if not eligible else "MANIFEST_EVIDENCE",
                  entry=d["reference"], stop=d["initial_stop"], issued_ms=d["confirmation_time"])


def entry_execution(ctx, done):
    candidate = done["strategy"]
    if candidate.status != "OK":
        return _ready("SKIPPED", reason="NO_UNAMBIGUOUS_CANDIDATE")
    q, market = ctx.quote, ctx.market
    if q is None:
        return _ready("UNAVAILABLE", reason="BID_ASK_UNAVAILABLE")
    if q.symbol != market.symbol:
        return _ready("UNAVAILABLE", reason="BID_ASK_SYMBOL_MISMATCH")
    finite(q.bid, positive=True)
    finite(q.ask, positive=True)
    milliseconds(q.at_ms)
    issued = candidate.facts["issued_ms"]
    if q.bid > q.ask or q.at_ms < issued or not 0 <= market.as_of_ms - q.at_ms <= ctx.policy.max_quote_age_ms:
        return _ready("UNAVAILABLE", reason="STALE_FUTURE_OR_CROSSED_QUOTE")
    side = candidate.facts["side"]
    executable = q.ask if side == 1 else q.bid
    atr = candidate.facts.get("atr_at_signal", market.features["atr"])
    if not atr:
        return _ready("UNAVAILABLE", reason="ATR_UNAVAILABLE")
    deviation = abs(executable - candidate.facts["entry"]) / atr
    return _ready("INVALIDATED" if deviation > ctx.policy.max_deviation_atr else "OK",
                  deviation_atr=deviation, spread=q.ask - q.bid)


def trade_management(ctx, done):
    candidate = done["strategy"]
    if candidate.status != "OK":
        return _ready("SKIPPED", reason="NO_UNAMBIGUOUS_CANDIDATE")
    d = candidate.facts
    risk = (d["entry"] - d["stop"]) * d["side"]
    if risk <= 0:
        return _ready("INVALIDATED", reason="INVALID_STOP_GEOMETRY")
    targets = d.get("targets", tuple(d["entry"] + risk * d["side"] * r for r in (1, 2, 3)))
    if min(targets) <= 0:
        return _ready("INVALIDATED", reason="INVALID_TARGET_GEOMETRY")
    identity = [ctx.market.source, ctx.market.symbol, ctx.market.timeframe_ms, d["signal_id"]]
    facts = dict(plan_id=digest(identity), signal_id=d["signal_id"], side=d["side"], family=d["family"],
                 entry=d["entry"], stop=d["stop"], targets=targets, initial_risk=risk,
                 issued_ms=d["issued_ms"], expires_ms=d.get("expires_ms", d["issued_ms"] + ctx.market.timeframe_ms),
                 policy="ONE_TWO_THREE_R_TP1_BREAKEVEN_NEXT_OBSERVED_QUOTE")
    if d["family"] == "CHAMPION":
        facts.update(source_plan_id=d["source_plan_id"],
                     policy="PINE_LEVELS_LOCAL_PAPER_MANAGEMENT")
    return _ready("OK", **facts)


def risk_guardian(ctx, done):
    if done["entry"].status != "OK" or done["management"].status != "OK":
        return _ready("SKIPPED", reason="NO_EXECUTABLE_PLAN")
    news = done["macro"]
    if news.status != "OK" or news.facts["blocked"]:
        return _ready("BLOCKED", reason=news.facts.get("status", "NEWS_DATA_UNAVAILABLE"))
    if done["volatility"].status == "SHOCK":
        return _ready("BLOCKED", reason="VOLATILITY_SHOCK")
    if ctx.guardian is None:
        return _ready("UNAVAILABLE", reason="RISK_INPUTS_UNAVAILABLE")
    risk = ctx.guardian(done["management"].facts, ctx.quote, ctx.market.as_of_ms)
    if type(risk.allowed) is not bool:
        raise ValueError("Invalid hard guardian result")
    # State and account identifiers never enter the journal.
    return _ready("OK" if risk.allowed else "BLOCKED", reason=identifier(risk.reason),
                  lots=risk.lots, target_lots=risk.target_lots,
                  estimated_loss_cash=risk.estimated_loss_cash)


def independent_auditor(ctx, done):
    market = ctx.market
    sha256(market.source_hash)
    closed = market.bar["time_close"]
    stale = not 0 <= market.as_of_ms - closed <= ctx.policy.max_bar_age_ms
    uncausal = market.features["available_at"] != closed or any(
        d["confirmation_time"] != closed for d in market.candidates)
    if stale or uncausal:
        return _ready("UNAVAILABLE", reason="STALE_BAR" if stale else "INVALID_AVAILABILITY",
                      source=market.source)
    return _ready("OK", evidence="INTERNAL_RULE_AUDIT", source_hash=market.source_hash,
                  source=market.source, available_at_ms=closed,
                  inference="NONE" if ctx.model_service is None else "UNAVAILABLE",
                  empirical_certainty="INSUFFICIENT_EVIDENCE")


# Keys also identify dependencies. The risk check consumes a frozen management plan.
SPECIALISTS = (
    ("regime", market_regime), ("structure", market_structure), ("liquidity", liquidity),
    ("momentum", momentum), ("volatility", volatility), ("mtf", multi_timeframe),
    ("macro", macro_news), ("strategy", strategy), ("entry", entry_execution),
    ("management", trade_management), ("risk", risk_guardian), ("audit", independent_auditor),
)
_workers = BoundedSemaphore(len(SPECIALISTS))


def _invoke(function, context, done):
    if not _workers.acquire(blocking=False):
        return _ready("UNAVAILABLE", reason="WORKER_CAPACITY")
    reply = Queue(maxsize=1)

    def worker():
        try:
            result = function(context, done)
            if not isinstance(result, SpecialistResult):
                raise ValueError("Invalid specialist result")
            digest(asdict(result))
            reply.put(result)
        except Exception as error:
            reply.put(_ready("UNAVAILABLE", reason="SPECIALIST_ERROR", error_type=type(error).__name__))
        finally:
            _workers.release()

    thread = Thread(target=worker, daemon=True)
    try:
        thread.start()
    except Exception:
        _workers.release()
        return _ready("UNAVAILABLE", reason="WORKER_START_FAILED")
    try:
        return reply.get(timeout=context.policy.timeout_ms / 1000)
    except Empty:
        return _ready("UNAVAILABLE", reason="SPECIALIST_TIMEOUT")


def analyze(context):
    done, latency_ms = {}, {}
    for name, specialist in SPECIALISTS:
        start = perf_counter_ns()
        done[name] = _invoke(specialist, context, dict(done))
        latency_ms[name] = (perf_counter_ns() - start) / 1e6
    state, reason = _verdict(done)
    market = context.market
    result = {
        "symbol": market.symbol, "timeframe_ms": market.timeframe_ms,
        "as_of_ms": market.as_of_ms, "bar_close_ms": market.bar["time_close"],
        "state": state, "reason": reason,
        "specialists": {name: asdict(value) for name, value in done.items()},
        "inference_provider": None, "broker_orders": False,
    }
    result["trace_id"] = digest(result)
    result["latency_ms"] = latency_ms
    return result


def _verdict(done):
    if done["audit"].status != "OK":
        return "DATA_UNAVAILABLE", done["audit"].facts.get("reason", "AUDIT_UNAVAILABLE")
    if done["macro"].status != "OK" or done["macro"].facts.get("status") == "NEWS_DATA_UNAVAILABLE":
        return "DATA_UNAVAILABLE", "NEWS_DATA_UNAVAILABLE"
    if done["risk"].status == "BLOCKED":
        return "RISK_BLOCKED", done["risk"].facts["reason"]
    if done["macro"].facts["blocked"]:
        return "RISK_BLOCKED", done["macro"].facts["status"]
    for key in ("regime", "volatility", "mtf", "strategy"):
        if done[key].status == "UNAVAILABLE":
            return "DATA_UNAVAILABLE", done[key].facts.get("reason", "SPECIALIST_UNAVAILABLE")
    if done["strategy"].status == "DISAGREEMENT" or done["mtf"].status == "DISAGREEMENT":
        return "WAIT_CONFIRMATION", "ANALYTICAL_DISAGREEMENT"
    if any(done[key].status == "INVALIDATED" for key in ("strategy", "management", "entry")):
        return "INVALIDATED", "INVALIDATED_SETUP_OR_EXECUTION"
    if done["strategy"].status == "EXPIRED":
        return "SIGNAL_EXPIRED", "SETUP_EXPIRED"
    state = done["strategy"].status
    if state == "WATCH":
        return "WAIT_CONFIRMATION", "SETUP_NOT_CONFIRMED"
    if state != "OK":
        return "NO_TRADE", "NO_ELIGIBLE_STRATEGY"
    for key in ("entry", "management", "risk"):
        if done[key].status != "OK":
            return "DATA_UNAVAILABLE", done[key].facts.get("reason", "SPECIALIST_UNAVAILABLE")
    return ("BUY_CANDIDATE" if done["strategy"].facts["side"] == 1 else "SELL_CANDIDATE"), "HARD_CHECKS_PASSED"
