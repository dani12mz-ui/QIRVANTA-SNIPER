"""Durable local intent reservation and reconciliation. No broker dependency."""
from contextlib import closing, contextmanager
from dataclasses import asdict, dataclass, replace
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from qirvanta_ultimate.mt5 import Mode, SymbolSnapshot, safe_mode
from qirvanta_ultimate.risk import BrokerSpec, RiskDecision, RiskPolicy, RiskState, TradePlan, assess_plan, millis, number


class IntentError(ValueError):
    pass


@dataclass(frozen=True)
class IntentRecord:
    plan_id: str
    intent_id: str
    symbol: str
    mode: str
    status: str
    lots: float
    reserved_risk_cash: float
    reserved_margin_cash: float
    expires_at_ms: int
    updated_at_ms: int


@dataclass(frozen=True)
class Reservation:
    intent: IntentRecord | None
    decision: RiskDecision | None
    replayed: bool = False


@dataclass(frozen=True)
class Transition:
    plan_id: str
    event_id: str
    from_status: str
    to_status: str
    at_ms: int


class IntentLedger:
    """Use a separate ledger per portfolio, broker server and execution mode.

    Supply broker/paper risk state exclusive of rows in this ledger. The ledger
    adds its own active risk, slot count and margin reservations atomically.
    SQLite serializes writes even when several local workers reserve at once.
    """
    def __init__(self, path, *, portfolio, server, account_currency, mode=Mode.RESEARCH):
        self._mode = safe_mode(mode)
        self._path = Path(path)
        if not all(isinstance(x, str) and x.strip() for x in (portfolio, server, account_currency)):
            raise IntentError('PORTFOLIO_BINDING_REQUIRED')
        self._server = server
        self._currency = account_currency
        with self._transaction() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS ledger (
                singleton INTEGER PRIMARY KEY CHECK (singleton=1), schema_version INTEGER NOT NULL,
                portfolio TEXT NOT NULL, server TEXT NOT NULL, currency TEXT NOT NULL,
                mode TEXT NOT NULL, last_write_ms INTEGER NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS intents (
                plan_id TEXT PRIMARY KEY NOT NULL, intent_id TEXT UNIQUE NOT NULL,
                symbol TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
                lots REAL NOT NULL, reserved_risk_cash REAL NOT NULL, reserved_margin_cash REAL NOT NULL,
                expires_at_ms INTEGER NOT NULL, updated_at_ms INTEGER NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS events (
                plan_id TEXT NOT NULL REFERENCES intents(plan_id), event_id TEXT PRIMARY KEY NOT NULL,
                from_status TEXT NOT NULL, to_status TEXT NOT NULL, at_ms INTEGER NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS vetoes (
                attempt INTEGER PRIMARY KEY, plan_id TEXT NOT NULL, reason TEXT NOT NULL,
                at_ms INTEGER NOT NULL)''')
            binding = (1, portfolio, server, account_currency, self._mode.value)
            old = db.execute('SELECT schema_version, portfolio, server, currency, mode FROM ledger').fetchone()
            if old is None:
                db.execute('INSERT INTO ledger VALUES (1, ?, ?, ?, ?, ?, 0)', binding)
            elif tuple(old) != binding:
                raise IntentError('LEDGER_PORTFOLIO_OR_SCHEMA_MISMATCH')

    @contextmanager
    def _transaction(self):
        with closing(sqlite3.connect(self._path, timeout=5, isolation_level=None)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('BEGIN IMMEDIATE')
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    @staticmethod
    def _clock(db, now_ms):
        last = db.execute('SELECT last_write_ms FROM ledger').fetchone()[0]
        if not millis(now_ms) or now_ms < last:
            raise IntentError('LEDGER_TIME_REWIND_OR_INVALID')
        db.execute('UPDATE ledger SET last_write_ms=?', (now_ms,))

    @staticmethod
    def _set_status(db, plan_id, event_id, before, after, now_ms):
        db.execute('UPDATE intents SET status=?, updated_at_ms=? WHERE plan_id=?', (after, now_ms, plan_id))
        event = Transition(plan_id, event_id, before, after, now_ms)
        db.execute('INSERT INTO events VALUES (?, ?, ?, ?, ?)', tuple(asdict(event).values()))
        return event

    def _expire(self, db, now_ms):
        old = db.execute('SELECT plan_id, intent_id FROM intents WHERE status=? AND expires_at_ms<=? ORDER BY plan_id',
                         ('RESERVED', now_ms)).fetchall()
        for row in old:
            self._set_status(db, row['plan_id'], 'system/expire/'+row['intent_id'], 'RESERVED', 'EXPIRED', now_ms)
        return len(old)

    def expire(self, now_ms):
        with self._transaction() as db:
            self._clock(db, now_ms)
            return self._expire(db, now_ms)

    def reserve(self, plan, quote, spec, state, policy, *, now_ms,
                capabilities=None, directional_volume_used=None, **assessment_inputs):
        if not isinstance(plan, TradePlan) or not isinstance(plan.plan_id, str) or not plan.plan_id:
            raise IntentError('PLAN_ID_REQUIRED')
        try:
            payload = json.dumps({'plan': asdict(plan), 'mode': self._mode.value},
                                 allow_nan=False, sort_keys=True, separators=(',', ':'))
        except (TypeError, ValueError):
            raise IntentError('INVALID_PLAN_PAYLOAD') from None
        intent_id = sha256(payload.encode('utf-8')).hexdigest()
        if not millis(now_ms):
            raise IntentError('INVALID_RESERVATION_TIMESTAMP')
        with self._transaction() as db:
            prior = db.execute('SELECT * FROM intents WHERE plan_id=?', (plan.plan_id,)).fetchone()
            if prior is not None:
                if prior['intent_id'] != intent_id:
                    raise IntentError('DUPLICATE_PLAN_ID_CHANGED_PAYLOAD')
                return Reservation(IntentRecord(**dict(prior)), None, True)
            self._clock(db, now_ms)
            self._expire(db, now_ms)

            def veto(reason):
                db.execute('INSERT INTO vetoes(plan_id,reason,at_ms) VALUES (?,?,?)',
                           (plan.plan_id, reason, now_ms))
                return Reservation(None, RiskDecision(False, reason))

            if not isinstance(spec, BrokerSpec) or (spec.server, spec.account_currency) != (self._server, self._currency):
                return veto('LEDGER_SPECIFICATION_BINDING_MISMATCH')
            if not isinstance(policy, RiskPolicy) or not millis(policy.max_spec_age_ms):
                return veto('INVALID_RISK_POLICY')
            broker_read = self._mode in (Mode.DEMO_READ_ONLY, Mode.LIVE_READ_ONLY)
            if broker_read and (not isinstance(capabilities, SymbolSnapshot)
                                or (capabilities.symbol, capabilities.server, capabilities.account_currency)
                                != (spec.symbol, self._server, self._currency)
                                or not millis(capabilities.observed_at_ms)
                                or not 0 <= now_ms-capabilities.observed_at_ms <= policy.max_spec_age_ms
                                or not capabilities.supports_market_bracket(plan.side)
                                or not number(capabilities.volume_limit) or capabilities.volume_limit < 0):
                return veto('BROKER_CAPABILITIES_UNVERIFIED_OR_UNSUPPORTED')
            count, risk, margin = db.execute('''SELECT COUNT(*), COALESCE(SUM(reserved_risk_cash),0),
                COALESCE(SUM(reserved_margin_cash),0) FROM intents WHERE status IN ('RESERVED','PAPER_OPEN')''').fetchone()
            if (not isinstance(state, RiskState) or not number(state.free_margin) or state.free_margin < 0
                    or not number(state.open_risk_cash) or state.open_risk_cash < 0
                    or not millis(state.open_positions)):
                return veto('INVALID_EXTERNAL_RESERVATION_STATE')
            adjusted = replace(state, open_positions=state.open_positions+count,
                               open_risk_cash=state.open_risk_cash+risk,
                               free_margin=max(0, state.free_margin-margin))
            decision = assess_plan(plan, quote, spec, adjusted, policy, now_ms=now_ms, **assessment_inputs)
            if not decision.allowed:
                return veto(decision.reason)
            if broker_read and capabilities.volume_limit > 0:
                local_lots = db.execute('''SELECT COALESCE(SUM(lots),0) FROM intents
                    WHERE symbol=? AND status IN ('RESERVED','PAPER_OPEN')''', (plan.symbol,)).fetchone()[0]
                # Counting both local directions is conservative when netting/margin interaction is unknown.
                if (not number(directional_volume_used) or directional_volume_used < 0
                        or directional_volume_used+local_lots+decision.lots > capabilities.volume_limit):
                    return veto('BROKER_DIRECTIONAL_VOLUME_LIMIT_OR_UNVERIFIED_USAGE')
            row = IntentRecord(plan.plan_id, intent_id, plan.symbol, self._mode.value, 'RESERVED',
                               decision.lots, decision.estimated_loss_cash,
                               decision.estimated_margin_cash, plan.expires_at_ms, now_ms)
            db.execute('INSERT INTO intents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', tuple(asdict(row).values()))
            db.execute('INSERT INTO events VALUES (?, ?, ?, ?, ?)',
                       (plan.plan_id, 'system/reserve/'+intent_id, 'NONE', 'RESERVED', now_ms))
            return Reservation(row, decision)

    def reconcile(self, plan_id, event_id, status, *, at_ms):
        """Apply a local transition once. Open/close records are paper-only."""
        if (not isinstance(event_id, str) or not event_id or event_id.startswith('system/')
                or status not in ('PAPER_OPEN', 'PAPER_CLOSED', 'RELEASED') or not millis(at_ms)):
            raise IntentError('INVALID_RECONCILIATION_EVENT')
        with self._transaction() as db:
            prior = db.execute('SELECT * FROM events WHERE event_id=?', (event_id,)).fetchone()
            if prior is not None:
                if (prior['plan_id'], prior['to_status'], prior['at_ms']) != (plan_id, status, at_ms):
                    raise IntentError('DUPLICATE_EVENT_ID_CHANGED_PAYLOAD')
                return Transition(**dict(prior))
            self._clock(db, at_ms)
            self._expire(db, at_ms)
            row = db.execute('SELECT * FROM intents WHERE plan_id=?', (plan_id,)).fetchone()
            if row is None:
                raise IntentError('UNKNOWN_INTENT')
            before = row['status']
            transitions = {'RESERVED': {'PAPER_OPEN', 'RELEASED'}, 'PAPER_OPEN': {'PAPER_CLOSED'}}
            if status not in transitions.get(before, set()):
                raise IntentError('ILLEGAL_INTENT_TRANSITION')
            if status.startswith('PAPER_') and self._mode is not Mode.PAPER:
                raise IntentError('PAPER_LEDGER_REQUIRED')
            return self._set_status(db, plan_id, event_id, before, status, at_ms)

    def records(self):
        with closing(sqlite3.connect(self._path)) as db:
            db.row_factory = sqlite3.Row
            return tuple(IntentRecord(**dict(r)) for r in db.execute('SELECT * FROM intents ORDER BY plan_id'))

    def audit(self):
        with closing(sqlite3.connect(self._path)) as db:
            db.row_factory = sqlite3.Row
            return {'events': tuple(dict(r) for r in db.execute('SELECT * FROM events ORDER BY rowid')),
                    'vetoes': tuple(dict(r) for r in db.execute('SELECT * FROM vetoes ORDER BY attempt'))}
