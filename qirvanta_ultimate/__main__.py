"""Operator CLI for local QIRVANTA research and quote-driven paper simulation."""
import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from time import sleep

from . import __version__
from .calendar import ArchivedCalendar, CalendarSnapshot, assess_news
from .common import decode_json, digest, milliseconds, read_json
from .intelligence import AnalysisContext, AnalysisPolicy, analyze, observe
from .journal import Journal
from .paper import report, set_manual_pause
from .service import (create_protocol, implementation_hash, load_profile, now_ms,
                      process_packet, verified_bars)


def _print(data, *, stream=sys.stdout):
    print(json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False), file=stream, flush=True)


def _parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)
    review = sub.add_parser("review", help="Analyze an archived, hash-pinned OHLC snapshot")
    review.add_argument("--bars", required=True, type=Path)
    review.add_argument("--sha256", required=True)
    review.add_argument("--symbol", required=True)
    review.add_argument("--source", required=True)
    review.add_argument("--at-ms", required=True, type=int)
    review.add_argument("--family", action="append", default=[])
    review.add_argument("--calendar", type=Path)
    review.add_argument("--output", type=Path)
    calendar = sub.add_parser("calendar", help="Check a local release snapshot as of an exact cutoff")
    calendar.add_argument("--snapshot", type=Path, required=True)
    calendar.add_argument("--at-ms", type=int, required=True)
    calendar.add_argument("--scope", default="US_MAJOR_RELEASES")
    sub.add_parser("fingerprint", help="Print the frozen implementation identity")
    reg = sub.add_parser("register", help="Freeze an explicit future paper protocol")
    reg.add_argument("--db", required=True, type=Path)
    reg.add_argument("--profile", required=True, type=Path)
    reg.add_argument("--draft", required=True, type=Path)
    monitor = sub.add_parser("serve", help="Consume a local spool with simulated fills only")
    monitor.add_argument("--db", required=True, type=Path)
    monitor.add_argument("--profile", required=True, type=Path)
    monitor.add_argument("--spool", required=True, type=Path)
    monitor.add_argument("--bars-root", required=True, type=Path)
    monitor.add_argument("--calendar-root", type=Path)
    monitor.add_argument("--once", action="store_true")
    monitor.add_argument("--poll-seconds", type=float, default=.25)
    pause = sub.add_parser("pause", help="Record an explicit paper entry pause or resume")
    pause.add_argument("--db", required=True, type=Path)
    pause.add_argument("state", choices=("on", "off"))
    for command in ("health", "report", "notifications"):
        item = sub.add_parser(command)
        item.add_argument("--db", required=True, type=Path)
    return parser


def _review(args):
    bars = verified_bars(args.bars, args.sha256)
    provider = ArchivedCalendar((CalendarSnapshot.from_file(args.calendar),)) if args.calendar else None
    market = observe(bars, symbol=args.symbol, source=args.source, source_hash=args.sha256,
                     as_of_ms=args.at_ms, families=tuple(args.family))
    result = analyze(AnalysisContext(market, AnalysisPolicy(), news_provider=provider))
    result["evidence_class"] = "HISTORICAL_RESEARCH"
    if args.output:
        with args.output.open("x", encoding="utf-8", newline="\n") as output:
            _print(result, stream=output)
    _print(result)


def _reject(journal, content_hash, error, at):
    with journal.transaction():
        if journal.accept("rejected:" + content_hash, {"content_hash": content_hash}, at):
            journal.append("INTAKE_REJECTED", at, {"input_hash": content_hash, "error_type": type(error).__name__})


def _spool_batch(journal, folder, seen):
    current, packets, read_errors = set(), [], 0
    for path in folder.glob("*.json"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            before = path.stat()
            key = (path.name, before.st_size, before.st_mtime_ns, before.st_ino)
            if key in seen:
                current.add(key)
                continue
            with path.open("rb") as stream:
                data = stream.read(65537)
            after = path.stat()
            if (after.st_size, after.st_mtime_ns, after.st_ino) != key[1:]:
                continue
        except OSError:
            read_errors += 1
            continue
        current.add(key)
        stamp = hashlib.sha256(data + str(before.st_size).encode()).hexdigest()
        try:
            if len(data) > 65536:
                raise ValueError("Quote packet exceeds size limit")
            packet = decode_json(data)
            timestamp = milliseconds(packet["quote"]["at_ms"])
            packets.append((timestamp, path.name, packet, stamp))
        except (TypeError, ValueError, KeyError) as error:
            _reject(journal, stamp, error, now_ms())
    packets.sort(key=lambda p: (p[0], p[1]))
    return current, packets, read_errors


def _serve(journal, args):
    if not .05 <= args.poll_seconds <= 30:
        raise ValueError("Polling interval outside .05–30 seconds")
    profile = load_profile(args.profile)
    state = journal.state()
    if state is None:
        raise ValueError("Register a paper protocol before startup")
    protocol = state["protocol"]
    if (protocol["implementation_hash"] != implementation_hash()
            or protocol["profile_hash"] != digest(asdict(profile))):
        raise ValueError("Paper source or profile drift")
    if not args.spool.is_dir() or not args.bars_root.is_dir():
        raise ValueError("Spool and bars directories must exist")
    _print({"status": "LOCAL_PAPER_MONITOR_STARTED", "broker_orders": False,
            "transport": "LOCAL_SPOOL", "evidence": "OPERATOR_DECLARED"})
    seen = set()
    while True:
        if now_ms() >= journal.state()["protocol"]["end_ms"]:
            _print({"status": "PROTOCOL_ENDED", "broker_orders": False})
            return
        current, packets, read_errors = _spool_batch(journal, args.spool, seen)
        if read_errors:
            _print({"status": "SPOOL_READ_DEFERRED", "count": read_errors})
        for _, _, packet, stamp in packets:
            try:
                result = process_packet(journal, profile, packet, bars_root=args.bars_root,
                                        calendar_root=args.calendar_root)
                _print(result)
            except (OSError, TypeError, ValueError, KeyError) as error:
                _reject(journal, stamp, error, now_ms())
                _print({"status": "INTAKE_REJECTED", "error_type": type(error).__name__, "input_hash": stamp})
        seen = current
        if args.once:
            return
        sleep(args.poll_seconds)


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        if args.command == "fingerprint":
            _print({"version": __version__, "implementation_hash": implementation_hash()})
            return 0
        if args.command == "review":
            _review(args)
            return 0
        if args.command == "calendar":
            provider = ArchivedCalendar((CalendarSnapshot.from_file(args.snapshot),))
            _print(asdict(assess_news(provider, as_of_ms=args.at_ms, required_scope=(args.scope,))))
            return 0
        if args.command != "register" and not args.db.is_file():
            raise ValueError("Journal does not exist")
        with Journal(args.db) as journal:
            if args.command == "register":
                _print(create_protocol(journal, load_profile(args.profile), read_json(args.draft), at_ms=now_ms()))
            elif args.command == "serve":
                _serve(journal, args)
            elif args.command == "pause":
                set_manual_pause(journal, args.state == "on", at_ms=now_ms())
                _print({"status": "RECORDED", "paused": args.state == "on", "broker_orders": False})
            elif args.command == "health":
                verified = journal.verify()
                events = journal.events()
                state = journal.state()
                observed = datetime.now(timezone.utc).isoformat()
                age = None if not state or state["last_quote_ms"] is None else now_ms() - state["last_quote_ms"]
                freshness = ("UNAVAILABLE" if age is None else "FUTURE_CLOCK_SKEW" if age < 0 else
                             "STALE" if age > state["protocol"]["max_quote_age_ms"] else "FRESH")
                _print({"observed_utc": observed, "journal": verified, "event_counts": Counter(e["kind"] for e in events),
                        "last_quote_age_ms": age, "quote_freshness": freshness,
                        "operator_paused": bool(state and state.get("operator_paused")),
                        "broker_connection": "NOT_CONNECTED_BY_LOCAL_SERVICE", "model_service": "NONE",
                        "orders_enabled": False, "mode": "PAPER" if state else "RESEARCH"})
            elif args.command == "report":
                _print(report(journal, at_ms=now_ms()))
            else:
                _print([e for e in journal.events() if e["kind"] == "LOCAL_NOTIFICATION"])
        return 0
    except KeyboardInterrupt:
        _print({"status": "STOPPED", "broker_orders": False})
        return 0
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as error:
        _print({"status": "INPUT_OR_STATE_REJECTED", "error_type": type(error).__name__,
                "broker_orders": False}, stream=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
