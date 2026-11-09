"""Causal, bounded quote ingestion and sampled UTC BID candles for paper use."""
from contextlib import contextmanager
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
import sqlite3
from time import sleep, time_ns

from .common import finite, milliseconds
from .risk import Quote, grid_aligned

TIMEFRAMES = (1, 5, 15, 30, 60, 240, 1440)
_MAX_SYMBOLS = 16
_MAX_LOOKBACK = 10_000
_APP_ID = 0x514D4B54
_LIVE = 'LIVE_INCOMPLETE'
_CLOSED = 'CLOSED_CONFIRMED'
_MISSING = 'MISSING_DATA'
_STALE = 'STALE_SOURCE'


def _now_ms():
    return time_ns() // 1_000_000


def stream_name(value):
    if (not isinstance(value, str) or not 0 < len(value) <= 160 or value != value.strip()
            or not value.isprintable()):
        raise ValueError('INVALID_MARKET_STREAM_IDENTITY')
    return value


@dataclass(frozen=True)
class MarketTick:
    symbol: str
    server: str
    bid: float
    ask: float
    last: float | None
    at_ms: int
    tick_size: float
    session_open: bool | None = None

    def __post_init__(self):
        stream_name(self.symbol)
        stream_name(self.server)
        milliseconds(self.at_ms)
        for price in (self.bid, self.ask, self.tick_size):
            finite(price, positive=True)
        if self.last is not None:
            finite(self.last, positive=True)
        if (self.bid > self.ask or not grid_aligned(self.bid, self.tick_size)
                or not grid_aligned(self.ask, self.tick_size)
                or self.session_open is not None and type(self.session_open) is not bool):
            raise ValueError('INVALID_MARKET_TICK')

    @property
    def quote(self):
        return Quote(self.symbol, self.bid, self.ask, self.at_ms, self.server)

    @property
    def spread(self):
        return self.ask - self.bid


@dataclass(frozen=True)
class MarketUpdate:
    status: str
    sequence: int
    tick: MarketTick | None
    observed_ms: int
    discontinuity: bool
    symbol: str
    closed_minutes: tuple[int, ...] = ()
    contract: object | None = None
    reason: str | None = None

    @property
    def accepted(self):
        return self.status == 'ACCEPTED'


class SessionCalendar:
    """Reviewed, exact-server UTC windows with an explicit coverage horizon."""
    def __init__(self, server, windows, *, coverage_start_ms, coverage_end_ms, evidence):
        self.server = stream_name(server)
        self.coverage = (milliseconds(coverage_start_ms), milliseconds(coverage_end_ms))
        self.evidence = stream_name(evidence)
        if self.coverage[0] >= self.coverage[1] or not 0 < len(windows) <= _MAX_SYMBOLS:
            raise ValueError('INVALID_SESSION_CALENDAR')
        self.windows = {}
        for symbol, intervals in windows.items():
            stream_name(symbol)
            if len(intervals) > 2048:
                raise ValueError('SESSION_CALENDAR_TOO_LARGE')
            verified = []
            previous_end = self.coverage[0]
            for left, right in intervals:
                milliseconds(left)
                milliseconds(right)
                if not previous_end <= left < right <= self.coverage[1]:
                    raise ValueError('INVALID_SESSION_WINDOW')
                verified.append((left, right))
                previous_end = right
            self.windows[symbol] = tuple(verified)

    def status(self, server, symbol, tick_ms, observed_ms):
        windows = self.windows.get(symbol)
        if server != self.server or windows is None:
            return None
        if not self.coverage[0] <= tick_ms <= observed_ms < self.coverage[1]:
            return None
        return any(left <= tick_ms <= observed_ms < right for left, right in windows)


_SCHEMA = """
CREATE TABLE config (id INTEGER PRIMARY KEY CHECK(id=1), source TEXT, server TEXT,
                     symbols TEXT, gap_ms INTEGER, max_age_ms INTEGER);
CREATE TABLE state (
    symbol TEXT PRIMARY KEY, at_ms INTEGER, observed_ms INTEGER, bid REAL, ask REAL, last REAL,
    tick_size REAL, session_open INTEGER, latched INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'MISSING_DATA', reason TEXT, last_discontinuity_ms INTEGER
);
CREATE TABLE candles (
    symbol TEXT, minutes INTEGER, start_ms INTEGER, end_ms INTEGER,
    open REAL, high REAL, low REAL, close REAL, sample_count INTEGER NOT NULL,
    status TEXT NOT NULL, complete INTEGER NOT NULL, available_ms INTEGER,
    PRIMARY KEY(symbol, minutes, start_ms)
) WITHOUT ROWID;
CREATE UNIQUE INDEX active_candle ON candles(symbol, minutes) WHERE available_ms IS NULL;
CREATE INDEX confirmed_candle ON candles(symbol, minutes, start_ms) WHERE status='CLOSED_CONFIRMED';
CREATE TABLE audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, observed_ms INTEGER NOT NULL,
    status TEXT NOT NULL, reason TEXT, broker_at_ms INTEGER, discontinuity INTEGER NOT NULL,
    bid REAL, ask REAL, session_open INTEGER
);
CREATE INDEX audit_symbol_time ON audit(symbol, observed_ms, sequence);
CREATE TABLE counts (status TEXT PRIMARY KEY, n INTEGER NOT NULL);
"""


class MarketStore:
    """Single ingestion owner. Transactions persist quote state, candles and audit together."""
    def __init__(self, path, *, source, server, symbols, max_quote_age_ms=5000, gap_ms=5000,
                 read_only=False):
        self.source = stream_name(source)
        self.server = stream_name(server)
        self.symbols = tuple(stream_name(symbol) for symbol in symbols)
        if not 0 < len(self.symbols) <= _MAX_SYMBOLS or len(set(self.symbols)) != len(self.symbols):
            raise ValueError('INVALID_MARKET_SYMBOLS')
        if (type(max_quote_age_ms) is not int or type(gap_ms) is not int
                or not 0 < max_quote_age_ms <= 60_000 or not 0 < gap_ms < 60_000):
            raise ValueError('INVALID_MARKET_FRESHNESS_POLICY')
        self.max_quote_age_ms = max_quote_age_ms
        self.gap_ms = gap_ms
        self.read_only = read_only
        location = Path(path).resolve().as_uri() + '?mode=ro' if read_only else Path(path)
        self._db = sqlite3.connect(location, uri=read_only, timeout=2, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._open()
        except BaseException:
            self._db.close()
            raise

    def _open(self):
        db = self._db
        application = db.execute('PRAGMA application_id').fetchone()[0]
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if (application, version) == (0, 0) and not self.read_only:
            if db.execute('SELECT 1 FROM sqlite_schema LIMIT 1').fetchone():
                raise ValueError('MARKET_CACHE_DATABASE_TYPE_MISMATCH')
            db.executescript(f'BEGIN IMMEDIATE; {_SCHEMA} PRAGMA application_id={_APP_ID};'
                             'PRAGMA user_version=1; COMMIT;')
        elif (application, version) != (_APP_ID, 1):
            raise ValueError('MARKET_CACHE_DATABASE_TYPE_MISMATCH')
        identity = (self.source, self.server, '\n'.join(sorted(self.symbols)),
                    self.gap_ms, self.max_quote_age_ms)
        if self.read_only:
            row = db.execute('SELECT source,server,symbols,gap_ms,max_age_ms FROM config WHERE id=1').fetchone()
            if row is None or tuple(row) != identity:
                raise ValueError('MARKET_CACHE_STREAM_OR_POLICY_MISMATCH')
            return
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        with self._transaction():
            row = db.execute('SELECT source,server,symbols,gap_ms,max_age_ms FROM config WHERE id=1').fetchone()
            if row is None:
                db.execute('INSERT INTO config VALUES(1,?,?,?,?,?)', identity)
                db.executemany('INSERT INTO state(symbol) VALUES(?)', ((symbol,) for symbol in self.symbols))
            elif tuple(row) != identity:
                raise ValueError('MARKET_CACHE_STREAM_OR_POLICY_MISMATCH')
            else:
                db.execute("UPDATE state SET latched=1,status='MISSING_DATA',reason='PROCESS_RESTART'")
                db.execute('UPDATE candles SET complete=0 WHERE available_ms IS NULL')

    @contextmanager
    def _transaction(self):
        if self.read_only:
            raise ValueError('MARKET_CACHE_READ_ONLY')
        self._db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self._db.commit()
        except BaseException:
            self._db.rollback()
            raise

    def __enter__(self):
        return self

    def __exit__(self, *_):
        try:
            if not self.read_only:
                with self._transaction():
                    self._db.execute("UPDATE state SET latched=1,status='MISSING_DATA',reason='MARKET_WRITER_STOPPED'")
                    self._db.execute('UPDATE candles SET complete=0 WHERE available_ms IS NULL')
        finally:
            self._db.close()

    def _state(self, symbol):
        if symbol not in self.symbols:
            raise ValueError('UNCONFIGURED_MARKET_SYMBOL')
        return self._db.execute('SELECT * FROM state WHERE symbol=?', (symbol,)).fetchone()

    def _record(self, symbol, observed_ms, status, *, reason=None, tick=None, discontinuity=False,
                closed_minutes=(), contract=None, broker_at_ms=None):
        at_ms = tick.at_ms if tick else broker_at_ms
        seq = self._db.execute(
            'INSERT INTO audit(symbol,observed_ms,status,reason,broker_at_ms,discontinuity,bid,ask,session_open) '
            'VALUES(?,?,?,?,?,?,?,?,?)',
            (symbol, observed_ms, status, reason, at_ms, discontinuity,
             None if tick is None else tick.bid, None if tick is None else tick.ask,
             None if tick is None else tick.session_open),
        ).lastrowid
        self._db.execute('INSERT INTO counts VALUES(?,1) ON CONFLICT(status) DO UPDATE SET n=n+1', (status,))
        if discontinuity:
            self._db.execute('UPDATE state SET last_discontinuity_ms=max(coalesce(last_discontinuity_ms,0),?) '
                             'WHERE symbol=?', (observed_ms, symbol))
        return MarketUpdate(status, seq, tick, observed_ms, discontinuity, symbol, closed_minutes, contract, reason)

    def _unavailable(self, symbol, observed_ms, status, reason, *, broker_at_ms=None):
        self._db.execute('UPDATE state SET latched=1,status=?,reason=? WHERE symbol=?', (status, reason, symbol))
        self._db.execute('UPDATE candles SET complete=0 WHERE symbol=? AND available_ms IS NULL', (symbol,))
        return self._record(symbol, observed_ms, status, reason=reason, discontinuity=True,
                            broker_at_ms=broker_at_ms)

    def mark_unavailable(self, symbol, *, observed_ms, reason='SOURCE_UNAVAILABLE'):
        milliseconds(observed_ms)
        stream_name(reason)
        with self._transaction():
            self._state(symbol)
            return self._unavailable(symbol, observed_ms, _MISSING, reason)

    def ingest(self, tick, *, observed_ms, contract=None):
        if not isinstance(tick, MarketTick) or tick.server != self.server:
            raise ValueError('MARKET_TICK_STREAM_MISMATCH')
        milliseconds(observed_ms)
        with self._transaction():
            previous = self._state(tick.symbol)
            status, reason = self._quality(tick, observed_ms, previous)
            if status == 'DUPLICATE':
                return self._record(tick.symbol, observed_ms, status, reason=reason, broker_at_ms=tick.at_ms)
            if status:
                return self._unavailable(tick.symbol, observed_ms, status, reason, broker_at_ms=tick.at_ms)
            restart = previous['at_ms'] is None or bool(previous['latched'])
            gap = previous['at_ms'] is not None and (
                tick.at_ms - previous['at_ms'] > self.gap_ms
                or observed_ms - previous['observed_ms'] > self.gap_ms
            )
            grid_changed = previous['tick_size'] is not None and previous['tick_size'] != tick.tick_size
            closed_session = tick.session_open is False or previous['session_open'] == 0
            discontinuity = restart or gap or grid_changed or closed_session
            closed = self._advance_candles(tick, observed_ms, discontinuity)
            self._db.execute(
                'UPDATE state SET at_ms=?,observed_ms=?,bid=?,ask=?,last=?,tick_size=?,session_open=?,'
                'latched=0,status=?,reason=NULL WHERE symbol=?',
                (tick.at_ms, observed_ms, tick.bid, tick.ask, tick.last, tick.tick_size, tick.session_open,
                 'ACCEPTED', tick.symbol),
            )
            reason = None
            if discontinuity:
                reason = ('SOURCE_GAP' if gap else 'TICK_SIZE_CHANGED' if grid_changed else
                          'SESSION_CLOSED' if closed_session else previous['reason'] or 'INITIAL_QUOTE')
            return self._record(tick.symbol, observed_ms, 'ACCEPTED', tick=tick, reason=reason,
                                discontinuity=discontinuity, closed_minutes=closed, contract=contract)

    def _quality(self, tick, observed_ms, previous):
        last_at = previous['at_ms']
        if previous['observed_ms'] is not None and observed_ms < previous['observed_ms']:
            return _STALE, 'OBSERVATION_CLOCK_MOVED_BACKWARD'
        if tick.at_ms > observed_ms:
            return _STALE, 'FUTURE_PROVIDER_TIMESTAMP'
        if observed_ms - tick.at_ms > self.max_quote_age_ms:
            return _STALE, 'QUOTE_TOO_OLD'
        if last_at is not None and tick.at_ms < last_at:
            return _STALE, 'OUT_OF_ORDER_PROVIDER_TIMESTAMP'
        if tick.at_ms == last_at:
            prices = (tick.bid, tick.ask, tick.last, tick.tick_size)
            old_prices = tuple(previous[field] for field in ('bid', 'ask', 'last', 'tick_size'))
            if prices == old_prices:
                return 'DUPLICATE', 'IDENTICAL_PROVIDER_TIMESTAMP_AND_PRICES'
            return _MISSING, 'CONFLICTING_PROVIDER_TIMESTAMP'
        return None, None

    def _advance_candles(self, tick, observed_ms, discontinuity):
        db = self._db
        closed = []
        for minutes in TIMEFRAMES:
            duration = minutes * 60_000
            start_ms = tick.at_ms // duration * duration
            old = db.execute('SELECT * FROM candles WHERE symbol=? AND minutes=? AND available_ms IS NULL',
                             (tick.symbol, minutes)).fetchone()
            if old and old['start_ms'] == start_ms:
                db.execute('UPDATE candles SET high=max(high,?),low=min(low,?),close=?,sample_count=sample_count+1,'
                           'complete=complete AND ? WHERE symbol=? AND minutes=? AND start_ms=?',
                           (tick.bid, tick.bid, tick.bid, not discontinuity, tick.symbol, minutes, start_ms))
                continue
            contiguous = old is not None and old['end_ms'] == start_ms and not discontinuity
            if old is not None:
                status = _CLOSED if contiguous and old['complete'] else _MISSING
                db.execute('UPDATE candles SET status=?,available_ms=? WHERE symbol=? AND minutes=? AND start_ms=?',
                           (status, observed_ms, tick.symbol, minutes, old['start_ms']))
                if status == _CLOSED:
                    closed.append(minutes)
            # A left edge with a missing feed history is incomplete even if a tick lands exactly on it.
            db.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL)',
                       (tick.symbol, minutes, start_ms, start_ms + duration,
                        tick.bid, tick.bid, tick.bid, tick.bid, 1, _LIVE, contiguous))
        return tuple(closed)

    def closed_bars(self, symbol, minutes, *, cutoff_ms, limit=512):
        """Return only the newest available continuous run of confirmed sampled candles."""
        milliseconds(cutoff_ms)
        self._state(symbol)
        if type(minutes) is not int or minutes not in TIMEFRAMES or type(limit) is not int or not 1 <= limit <= _MAX_LOOKBACK:
            raise ValueError('INVALID_MARKET_CANDLE_QUERY')
        rows = self._db.execute(
            'SELECT * FROM candles WHERE symbol=? AND minutes=? AND start_ms<? '
            'ORDER BY start_ms DESC LIMIT ?',
            (symbol, minutes, cutoff_ms // (minutes * 60_000) * (minutes * 60_000), limit),
        )
        result = []
        expected_end = cutoff_ms // (minutes * 60_000) * (minutes * 60_000)
        for row in rows:
            if (row['end_ms'] != expected_end or row['status'] != _CLOSED
                    or row['available_ms'] is None or row['available_ms'] > cutoff_ms):
                break
            result.append(_bar(row))
            expected_end = row['start_ms']
        return list(reversed(result))

    def feature_bars(self, symbol, minutes, *, cutoff_ms, limit=512):
        """Use earlier complete candles across gaps only after a new causal close."""
        milliseconds(cutoff_ms)
        state = self._state(symbol)
        if type(minutes) is not int or minutes not in TIMEFRAMES or type(limit) is not int or not 1 <= limit <= _MAX_LOOKBACK:
            raise ValueError('INVALID_MARKET_CANDLE_QUERY')
        if (state['latched'] or state['at_ms'] is None or state['observed_ms'] > cutoff_ms
                or cutoff_ms - state['at_ms'] > self.max_quote_age_ms):
            return []
        duration = minutes * 60_000
        current_boundary = cutoff_ms // duration * duration
        rows = self._db.execute(
            'SELECT * FROM candles WHERE symbol=? AND minutes=? AND start_ms<? '
            'AND status=? AND available_ms<=? ORDER BY start_ms DESC LIMIT ?',
            (symbol, minutes, current_boundary, _CLOSED, cutoff_ms, limit),
        ).fetchall()
        if (not rows or rows[0]['end_ms'] != current_boundary
                or rows[0]['available_ms'] <= state['last_discontinuity_ms']):
            return []
        return [_bar(row) for row in reversed(rows)]

    def health(self, *, observed_ms):
        milliseconds(observed_ms)
        symbols = {}
        for symbol in self.symbols:
            row = self._state(symbol)
            age = None if row['at_ms'] is None else observed_ms - row['at_ms']
            status = row['status']
            reason = row['reason']
            if age is not None and not 0 <= age <= self.max_quote_age_ms:
                status, reason = _STALE, 'NO_CURRENT_FRESH_QUOTE'
            symbols[symbol] = {'status': status, 'reason': reason, 'last_successful_update_ms': row['observed_ms'],
                               'quote_age_ms': age, 'discontinuity_latched': bool(row['latched']),
                               'last_discontinuity_ms': row['last_discontinuity_ms'],
                               'session_open_at_last_update': None if row['session_open'] is None
                               else bool(row['session_open']),
                               'candles': self._candle_health(symbol, observed_ms, status)}
            if status == 'ACCEPTED':
                symbols[symbol]['last_fresh_quote'] = {
                    'bid': row['bid'], 'ask': row['ask'], 'last': row['last'], 'at_ms': row['at_ms'],
                    'spread_price_units': row['ask'] - row['bid'], 'tick_size': row['tick_size'],
                }
        return {'source': self.source, 'server': self.server, 'symbols': symbols,
                'events': dict(self._db.execute('SELECT status,n FROM counts'))}

    def _candle_health(self, symbol, observed_ms, quote_status):
        result = {}
        for minutes in TIMEFRAMES:
            duration = minutes * 60_000
            boundary = observed_ms // duration * duration
            row = self._db.execute('SELECT status,complete,sample_count FROM candles '
                                   'WHERE symbol=? AND minutes=? AND start_ms=? AND available_ms IS NULL',
                                   (symbol, minutes, boundary)).fetchone()
            latest_closed = self._db.execute(
                'SELECT end_ms FROM candles WHERE symbol=? AND minutes=? AND status=? AND available_ms<=? '
                'ORDER BY start_ms DESC LIMIT 1', (symbol, minutes, _CLOSED, observed_ms),
            ).fetchone()
            status = quote_status if quote_status in (_STALE, _MISSING) else _LIVE if row else _MISSING
            result[str(minutes)] = {'status': status, 'utc_start_ms': boundary,
                                    'utc_end_ms': boundary + duration,
                                    'samples': 0 if row is None else row['sample_count'],
                                    'continuity_candidate': status == _LIVE and bool(row['complete']),
                                    'latest_confirmed_close_ms': None if latest_closed is None else latest_closed[0]}
        return result

    def quote_at(self, symbol, receipt_ms):
        """Point-in-time executable sides, from live observations known at receipt."""
        milliseconds(receipt_ms)
        self._state(symbol)
        row = self._db.execute(
            'SELECT a.status,a.broker_at_ms,a.bid,a.ask,a.session_open,a.discontinuity FROM audit AS a '
            'JOIN state AS s ON s.symbol=a.symbol WHERE a.symbol=? AND s.latched=0 AND a.observed_ms<=? '
            "AND a.observed_ms>=? AND a.status!='DUPLICATE' ORDER BY a.observed_ms DESC,a.sequence DESC LIMIT 1",
            (symbol, receipt_ms, receipt_ms - self.max_quote_age_ms),
        ).fetchone()
        if (row is None or row['status'] != 'ACCEPTED' or row['session_open'] == 0 or row['discontinuity']
                or not 0 <= receipt_ms - row['broker_at_ms'] <= self.max_quote_age_ms):
            return None
        return Quote(symbol, row['bid'], row['ask'], row['broker_at_ms'], self.server)


def _bar(row):
    return {'time': row['start_ms'], 'time_close': row['end_ms'],
            **{name: row[name] for name in ('open', 'high', 'low', 'close')}}


def poll_readonly(connect_factory, store, *, clock=None, sleeper=sleep, cadence_s=1.0,
                  retry_initial_s=1.0, retry_cap_s=30.0, session_calendar=None, max_polls=None):
    """Pull updates inline. There is no producer backlog or dropped queue."""
    from .mt5 import MT5Unavailable

    clock = _now_ms if clock is None else clock
    delays = (cadence_s, retry_initial_s, retry_cap_s)
    if (not all(type(x) in (int, float) and isfinite(x) and 0 < x <= 300 for x in delays)
            or retry_initial_s > retry_cap_s
            or max_polls is not None and (type(max_polls) is not int or max_polls <= 0)):
        raise ValueError('INVALID_MARKET_POLL_POLICY')
    retry = retry_initial_s
    attempts = 0
    reconnect_codes = {'MT5_DISCONNECTED', 'MT5_ACCOUNT_CHANGED', 'MT5_ACCOUNT_READ_FAILED',
                       'MT5_CONTEXT_CLOSED', 'MT5_TERMINAL_CHANGED'}
    while max_polls is None or attempts < max_polls:
        attempted_poll = False
        try:
            with connect_factory() as reader:
                while max_polls is None or attempts < max_polls:
                    attempts += 1
                    attempted_poll = True
                    for symbol in store.symbols:
                        try:
                            contract = reader.symbol(symbol)
                            tick = reader.market_tick(symbol, contract=contract)
                        except MT5Unavailable as exc:
                            if str(exc) in reconnect_codes:
                                raise
                            yield store.mark_unavailable(symbol, observed_ms=clock(), reason=str(exc))
                            continue
                        observed_ms = clock()
                        session = None if session_calendar is None else session_calendar.status(
                            tick.server, tick.symbol, tick.at_ms, observed_ms)
                        tick = MarketTick(tick.symbol, tick.server, tick.bid, tick.ask, tick.last,
                                          tick.at_ms, tick.tick_size, session)
                        update = store.ingest(tick, observed_ms=observed_ms, contract=contract)
                        if update.accepted:
                            retry = retry_initial_s
                        yield update
                    if max_polls is None or attempts < max_polls:
                        sleeper(cadence_s)
        except MT5Unavailable as exc:
            # Count connect failures as bounded poll attempts as well.
            if not attempted_poll:
                attempts += 1
            now = clock()
            for symbol in store.symbols:
                yield store.mark_unavailable(symbol, observed_ms=now, reason=str(exc))
            if max_polls is None or attempts < max_polls:
                sleeper(retry)
                retry = min(retry * 2, retry_cap_s)
