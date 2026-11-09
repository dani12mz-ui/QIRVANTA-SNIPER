"""Durable Bulgarian paper notifications and explicitly authorized Telegram delivery."""
import argparse
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import http.client
import json
from math import fsum
import os
from pathlib import Path
import re
import sqlite3
import sys
import time
from urllib.parse import quote

from .common import canonical, decode_json, digest, finite, identifier, milliseconds
from .journal import Journal

HOST = "api.telegram.org"
APPLICATION_ID = 0x514F4258
MAX_ATTEMPTS = 4
LEASE_MS = 30_000
MAX_AGE_MS = 15 * 60 * 1_000
ZERO_HASH = "0" * 64
TIMEFRAMES_MS = {"M5": 300_000, "M15": 900_000, "M30": 1_800_000}
NOTICE_LABELS = {
    "GAP_INVALIDATION": "Ценови скок обезсили плана",
    "GAP_RISK_CAP": "Ценови скок надвиши лимита за риск",
    "ENTRY_EXPIRED": "Срокът на сигнала изтече",
    "UNFILLED_LIMIT": "Лимитът остана неизпълнен до крайния срок",
    "FILL_RISK_RECHECK_BLOCKED": "Проверката на риска блокира симулирания вход",
    "OBSERVATION_GAP": "Прекъснати пазарни наблюдения",
}


class Telegram:
    """One destination. There is no recipient field on the outbox record."""

    def __init__(self, token, chat_id, authorized_chat_id):
        if (not isinstance(token, str) or not re.fullmatch(r"[0-9]{6,16}:[A-Za-z0-9_-]{25,100}", token)
                or not isinstance(chat_id, str) or not re.fullmatch(r"-?[1-9][0-9]{0,15}", chat_id)
                or abs(int(chat_id)) >= 2**52 or chat_id != authorized_chat_id):
            raise ValueError("Липсва валидна и изрично разрешена Telegram конфигурация.")
        self._token = token
        self._chat_id = int(chat_id)
        self.binding = digest(["telegram", token, chat_id])

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        if env.get("QIRVANTA_TELEGRAM_ENABLED") != "1":
            raise ValueError("Външната доставка през Telegram е изключена.")
        return cls(env.get("QIRVANTA_TELEGRAM_BOT_TOKEN"),
                   env.get("QIRVANTA_TELEGRAM_CHAT_ID"),
                   env.get("QIRVANTA_TELEGRAM_AUTHORIZED_CHAT_ID"))

    def send(self, text):
        """Return a sanitized outcome, reason, retry delay in ms, and message ID."""
        connection = http.client.HTTPSConnection(HOST, timeout=5)
        try:
            try:
                connection.connect()
            except (OSError, http.client.HTTPException):
                return "RETRY", "CONNECT_FAILED", 0, None
            try:
                payload = canonical({"chat_id": self._chat_id, "text": text,
                                     "link_preview_options": {"is_disabled": True}}).encode("utf-8")
                connection.request("POST", f"/bot{quote(self._token, safe=':')}/sendMessage",
                                   payload, {"Content-Type": "application/json"})
                response = connection.getresponse()
                raw = response.read(65_537)
                if len(raw) > 65_536:
                    return "UNKNOWN", "RESPONSE_LIMIT", 0, None
                body = decode_json(raw)
            except (OSError, ValueError, http.client.HTTPException):
                return "UNKNOWN", "RECEIPT_UNAVAILABLE", 0, None
        finally:
            connection.close()

        if isinstance(body, dict) and body.get("ok") is True and response.status == 200:
            result = body.get("result")
            chat = result.get("chat") if isinstance(result, dict) else None
            message_id = result.get("message_id") if isinstance(result, dict) else None
            if (isinstance(chat, dict) and type(chat.get("id")) is int and chat["id"] == self._chat_id
                    and type(message_id) is int and 0 < message_id < 2**63):
                return "DELIVERED", "PROVIDER_ACCEPTED", 0, message_id
            return "UNKNOWN", "RECEIPT_MISMATCH", 0, None
        if isinstance(body, dict) and body.get("ok") is False and body.get("error_code") == response.status:
            if response.status == 429:
                params = body.get("parameters")
                retry = params.get("retry_after") if isinstance(params, dict) else None
                if type(retry) is int and retry > 0:
                    return "RETRY", "RATE_LIMITED", min(retry * 1000, MAX_AGE_MS), None
            if response.status in (400, 401, 403, 404):
                return "FAILED", "PROVIDER_REJECTED", 0, None
        return "UNKNOWN", "RECEIPT_UNAVAILABLE", 0, None


def _message(kind, data, strategy, symbol, timeframe):
    if kind == "LOCAL_NOTIFICATION":
        body = data["text"]
    elif kind == "PAPER_PENDING":
        targets = " / ".join(map(str, data["targets"]))
        body = (f"Планът е допуснат за симулиран вход · {data['plan_id'][:12]}\n"
                f"Вход {data['reference']} | SL {data['initial_stop']} | TP1–3 {targets}")
    elif kind == "PAPER_COMPLETE":
        body = f"Симулирана позиция е затворена · план {data['plan_id'][:12]}"
    elif kind == "PAPER_MISSED":
        reason = data["reason"]
        body = f"Симулиран план без вход · {data['plan_id'][:12]} · {NOTICE_LABELS.get(reason, reason)}"
    elif kind == "PAPER_FEED_GAP":
        body = "Прекъсване в поредицата от наблюдавани котировки"
    else:
        return None
    return f"QIRVANTA · {symbol} {timeframe} · {strategy}\n{body}\nPAPER · Симулиран режим"


class Outbox:
    """SQLite delivery state. Ambiguous POST outcomes are never retried."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.db.row_factory = sqlite3.Row
        try:
            with self._write():
                app = self.db.execute("PRAGMA application_id").fetchone()[0]
                version = self.db.execute("PRAGMA user_version").fetchone()[0]
                if (app, version) == (0, 0):
                    if self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                        raise ValueError("Outbox path points to another database")
                    self.db.execute("""CREATE TABLE messages (
                        id TEXT PRIMARY KEY, text TEXT NOT NULL, at_ms INTEGER NOT NULL,
                        state TEXT NOT NULL,
                        attempts INTEGER NOT NULL DEFAULT 0, next_ms INTEGER NOT NULL DEFAULT 0,
                        lease_ms INTEGER NOT NULL DEFAULT 0, reason TEXT, provider_id INTEGER,
                        binding TEXT
                    )""")
                    self.db.execute("""CREATE TABLE streams (
                        id TEXT PRIMARY KEY, seq INTEGER NOT NULL, hash TEXT NOT NULL,
                        profile TEXT NOT NULL
                    )""")
                    self.db.execute("""CREATE TABLE transitions (
                        id TEXT PRIMARY KEY, marker TEXT NOT NULL
                    )""")
                    self.db.execute("CREATE INDEX message_queue ON messages (state, at_ms, id)")
                    self.db.execute(f"PRAGMA application_id={APPLICATION_ID}")
                    self.db.execute("PRAGMA user_version=1")
                elif (app, version) != (APPLICATION_ID, 1):
                    raise ValueError("Outbox path points to another database")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
        except BaseException:
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    @contextmanager
    def _write(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def _enqueue(self, event_id, text, at_ms, binding):
        identifier(event_id)
        milliseconds(at_ms)
        if not isinstance(text, str) or not text.strip() or len(text.encode("utf-16-le")) > 8192:
            raise ValueError("Invalid notification text")
        old = self.db.execute("SELECT text,at_ms,binding FROM messages WHERE id=?", (event_id,)).fetchone()
        if old:
            if tuple(old) != (text, at_ms, binding):
                raise ValueError("Outbox ID has conflicting content")
            return False
        state = "QUEUED" if binding else "LOCAL_ONLY"
        self.db.execute("INSERT INTO messages (id,text,at_ms,state,binding) VALUES (?,?,?,?,?)",
                        (event_id, text, at_ms, state, binding))
        return True

    def enqueue(self, event_id, text, at_ms):
        with self._write():
            return self._enqueue(event_id, text, at_ms, None)

    def enqueue_transition(self, scope, marker, event_id, text, at_ms):
        """Deduplicate consecutive health states across process restarts."""
        identifier(scope)
        stamp = digest(marker)
        with self._write():
            old = self.db.execute("SELECT marker FROM transitions WHERE id=?", (scope,)).fetchone()
            if old and old[0] == stamp:
                return False
            added = self._enqueue(event_id, text, at_ms, None)
            if added:
                self.db.execute(
                    "INSERT INTO transitions VALUES (?,?) ON CONFLICT(id) DO UPDATE SET marker=excluded.marker",
                    (scope, stamp))
            return added

    def status(self):
        counts = dict(self.db.execute("SELECT state,COUNT(*) FROM messages GROUP BY state"))
        return {state: counts.get(state, 0) for state in
                ("LOCAL_ONLY", "QUEUED", "RETRY", "IN_FLIGHT", "DELIVERED", "FAILED", "UNKNOWN")}

    def messages(self, limit=50):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Outbox limit must be 1–1000")
        return [dict(row) for row in self.db.execute(
            "SELECT id,text,at_ms,state,attempts,next_ms,reason,provider_id FROM messages "
            "ORDER BY at_ms DESC,rowid DESC LIMIT ?", (limit,))]

    def sync_journal(self, journal, stream_id, *, strategy, symbol, timeframe, limit=256,
                     transport=None, external_after_ms=None):
        """Mirror immutable journal notices locally with an atomic checkpoint."""
        identifier(stream_id)
        profile = canonical([identifier(strategy), identifier(symbol), identifier(timeframe)])
        if transport is not None or external_after_ms is not None:
            if not isinstance(transport, Telegram):
                raise ValueError("Expected a destination-bound Telegram transport")
            milliseconds(external_after_ms)
            protocol = (journal.state() or {}).get("protocol", {})
            if protocol.get("evidence_class") != "PROSPECTIVE_PAPER":
                raise ValueError("External delivery requires the prospective paper journal")
            if ((protocol.get("strategy_id"), protocol.get("symbol"), protocol.get("timeframe_ms"))
                    != (strategy, symbol, TIMEFRAMES_MS.get(timeframe)) or timeframe not in TIMEFRAMES_MS):
                raise ValueError("External delivery requires the registered strategy, symbol and timeframe")
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("Outbox limit must be 1–1000")
        if journal.db.in_transaction:
            raise ValueError("Outbox requires committed journal events")
        with self._write():
            old = self.db.execute("SELECT seq,hash,profile FROM streams WHERE id=?", (stream_id,)).fetchone()
            seq, last_hash = (old["seq"], old["hash"]) if old else (0, ZERO_HASH)
            if old:
                source = journal.db.execute("SELECT hash FROM events WHERE seq=?", (seq,)).fetchone()
                if old["profile"] != profile or (seq and (not source or source[0] != last_hash)):
                    raise ValueError("Outbox journal stream changed")
            added, scanned = 0, 0
            rows = journal.db.execute(
                "SELECT seq,kind,at_ms,data,previous,hash FROM events WHERE seq>? ORDER BY seq LIMIT ?",
                (seq, limit))
            for index, kind, at_ms, raw, parent, stamp in rows:
                data = json.loads(raw)
                if index != seq + 1 or parent != last_hash or stamp != digest([index, kind, at_ms, data, parent]):
                    raise ValueError("Outbox journal chain changed")
                body = _message(kind, data, strategy, symbol, timeframe)
                if body is not None:
                    binding = transport.binding if transport and at_ms >= external_after_ms else None
                    added += self._enqueue(digest(["journal-notice", stamp]), body, at_ms, binding)
                seq, last_hash, scanned = index, stamp, scanned + 1
            self.db.execute(
                "INSERT INTO streams VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET seq=excluded.seq,hash=excluded.hash",
                (stream_id, seq, last_hash, profile))
            return {"scanned": scanned, "added": added, "sequence": seq}

    def summary(self, journal, *, period, end_date, now_ms, strategy, symbol, timeframe):
        """Write one closed UTC period snapshot based on complete campaign events."""
        label = {"day": "Дневно", "week": "Седмично"}.get(period)
        end = date.fromisoformat(end_date)
        if (label is None or end.isoformat() != end_date or end <= date(1970, 1, 8)
                or (period == "week" and end.weekday() != 0)):
            raise ValueError("Expected a closed UTC day or a week ending on Monday")
        start = end - timedelta(days=1 if period == "day" else 7)
        end_ms = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp() * 1000)
        start_ms = end_ms - (end - start).days * 86_400_000
        if milliseconds(now_ms) < end_ms or start_ms <= 0:
            raise ValueError("Summary period must have elapsed")
        state = journal.state()
        if not state or journal.db.in_transaction:
            raise ValueError("Summary requires a committed paper protocol")
        values = [
            finite(json.loads(raw)["cash_net"])
            for (raw,) in journal.db.execute(
                "SELECT data FROM events WHERE kind='PAPER_COMPLETE' AND at_ms>=? AND at_ms<? ORDER BY seq",
                (start_ms, end_ms))
        ]
        profile = f"{identifier(symbol)} {identifier(timeframe)} · {identifier(strategy)}"
        result = f"{fsum(values):+.2f} във валутата на симулацията" if values else "няма затворени позиции"
        protocol = state["protocol"]
        coverage = identifier(protocol["evidence_class"])
        text = (f"QIRVANTA · {profile}\n{label} обобщение · {start} → {end} UTC\n"
                f"Записани затворени позиции {len(values)} · нетен резултат {result}\n"
                f"PAPER · Симулиран режим · {coverage}")
        event_id = digest(["paper-summary", identifier(protocol["protocol_id"]), period, end_date])
        return {"id": event_id, "added": self.enqueue(event_id, text, end_ms),
                "complete_campaigns": len(values)}

    def deliver_one(self, transport, now_ms):
        if not isinstance(transport, Telegram):
            raise ValueError("Expected a destination-bound Telegram transport")
        milliseconds(now_ms)
        with self._write():
            self.db.execute("UPDATE messages SET state='UNKNOWN',reason='INTERRUPTED_SEND' "
                            "WHERE state='IN_FLIGHT' AND lease_ms<=?", (now_ms,))
            self.db.execute("UPDATE messages SET state='FAILED',reason='EXPIRED' "
                            "WHERE state IN ('QUEUED','RETRY') AND at_ms<=?", (now_ms - MAX_AGE_MS,))
            if self.db.execute("SELECT 1 FROM messages WHERE state='IN_FLIGHT'").fetchone():
                return None
            row = self.db.execute(
                "SELECT id,text,at_ms,attempts,next_ms,binding FROM messages WHERE state IN ('QUEUED','RETRY') "
                "ORDER BY at_ms,id LIMIT 1").fetchone()
            if not row or max(row["at_ms"], row["next_ms"]) > now_ms:
                return None
            event_id, text, at_ms, attempts, _, binding = tuple(row)
            if binding != transport.binding:
                self.db.execute("UPDATE messages SET state='FAILED',reason='DESTINATION_CHANGED' WHERE id=?",
                                (event_id,))
                return {"id": event_id, "state": "FAILED", "reason": "DESTINATION_CHANGED"}
            attempts += 1
            self.db.execute("UPDATE messages SET state='IN_FLIGHT',attempts=?,lease_ms=?,binding=? WHERE id=?",
                            (attempts, now_ms + LEASE_MS, transport.binding, event_id))

        try:
            state, reason, delay, provider_id = transport.send(text)
        except Exception:
            state, reason, delay, provider_id = "UNKNOWN", "RECEIPT_UNAVAILABLE", 0, None
        next_ms = 0
        if state == "RETRY":
            next_ms = now_ms + max(delay, 1000 * 2**(attempts - 1))
            if attempts >= MAX_ATTEMPTS or next_ms >= at_ms + MAX_AGE_MS:
                state, reason, next_ms = "FAILED", "RETRY_LIMIT", 0
        with self._write():
            changed = self.db.execute(
                "UPDATE messages SET state=?,reason=?,next_ms=?,provider_id=? "
                "WHERE id=? AND state='IN_FLIGHT' AND attempts=?",
                (state, reason, next_ms, provider_id, event_id, attempts)).rowcount
        if not changed:
            return {"id": event_id, "state": "UNKNOWN", "reason": "INTERRUPTED_SEND"}
        return {"id": event_id, "state": state, "reason": reason}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--outbox", required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    listing = commands.add_parser("messages")
    listing.add_argument("--limit", type=int, default=50)
    sync = commands.add_parser("sync")
    sync.add_argument("--journal", required=True)
    sync.add_argument("--stream", required=True)
    sync.add_argument("--strategy", required=True)
    sync.add_argument("--symbol", required=True)
    sync.add_argument("--timeframe", required=True)
    summary = commands.add_parser("summary")
    for flag in ("journal", "strategy", "symbol", "timeframe", "end-date"):
        summary.add_argument("--" + flag, required=True)
    summary.add_argument("--period", choices=("day", "week"), required=True)
    commands.add_parser("deliver-one")
    args = parser.parse_args(argv)
    try:
        with Outbox(args.outbox) as outbox:
            if args.command == "status":
                result = outbox.status()
            elif args.command == "messages":
                result = outbox.messages(args.limit)
            elif args.command in ("sync", "summary"):
                if not Path(args.journal).is_file():
                    raise ValueError("Missing journal")
                profile = {"strategy": args.strategy, "symbol": args.symbol, "timeframe": args.timeframe}
                with Journal(args.journal) as journal:
                    if args.command == "sync":
                        result = outbox.sync_journal(journal, args.stream, **profile)
                    else:
                        result = outbox.summary(journal, **profile, period=args.period,
                                                end_date=args.end_date, now_ms=time.time_ns() // 1_000_000)
            else:
                result = outbox.deliver_one(Telegram.from_env(), time.time_ns() // 1_000_000)
        print(canonical(result))
        return 0
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
