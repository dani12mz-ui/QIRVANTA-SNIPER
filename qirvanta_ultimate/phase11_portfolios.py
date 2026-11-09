"""Immutable Phase 11 roster over the Phase 10 local paper engine."""
from contextlib import ExitStack
from dataclasses import asdict, replace
from math import isclose
from pathlib import Path
from re import fullmatch

from .calendar import ArchivedCalendar, CalendarSnapshot, assess_news
from .common import canonical, digest, finite, identifier, milliseconds, read_json, sha256
from .intelligence import SPECIALISTS, AnalysisContext, AnalysisPolicy, analyze, observe
from .journal import Journal
from .mt5 import SymbolSnapshot
from .paper import PaperProtocol, paper_step, register, report, set_manual_pause
from .risk import Quote, RiskDecision, grid_aligned, millis, number
from .service import PaperProfile, _guardian, _risk_state, implementation_hash, now_ms


STRATEGIES = ("CHAMPION", "S3", "S5", "S8")
EVIDENCE_CLASSES = ("PROSPECTIVE_PAPER", "HISTORICAL_REPLAY", "SYNTHETIC_TEST")
ROSTER_SCHEMA = "qirvanta.phase11.paper-roster.v1"
PROTOCOL_FIELDS = ("history_cutoff_ms", "start_ms", "end_ms", "timeframe_ms", "initial_cash")
PROTOCOL_DEFAULTS = dict(min_campaigns=100, min_profit_factor=1.1, min_net_r=.1,
                         max_drawdown_fraction=.1, max_quote_age_ms=5000,
                         max_hold_ms=43200000, max_gap_ms=30000,
                         profit_lock_r=0., atr_trail_multiplier=None)
RISK_CAPS = dict(portfolio_risk_fraction=.01, daily_loss_limit_fraction=.02,
                 weekly_loss_limit_fraction=.04, max_free_margin_fraction=.5)
META_FIELDS = {"profile", "contract_spec_source", "dataset_id", "champion_core_sha256", "risk_limits"}


def _phase11_implementation_hash():
    from hashlib import sha256 as file_hash

    mt5_sha256 = file_hash(Path(__file__).with_name("mt5.py").read_bytes()).hexdigest()
    return digest([implementation_hash(), mt5_sha256])


def _risk_limits(raw, profile):
    if not isinstance(raw, dict) or set(raw) - (RISK_CAPS.keys() | {"max_loss_cash", "min_net_reward_r"}):
        raise ValueError("Unknown Phase 11 hard risk limit")
    limits = {**RISK_CAPS, "max_positions": 1, "min_net_reward_r": 1., **raw}
    for key in limits:
        finite(limits[key], positive=True)
    if (any(limits[k] > v for k, v in RISK_CAPS.items())
            or limits["min_net_reward_r"] < 1 or profile.risk_fraction > limits["portfolio_risk_fraction"]
            or limits["daily_loss_limit_fraction"] > limits["weekly_loss_limit_fraction"]):
        raise ValueError("Phase 11 risk limits may only tighten the documented guardrails")
    return limits


def _registration(config, at_ms):
    if not isinstance(config, dict) or set(config) != {"roster_id", "source_commit", "evidence_class", "instruments"}:
        raise ValueError("Unexpected Phase 11 roster registration fields")
    roster_id = identifier(config["roster_id"])
    source_commit = config["source_commit"]
    if not isinstance(source_commit, str) or not fullmatch("[0-9a-f]{40}", source_commit):
        raise ValueError("A full Git source commit is required")
    evidence = config["evidence_class"]
    if evidence not in EVIDENCE_CLASSES:
        raise ValueError("Unknown Phase 11 input evidence class")
    if not isinstance(config["instruments"], list) or not 1 <= len(config["instruments"]) <= 2:
        raise ValueError("Register one or two separate instrument profiles")
    manifest = dict(schema=ROSTER_SCHEMA, roster_id=roster_id, registered_ms=at_ms,
                    implementation_hash=_phase11_implementation_hash(), source_commit=source_commit,
                    evidence_class=evidence, instruments=[])
    seen = set()
    for row in config["instruments"]:
        if set(row) - (META_FIELDS | set(PROTOCOL_FIELDS) | PROTOCOL_DEFAULTS.keys()):
            raise ValueError("Unexpected Phase 11 instrument fields")
        profile = PaperProfile(**row["profile"])
        if profile.symbol in seen:
            raise ValueError("A roster requires a distinct ledger set per instrument")
        seen.add(profile.symbol)
        dataset = identifier(row["dataset_id"])
        spec = identifier(row["contract_spec_source"])
        core = row.get("champion_core_sha256")
        if core is not None:
            sha256(core)
        params = {key: row[key] for key in PROTOCOL_FIELDS}
        params.update({key: row.get(key, default) for key, default in PROTOCOL_DEFAULTS.items()})
        if not at_ms < params["start_ms"]:
            raise ValueError("Register before the fixed paper start time")
        if (params["min_campaigns"] < 100 or params["min_profit_factor"] < 1.1
                or params["min_net_r"] < .1 or params["max_drawdown_fraction"] > .1
                or params["max_quote_age_ms"] > 5000 or params["max_hold_ms"] > 43200000
                or params["max_gap_ms"] > 30000):
            raise ValueError("Phase 11 thresholds may only tighten the registered defaults")
        group = dict(profile=asdict(profile), contract_spec_source=spec, dataset_id=dataset,
                     champion_core_sha256=core, risk_limits=_risk_limits(row.get("risk_limits", {}), profile),
                     params=params)
        profile_hash = digest(group)
        group["portfolios"] = []
        for strategy in STRATEGIES:
            identity = digest([roster_id, profile.symbol, params["timeframe_ms"], strategy])
            protocol = PaperProtocol(protocol_id=identity, source=profile.source, symbol=profile.symbol,
                                     families=(strategy,), strategy_id=strategy,
                                     implementation_hash=manifest["implementation_hash"], profile_hash=profile_hash,
                                     evidence_class=evidence, **params)
            group["portfolios"].append(dict(strategy_id=strategy, file=identity + ".sqlite", protocol=asdict(protocol)))
        manifest["instruments"].append(group)
    return manifest


def register_roster(root, config, *, at_ms=None):
    """Create an immutable manifest and independent SQLite ledgers before the fixed start."""
    if config["evidence_class"] == "PROSPECTIVE_PAPER" and at_ms is not None:
        raise ValueError("Prospective registration requires the actual system clock")
    registered = milliseconds(now_ms() if at_ms is None else at_ms)
    manifest = _registration(config, registered)
    root = Path(root)
    root.mkdir(parents=True, exist_ok=False)
    for group in manifest["instruments"]:
        for row in group["portfolios"]:
            with Journal(root / row["file"]) as journal:
                register(journal, PaperProtocol(**row["protocol"]), now_ms=registered)
    stamp = digest(manifest)
    staged = root / "roster.pending"
    staged.write_text(canonical({"manifest": manifest, "manifest_sha256": stamp}) + "\n", encoding="utf-8")
    staged.replace(root / "roster.json")
    return {"status": "REGISTERED", "roster_id": manifest["roster_id"], "manifest_sha256": stamp,
            "evidence_class": manifest["evidence_class"], "broker_orders": False}


def _contract_status(contract, profile, quote, at_ms, evidence, max_age):
    if contract is None and evidence != "PROSPECTIVE_PAPER":
        return None
    if not isinstance(contract, SymbolSnapshot):
        return "CONTRACT_METADATA_UNAVAILABLE"
    if not millis(contract.observed_at_ms) or not 0 <= at_ms - contract.observed_at_ms <= max_age:
        return "CONTRACT_METADATA_STALE"
    if (contract.symbol, contract.server, contract.account_currency) != (
            quote.symbol, quote.server, profile.account_currency):
        return "CONTRACT_IDENTITY_OR_CURRENCY_MISMATCH"
    exact = {
        "contract_size": "contract_size", "point": "point", "tick_size": "tick_size",
        "tick_value_loss": "loss_per_tick_lot", "tick_value_profit": "profit_per_tick_lot",
        "volume_min": "min_lot", "volume_step": "lot_step", "stops_level_points": "stops_level_points",
        "freeze_level_points": "freeze_level_points",
    }
    if (not number(contract.volume_max) or not number(contract.volume_limit) or contract.volume_limit < 0
            or any(not number(getattr(contract, k))
            or not isclose(getattr(contract, k), getattr(profile, v), rel_tol=1e-9, abs_tol=1e-12)
            for k, v in exact.items()) or profile.max_lot > contract.volume_max
            or (contract.volume_limit > 0 and profile.max_lot > contract.volume_limit)
            or contract.calculation_mode not in (3, 4, 5) or contract.currency_profit != profile.account_currency):
        return "CONTRACT_PROFILE_MISMATCH"
    return None


def _calendar(calendar, profile, at_ms):
    if calendar is None or profile.calendar_source is None:
        return None
    snapshot = calendar if isinstance(calendar, CalendarSnapshot) else calendar.snapshot(at_ms)
    if not isinstance(snapshot, CalendarSnapshot) or snapshot.source != profile.calendar_source:
        return None
    return ArchivedCalendar((snapshot,)) if snapshot.observed_ms <= at_ms else None


def _unavailable(profile, protocol, at_ms, reason, *, bar_close_ms=None):
    decision = {"symbol": profile.symbol, "timeframe_ms": protocol["timeframe_ms"], "as_of_ms": at_ms,
                "bar_close_ms": bar_close_ms, "state": "DATA_UNAVAILABLE", "reason": reason,
                "inference_provider": None, "broker_orders": False,
                "specialists": {name: {"status": "UNAVAILABLE", "facts": {"reason": reason}}
                                for name, _ in SPECIALISTS}}
    decision["specialists"]["audit"]["facts"]["source"] = profile.source
    decision["trace_id"] = digest(decision)
    return decision


def _champion_candidate(signal, pin, protocol, quote, frame, at_ms):
    if signal is None or pin is None or frame is None:
        return None
    from .phase11_signals import ChampionSignal

    if (not isinstance(signal, ChampionSignal) or signal.core_sha256 != pin
            or signal.symbol != protocol["symbol"] or signal.timeframe_ms != protocol["timeframe_ms"]
            or signal.bar_close_ms != frame.bar["time_close"] or signal.bar_close_ms < protocol["start_ms"]
            or not signal.bar_close_ms <= signal.quote_at_ms <= signal.received_ms <= at_ms < signal.expires_ms
            or quote.at_ms < signal.bar_close_ms or at_ms - signal.received_ms > protocol["max_quote_age_ms"]):
        return None
    return {"status": "CONFIRMED", "family": "CHAMPION", "side": signal.side,
            "signal_id": signal.event_id, "source_plan_id": signal.plan_id, "reasons": (),
            "confirmation_time": signal.bar_close_ms, "reference": signal.reference,
            "initial_stop": signal.stop, "targets": signal.targets, "expires_ms": signal.expires_ms,
            "atr_at_signal": signal.atr_at_signal}


class PaperRoster:
    """An independent local wallet per registered symbol and frozen strategy."""

    def __init__(self, root):
        root = Path(root)
        document = read_json(root / "roster.json")
        self.manifest = document["manifest"]
        self.manifest_sha256 = sha256(document["manifest_sha256"])
        if self.manifest["schema"] != ROSTER_SCHEMA or digest(self.manifest) != self.manifest_sha256:
            raise ValueError("Roster manifest integrity mismatch")
        self.implementation_hash = _phase11_implementation_hash()
        self.evidence_class = self.manifest["evidence_class"]
        self.groups = {group["profile"]["symbol"]: group for group in self.manifest["instruments"]}
        self._ledgers = {}
        self._frames = {}
        self._stack = ExitStack()
        try:
            for symbol, group in self.groups.items():
                for row in group["portfolios"]:
                    protocol = row["protocol"]
                    file = root / (sha256(protocol["protocol_id"]) + ".sqlite")
                    if not file.is_file():
                        raise ValueError("Registered portfolio journal missing")
                    journal = self._stack.enter_context(Journal(file))
                    if not journal.state() or journal.state()["protocol"] != protocol:
                        raise ValueError("Registered portfolio protocol differs from SQLite")
                    self._ledgers[symbol, row["strategy_id"]] = journal
        except BaseException:
            self.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._stack.close()

    def _time(self, at_ms):
        wall = now_ms()
        at_ms = milliseconds(wall if at_ms is None else at_ms)
        if self.evidence_class == "PROSPECTIVE_PAPER" and not 0 <= wall - at_ms <= 5000:
            raise ValueError("Prospective input must use a current local receipt timestamp")
        return at_ms

    def _frame(self, bars, bars_hash, profile, protocol, q, strategy):
        key = (profile.symbol, strategy)
        item = self._frames.get(key)
        if item and item[0] == bars_hash:
            return item[1]
        try:
            frame = observe(bars, symbol=q.symbol, source=profile.source, source_hash=bars_hash,
                            as_of_ms=q.at_ms, families=() if strategy == "CHAMPION" else (strategy,),
                            mintick=profile.tick_size)
            if frame.timeframe_ms != protocol["timeframe_ms"]:
                raise ValueError("Wrong registered timeframe")
        except (ValueError, KeyError, TypeError):
            frame = None
        self._frames[key] = (bars_hash, frame)
        return frame

    def process(self, symbol, quote, *, source, bars, calendar=None, session_open=False,
                discontinuity=False, at_ms=None, contract=None, champion_signal=None):
        at_ms = self._time(at_ms)
        if self.implementation_hash != self.manifest["implementation_hash"]:
            raise ValueError("Frozen implementation changed; register a new roster")
        group = self.groups[symbol]
        profile = PaperProfile(**group["profile"])
        params = group["params"]
        if (not isinstance(quote, Quote) or source != profile.source or quote.symbol != symbol
                or quote.server != profile.quote_server or type(session_open) is not bool
                or type(discontinuity) is not bool):
            raise ValueError("Paper source, broker, symbol or session mismatch")
        if not params["start_ms"] <= at_ms < params["end_ms"]:
            return {"status": "OUTSIDE_REGISTERED_WINDOW", "portfolios": {}, "broker_orders": False}
        if (not grid_aligned(quote.bid, profile.tick_size) or not grid_aligned(quote.ask, profile.tick_size)):
            raise ValueError("Paper quote is off the registered broker price grid")
        bars = [dict(row) for row in bars if row["time_close"] <= quote.at_ms]
        bars_hash = digest(bars)
        news = _calendar(calendar, profile, at_ms)
        snapshot = None if news is None else news.snapshot(at_ms)
        metadata = _contract_status(contract, profile, quote, at_ms, self.evidence_class, params["max_quote_age_ms"])
        costs = profile.costs()
        output = {}
        for row in group["portfolios"]:
            protocol = row["protocol"]
            strategy = row["strategy_id"]
            journal = self._ledgers[symbol, strategy]
            frame = self._frame(bars, bars_hash, profile, protocol, quote, strategy)
            external = None
            if strategy == "CHAMPION":
                external = _champion_candidate(champion_signal, group["champion_core_sha256"], protocol,
                                               quote, frame, at_ms)
                if external:
                    frame = replace(frame, candidates=(external,))
            contract_evidence = asdict(contract) if isinstance(contract, SymbolSnapshot) and metadata is None else None
            input_hash = digest([bars_hash, asdict(snapshot) if snapshot else None,
                                 contract_evidence, metadata, external])
            policy = AnalysisPolicy(research_candidates=True, max_quote_age_ms=protocol["max_quote_age_ms"],
                                    max_bar_age_ms=protocol["timeframe_ms"])
            usable = frame is not None and 0 <= at_ms - frame.bar["time_close"] < protocol["timeframe_ms"]

            def guard(state, plan, q, as_of, current_risk):
                if not usable:
                    return RiskDecision(False, "CLOSED_BAR_UNAVAILABLE_OR_STALE")
                if metadata:
                    return RiskDecision(False, metadata)
                if isinstance(contract, SymbolSnapshot) and not contract.supports_market_bracket(plan["side"]):
                    return RiskDecision(False, "CONTRACT_DIRECTION_OR_BRACKET_UNAVAILABLE")
                return _guardian(profile, state, plan, q, as_of, session_open=session_open,
                                 risk=current_risk, overrides=group["risk_limits"])

            def evaluate(state, q, as_of):
                if strategy == "CHAMPION" and external is None:
                    reason = "CHAMPION_CORE_UNPINNED" if group["champion_core_sha256"] is None else "NO_RECONCILED_CHAMPION_SIGNAL"
                    return _unavailable(profile, protocol, as_of, reason), costs
                if not usable:
                    return _unavailable(profile, protocol, as_of, "CLOSED_BAR_UNAVAILABLE_OR_STALE"), costs
                if state.get("gap_quote_ms") == q.at_ms or metadata:
                    return _unavailable(profile, protocol, as_of, metadata or "OBSERVATION_GAP",
                                        bar_close_ms=frame.bar["time_close"]), costs
                current = replace(frame, as_of_ms=as_of)
                paused = profile.manual_pause or state.get("operator_paused", False)
                current_risk = _risk_state(journal, profile, state, as_of)
                decision = analyze(AnalysisContext(current, policy, q, news, paused,
                                                    lambda plan, qq, t: guard(state, plan, qq, t, current_risk)))
                decision["portfolio"] = {"strategy_id": strategy, "protocol_id": protocol["protocol_id"],
                                         "dataset_id": group["dataset_id"],
                                         "contract_spec_source": group["contract_spec_source"]}
                decision["evidence"] = {"quote": asdict(q), "bars_sha256": bars_hash,
                                        "contract": contract_evidence, "input_sha256": input_hash}
                decision["trace_id"] = digest({k: v for k, v in decision.items() if k not in ("trace_id", "latency_ms")})
                return decision, costs

            def recheck(plan, q, state, as_of):
                paused = profile.manual_pause or state.get("operator_paused", False)
                if not usable or metadata or discontinuity or assess_news(news, as_of_ms=as_of, manual_pause=paused).blocked:
                    return False
                if strategy == "CHAMPION" and (external is None or external["source_plan_id"] != plan["source_plan_id"]):
                    return False
                current_risk = _risk_state(journal, profile, state, as_of, recheck=True)
                verified = guard(state, plan, q, as_of, current_risk)
                return verified.allowed and verified.lots >= plan["lots"]

            output[strategy] = paper_step(journal, f"quote:{quote.at_ms}", quote, now_ms=at_ms,
                                          implementation_hash=self.implementation_hash,
                                          profile_hash=protocol["profile_hash"], evaluate=evaluate, recheck=recheck,
                                          entry_kind=profile.entry_kind, input_hash=input_hash,
                                          session_open=session_open, discontinuity=discontinuity,
                                          atr=frame.features["atr"] if usable and not discontinuity else None)
        return {"status": "RECORDED", "symbol": symbol, "portfolios": output, "broker_orders": False}

    def reports(self, at_ms=None):
        at_ms = milliseconds(now_ms() if at_ms is None else at_ms)
        instruments = {}
        for symbol, group in self.groups.items():
            strategies = {}
            for row in group["portfolios"]:
                strategy = row["strategy_id"]
                journal = self._ledgers[symbol, strategy]
                summary = report(journal, at_ms=at_ms)
                if strategy == "CHAMPION" and summary["pending_submissions"] == 0:
                    summary["external_signal_status"] = ("BLOCKED_EXTERNAL" if group["champion_core_sha256"] is None
                                                         else "AWAITING_ELIGIBLE_RECONCILED_SIGNAL")
                strategies[strategy] = summary
            instruments[symbol] = {"dataset_id": group["dataset_id"], "portfolios": strategies,
                                   "contract_spec_source": group["contract_spec_source"]}
        return {"roster_id": self.manifest["roster_id"], "manifest_sha256": self.manifest_sha256,
                "evidence_class": self.evidence_class, "instruments": instruments,
                "implementation_drift": self.implementation_hash != self.manifest["implementation_hash"],
                "automatic_promotion": False, "broker_orders": False}

    def set_pause(self, symbol, paused, *, at_ms=None):
        at_ms = self._time(at_ms)
        for strategy in STRATEGIES:
            set_manual_pause(self._ledgers[symbol, strategy], paused, at_ms=at_ms)

    def journal_streams(self, symbol):
        """Borrow the open audit streams for read-only consumers such as the outbox."""
        return {strategy: self._ledgers[symbol, strategy] for strategy in STRATEGIES}
