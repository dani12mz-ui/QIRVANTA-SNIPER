"""SQLite transactions, immutable intake receipts and local audit integrity."""
from contextlib import contextmanager
import json
import sqlite3

from .common import canonical, digest, identifier, milliseconds


class Journal:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=10)
        try:
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise ValueError("Unsupported journal schema version")
            if version == 0:
                existing = self.db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchone()
                if existing:
                    raise ValueError("Unrecognized existing database")
                self.db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS events (
                        seq INTEGER PRIMARY KEY, kind TEXT NOT NULL, at_ms INTEGER NOT NULL,
                        data TEXT NOT NULL, previous TEXT NOT NULL, hash TEXT NOT NULL UNIQUE
                    );
                    CREATE TABLE IF NOT EXISTS intake (id TEXT PRIMARY KEY, hash TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS materialized_state (
                        id INTEGER PRIMARY KEY CHECK (id=1), data TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS event_time ON events (at_ms);
                    PRAGMA user_version=1;
                    COMMIT;
                """)
            self.verify()
        except BaseException:
            self.db.close()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.db.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise

    def append(self, kind, at_ms, data):
        if not self.db.in_transaction:
            raise RuntimeError("Journal writes require a transaction")
        identifier(kind)
        milliseconds(at_ms)
        last = self.db.execute("SELECT seq,hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        seq, previous = (last[0] + 1, last[1]) if last else (1, "0" * 64)
        stamp = digest([seq, kind, at_ms, data, previous])
        self.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?)",
                        (seq, kind, at_ms, canonical(data), previous, stamp))
        return stamp

    def accept(self, request_id, payload, at_ms):
        """Must run in the same transaction as decisions and effects."""
        identifier(request_id)
        stamp = digest(payload)
        old = self.db.execute("SELECT hash FROM intake WHERE id=?", (request_id,)).fetchone()
        if old:
            if old[0] != stamp:
                raise ValueError("Request identity has conflicting content")
            return False
        self.append("INTAKE", at_ms, {"request_id": request_id, "payload_hash": stamp})
        self.db.execute("INSERT INTO intake VALUES (?,?)", (request_id, stamp))
        return True

    def state(self):
        row = self.db.execute("SELECT data FROM materialized_state WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    def set_state(self, state, at_ms):
        self.append("STATE_COMMIT", at_ms, {"state_hash": digest(state)})
        self.db.execute("INSERT INTO materialized_state VALUES (1,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                        (canonical(state),))

    def events(self, *, since_ms=None):
        if since_ms is None:
            rows = self.db.execute("SELECT seq,kind,at_ms,data FROM events ORDER BY seq")
        else:
            rows = self.db.execute("SELECT seq,kind,at_ms,data FROM events WHERE at_ms>=? ORDER BY seq",
                                   (since_ms,))
        return [{"sequence": seq, "kind": kind, "at_ms": at_ms, "data": json.loads(data)}
                for seq, kind, at_ms, data in rows]

    def verify(self):
        self.db.execute("BEGIN")
        try:
            previous, expected, state_hash, receipts = "0" * 64, 1, None, {}
            for seq, kind, at_ms, payload, parent, stamp in self.db.execute(
                    "SELECT seq,kind,at_ms,data,previous,hash FROM events ORDER BY seq"):
                data = json.loads(payload)
                if (seq != expected or parent != previous
                        or stamp != digest([seq, kind, at_ms, data, parent])):
                    raise ValueError("Audit chain mismatch")
                if kind == "STATE_COMMIT":
                    state_hash = data["state_hash"]
                if kind == "INTAKE":
                    if data["request_id"] in receipts:
                        raise ValueError("Repeated intake receipt")
                    receipts[data["request_id"]] = data["payload_hash"]
                previous, expected = stamp, expected + 1
            state = self.state()
            if state_hash != (digest(state) if state is not None else None):
                raise ValueError("Materialized state integrity mismatch")
            if receipts != dict(self.db.execute("SELECT id,hash FROM intake")):
                raise ValueError("Intake index integrity mismatch")
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("SQLite integrity check failed")
            return {"events": expected - 1, "head": previous, "status": "LOCALLY_VERIFIED"}
        finally:
            self.db.execute("ROLLBACK")
