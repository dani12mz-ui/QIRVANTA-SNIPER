"""Foreground market observation and isolated QIRVANTA paper portfolios."""
import argparse
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass
from functools import partial
import os
from pathlib import Path
import sqlite3

from .calendar import ArchivedCalendar, CalendarSnapshot
from .common import canonical, digest, identifier, milliseconds, read_json
from .intelligence import AnalysisContext, AnalysisPolicy, analyze, observe
from .journal import Journal
from .market import MarketStore, SessionCalendar, poll_readonly
from .mt5 import MT5Unavailable, connect_readonly, environment_status
from .phase11_outbox import Outbox, Telegram
from .phase11_signals import MAX_BODY_BYTES, PRODUCTION_CORE, SignalInbox, SignalPolicy
from .service import PaperProfile, now_ms, verified_bars


MODES = ("DEMO_READ_ONLY", "LIVE_READ_ONLY")
ATTACH_ENV = ("QIRVANTA_MT5_TERMINAL_PATH", "QIRVANTA_MT5_EXPECTED_SERVER",
              "QIRVANTA_MT5_EXPECTED_COMPANY")
GATEWAY_ENV = "QIRVANTA_TV_GATEWAY_KEY"


@dataclass(frozen=True)
class RuntimeConfig:
    roster: dict
    market: dict
    signals: tuple
    bars_limit: int
    instruments: dict
    config_hash: str

    @classmethod
    def read(cls, path):
        obj = read_json(path)
        if not isinstance(obj, dict) or set(obj) - {"roster", "market", "signals", "bars_limit"}:
            raise ValueError("Invalid Phase 11 configuration")
        roster, market = obj["roster"], obj["market"]
        allowed = {"source", "server", "mode", "cadence_s", "sessions_path", "calendar_path",
                   "warmup_manifests"}
        if (set(market) - allowed or market["mode"] not in MODES
                or roster["evidence_class"] != "PROSPECTIVE_PAPER"):
            raise ValueError("Foreground requires a preregistered MT5 read-only source")
        source, server = identifier(market["source"]), identifier(market["server"])
        cadence = market.get("cadence_s", 1)
        if type(cadence) not in (int, float) or not .25 <= cadence <= 2:
            raise ValueError("Invalid foreground polling cadence")
        for name in ("sessions_path", "calendar_path"):
            if name in market and not Path(market[name]).is_absolute():
                raise ValueError("Private artifact paths must be absolute")
        instruments, champion_symbols = {}, set()
        for instrument in roster["instruments"]:
            profile = PaperProfile(**instrument["profile"])
            minutes, remainder = divmod(instrument["timeframe_ms"], 60_000)
            if ((profile.source, profile.quote_server) != (source, server)
                    or profile.symbol in instruments or remainder
                    or minutes not in (5, 15, 30)):
                raise ValueError("Mismatched market identity or unsupported paper timeframe")
            instruments[profile.symbol] = minutes
            if instrument.get("champion_core_sha256") is not None:
                if instrument["champion_core_sha256"] != PRODUCTION_CORE:
                    raise ValueError("Only the frozen production core is eligible for champion intake")
                champion_symbols.add(profile.symbol)
        signals = tuple(SignalPolicy(**value) for value in obj.get("signals", ()))
        if (not instruments or any(p.server != server or instruments.get(p.broker_symbol) != p.minutes
                                   for p in signals)
                or {p.broker_symbol for p in signals} != champion_symbols
                or len(signals) != len(champion_symbols)):
            raise ValueError("TradingView mappings must belong to the registered broker instrument")
        warmup = market.get("warmup_manifests", {})
        if (not isinstance(warmup, dict) or set(warmup) - instruments.keys()
                or any(not Path(value).is_absolute() for value in warmup.values())):
            raise ValueError("Warmup manifests must identify registered symbols and absolute private paths")
        limit = obj.get("bars_limit", 3600)
        if type(limit) is not int or not 50 * (240 // min(instruments.values())) <= limit <= 6000:
            raise ValueError("Candle window cannot cover the frozen H4 context")
        return cls(roster, market, signals, limit, instruments, digest(obj))

    def store(self, root, *, read_only=False):
        return MarketStore(root / "market.sqlite3", source=self.market["source"],
                           server=self.market["server"], symbols=self.instruments,
                           read_only=read_only)

    def connector(self):
        terminal, server, company = (os.environ.get(name) for name in ATTACH_ENV)
        if not all((terminal, server, company)):
            raise MT5Unavailable("PRIVATE_MT5_IDENTITY_NOT_CONFIGURED")
        if server != self.market["server"]:
            raise MT5Unavailable("CONFIGURED_MT5_SERVER_MISMATCH")
        return partial(connect_readonly, terminal, expected_server=server,
                       expected_company=company, mode=self.market["mode"])


def _binding(root, cfg, *, register=False):
    binding = root / "runtime.sqlite3"
    if not register and not binding.is_file():
        raise ValueError("Runtime registration is missing")
    with Journal(binding) as journal, journal.transaction():
        expected = {"config_sha256": cfg.config_hash}
        if journal.state() is None and register:
            journal.set_state(expected, now_ms())
        if journal.state() != expected:
            raise ValueError("Runtime configuration differs from preregistration")


@contextmanager
def _foreground_lock(root):
    """Use an OS-owned lock that releases on process exit or a crash."""
    with (root / "foreground.lock").open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def _sessions(cfg):
    path = cfg.market.get("sessions_path")
    if not path:
        return None
    try:
        return SessionCalendar(cfg.market["server"], **read_json(path))
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _calendar(cfg):
    path = cfg.market.get("calendar_path")
    try:
        return ArchivedCalendar((CalendarSnapshot.from_file(path),)) if path else None
    except (OSError, ValueError, TypeError, KeyError):
        return None


def _warmup(cfg, *, known_at_ms):
    from .market_cache import cached_bars

    warmup = {}
    for row in cfg.roster["instruments"]:
        symbol = row["profile"]["symbol"]
        path = cfg.market.get("warmup_manifests", {}).get(symbol)
        if path is None:
            continue
        bars = cached_bars(path, source=cfg.market["source"], server=cfg.market["server"],
                           symbol=symbol, minutes=cfg.instruments[symbol], cutoff_ms=known_at_ms,
                           bid_only=True)
        if not bars or bars[-1]["time_close"] > row["history_cutoff_ms"]:
            raise ValueError("Warmup must be known at registration and close by the frozen history cutoff")
        warmup[symbol] = bars[-cfg.bars_limit:]
    return warmup


def _market_notice(outbox, store, update):
    if update.status == "DUPLICATE":
        return
    key = (update.status, update.reason, update.discontinuity)
    descriptions = {
        "ACCEPTED": "Приета свежа брокерска котировка",
        "STALE_SOURCE": "Остаряла котировка",
        "MISSING_DATA": "Липсват потвърдени пазарни данни",
    }
    label = descriptions.get(update.status, "Промяна в качеството на пазарните данни")
    text = f"QIRVANTA · {update.symbol} · {label} · {update.status}"
    if update.reason:
        text += f" · {update.reason}"
    if update.discontinuity:
        text += " · Прекъсване на наблюдението"
    scope = digest([store.source, store.server, update.symbol])
    outbox.enqueue_transition(scope, key, digest([scope, update.sequence]), text, update.observed_ms)


def _process_update(store, roster, inbox, cfg, update, warmup, *, clock):
    if not update.accepted:
        return
    tick = update.tick
    minutes = cfg.instruments[tick.symbol]
    start_ms = roster.groups[tick.symbol]["params"]["start_ms"]
    if tick.at_ms < start_ms:
        return
    bars = store.feature_bars(tick.symbol, minutes, cutoff_ms=update.observed_ms, limit=cfg.bars_limit)
    if bars and bars[-1]["time_close"] >= start_ms:
        preceding = [bar for bar in warmup.get(tick.symbol, ())
                     if bar["time_close"] <= bars[0]["time"]]
        bars = (preceding + bars)[-cfg.bars_limit:]
    else:
        bars = []
    at_ms = max(update.observed_ms, clock())
    signal = (inbox.eligible(tick.symbol, minutes * 60_000, at_ms=at_ms,
                             bar_close_ms=bars[-1]["time_close"])
              if bars and cfg.signals else None)
    roster.process(tick.symbol, tick.quote, source=store.source, bars=bars, calendar=_calendar(cfg),
                   session_open=tick.session_open is True, discontinuity=update.discontinuity,
                   at_ms=at_ms, champion_signal=signal, contract=update.contract)


def foreground(root, cfg, *, max_polls=None, telegram_after_ms=None, clock=now_ms,
               sleeper=None, connect_factory=None):
    from .phase11_portfolios import PaperRoster

    _binding(root, cfg)
    delivery = {}
    if telegram_after_ms is not None:
        if milliseconds(telegram_after_ms) <= clock():
            raise ValueError("External queue requires a future UTC delivery boundary")
        delivery = {"transport": Telegram.from_env(), "external_after_ms": telegram_after_ms}
    with _foreground_lock(root), ExitStack() as stack:
        roster = stack.enter_context(PaperRoster(root / "portfolios"))
        warmup = _warmup(cfg, known_at_ms=roster.manifest["registered_ms"])
        store = stack.enter_context(cfg.store(root))
        inbox = stack.enter_context(SignalInbox(root / "signals.sqlite3", cfg.signals))
        outbox = stack.enter_context(Outbox(root / "outbox.sqlite3"))
        options = {"clock": clock, "cadence_s": cfg.market.get("cadence_s", 1),
                   "session_calendar": _sessions(cfg), "max_polls": max_polls}
        if sleeper is not None:
            options["sleeper"] = sleeper
        for symbol in store.symbols:
            _sync_outbox(outbox, roster, cfg, symbol, **delivery)
        try:
            if connect_factory is None:
                preflight = environment_status()
                if not preflight["sdk_discoverable"]:
                    raise MT5Unavailable("METATRADER5_SDK_UNAVAILABLE")
                if not preflight["running_terminal_path_count"]:
                    raise MT5Unavailable("NO_RUNNING_MT5_TERMINAL")
                connect_factory = cfg.connector()
        except MT5Unavailable as error:
            for symbol in store.symbols:
                _market_notice(outbox, store,
                               store.mark_unavailable(symbol, observed_ms=clock(), reason=str(error)))
            return {"status": "BLOCKED_EXTERNAL", "reason": str(error),
                    "market": store.health(observed_ms=clock()), "broker_orders_enabled": False}
        accepted = 0
        poller = poll_readonly(connect_factory, store, **options)
        try:
            for update in poller:
                _market_notice(outbox, store, update)
                _process_update(store, roster, inbox, cfg, update, warmup, clock=clock)
                if update.accepted:
                    accepted += 1
                    _sync_outbox(outbox, roster, cfg, update.symbol, **delivery)
        finally:
            poller.close()
        return {"status": "OBSERVATION_COMPLETE" if accepted else "DATA_UNAVAILABLE",
                "accepted_quotes": accepted, "market": store.health(observed_ms=clock()),
                "broker_orders_enabled": False}


def _sync_outbox(outbox, roster, cfg, symbol, **delivery):
    for strategy, journal in roster.journal_streams(symbol).items():
        outbox.sync_journal(journal, digest([symbol, strategy]), strategy=strategy,
                            symbol=symbol, timeframe=f"M{cfg.instruments[symbol]}", **delivery)


def replay(path, expected, *, symbol, source):
    bars = verified_bars(path, expected)
    if not bars:
        raise ValueError("Historical input is empty")
    cutoff = bars[-1]["time_close"]
    market = observe(bars, symbol=symbol, source=source, source_hash=expected, as_of_ms=cutoff,
                     families=("S3", "S5", "S8"))
    result = analyze(AnalysisContext(market, AnalysisPolicy(research_candidates=True)))
    return {"evidence_class": "HISTORICAL", "bars": len(bars), "first_open_ms": bars[0]["time"],
            "last_close_ms": cutoff, "source": source, "source_sha256": expected,
            "broker_quote_supplied": False, "paper_campaigns": 0, "analysis": result}


def _bounded_bytes(path, limit):
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if not 0 < len(data) <= limit:
        raise ValueError("Invalid local signal artifact size")
    return data


def _intake(root, cfg, args):
    _binding(root, cfg)
    if not cfg.signals:
        raise ValueError("No chart mapping is registered")
    key = os.environ.get(GATEWAY_ENV, "").encode()
    signature = _bounded_bytes(args.signature_file, 96).decode("ascii").strip()
    body = _bounded_bytes(args.body, MAX_BODY_BYTES)
    with ExitStack() as stack:
        store = (stack.enter_context(cfg.store(root, read_only=True))
                 if (root / "market.sqlite3").is_file() else None)
        inbox = stack.enter_context(SignalInbox(root / "signals.sqlite3", cfg.signals))
        observed_ms = now_ms()
        result = inbox.ingest(body, signature, secret=key, received_ms=args.received_ms,
                              observed_ms=observed_ms,
                              quote_at=store.quote_at if store else lambda *_: None)
        with Outbox(root / "outbox.sqlite3") as outbox:
            for event in result:
                status = event["status"]
                if status in ("ADVISORY_READY", "DATA_UNAVAILABLE", "SIGNAL_EXPIRED"):
                    outbox.enqueue(digest(["tradingview", event["event_id"], status]),
                                   f"QIRVANTA · Получен TradingView план · {status} · {event['event_id']}",
                                   observed_ms)
        return result


def _status(root, cfg):
    from .phase11_portfolios import PaperRoster

    _binding(root, cfg)
    market = {"status": "DATA_UNAVAILABLE", "reason": "FOREGROUND_NOT_STARTED"}
    if (root / "market.sqlite3").is_file():
        with cfg.store(root, read_only=True) as store:
            market = store.health(observed_ms=now_ms())
    outbox = {}
    if (root / "outbox.sqlite3").is_file():
        with Outbox(root / "outbox.sqlite3") as delivery:
            outbox = delivery.status()
    with PaperRoster(root / "portfolios") as roster:
        return {"market": market, "outbox": outbox, "paper": roster.reports(),
                "broker_orders_enabled": False}


def _run(args):
    if args.command == "probe":
        return dict(environment_status(),
                    mt5_identity_configured=all(os.environ.get(v) for v in ATTACH_ENV),
                    tv_gateway_key_configured=bool(os.environ.get(GATEWAY_ENV)))
    if args.command == "replay":
        return replay(args.bars, args.sha256, symbol=args.symbol, source=args.source)
    from .phase11_portfolios import PaperRoster, register_roster

    if args.command == "report":
        with PaperRoster(args.root / "portfolios") as roster:
            return roster.reports()
    cfg = RuntimeConfig.read(args.config)
    if args.command == "register":
        args.root.mkdir(parents=True, exist_ok=True)
        with _foreground_lock(args.root):
            _warmup(cfg, known_at_ms=now_ms())
            _binding(args.root, cfg, register=True)
            return register_roster(args.root / "portfolios", cfg.roster)
    if args.command == "serve":
        return foreground(args.root, cfg, max_polls=args.max_polls,
                          telegram_after_ms=args.telegram_after_ms)
    if args.command == "status":
        return _status(args.root, cfg)
    if args.command == "signal":
        return _intake(args.root, cfg, args)
    if args.command == "pause":
        _binding(args.root, cfg)
        with _foreground_lock(args.root), PaperRoster(args.root / "portfolios") as roster:
            roster.set_pause(args.symbol, args.state == "on")
        return {"status": "OPERATOR_PAUSE_UPDATED", "symbol": args.symbol,
                "paused": args.state == "on", "broker_orders_enabled": False}
    from .market_cache import archive_bars

    if args.symbol not in cfg.instruments:
        raise ValueError("Historical request requires an exact registered symbol")
    with cfg.connector()() as reader:
        manifest = archive_bars(reader, args.root / "native-history", args.symbol, args.minutes,
                                args.start_ms, args.end_ms, source=cfg.market["source"],
                                retrieved_ms=now_ms, prior_manifest=args.prior_manifest,
                                session_calendar=_sessions(cfg))
    return {"status": read_json(manifest, limit=48 * 1024 * 1024)["status"],
            "manifest": str(manifest), "broker_orders_enabled": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("probe")
    report = commands.add_parser("report")
    report.add_argument("--root", type=Path, required=True)
    archive = commands.add_parser("replay")
    archive.add_argument("--bars", type=Path, required=True)
    for field in ("sha256", "symbol", "source"):
        archive.add_argument(f"--{field}", required=True)
    for action in ("register", "serve", "status", "signal", "pause", "cache-bars"):
        command = commands.add_parser(action)
        command.add_argument("--root", type=Path, required=True)
        command.add_argument("--config", type=Path, required=True)
        if action == "serve":
            command.add_argument("--max-polls", type=int)
            command.add_argument("--telegram-after-ms", type=int)
        elif action == "signal":
            command.add_argument("--body", type=Path, required=True)
            command.add_argument("--signature-file", type=Path, required=True)
            command.add_argument("--received-ms", type=int, required=True)
        elif action == "pause":
            command.add_argument("--symbol", required=True)
            command.add_argument("--state", choices=("on", "off"), required=True)
        elif action == "cache-bars":
            command.add_argument("--symbol", required=True)
            for field in ("minutes", "start-ms", "end-ms"):
                command.add_argument(f"--{field}", type=int, required=True)
            command.add_argument("--prior-manifest", type=Path)
    args = parser.parse_args(argv)
    try:
        result = _run(args)
        print(canonical(result))
        return 2 if isinstance(result, dict) and result.get("status") in (
            "BLOCKED_EXTERNAL", "DATA_UNAVAILABLE") else 0
    except KeyboardInterrupt:
        print(canonical({"status": "STOPPED_BY_OPERATOR", "broker_orders_enabled": False}))
        return 130
    except MT5Unavailable as error:
        print(canonical({"status": "BLOCKED_EXTERNAL", "reason": str(error),
                         "broker_orders_enabled": False}))
        return 2
    except (ValueError, OSError, TypeError, KeyError, sqlite3.Error) as error:
        print(canonical({"status": "FAILED_VALIDATION", "error_type": type(error).__name__,
                         "reason": "CHECK_PRIVATE_CONFIGURATION_ARTIFACT_AND_LOCAL_STATE",
                         "broker_orders_enabled": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
