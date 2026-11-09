"""Restricted MetaTrader 5 reader. Mutating terminal and broker APIs are absent."""
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from importlib import import_module
from importlib.util import find_spec
import json
import os
from pathlib import Path
import subprocess
from threading import Lock
from time import time_ns

from qirvanta_ultimate.risk import BrokerSpec, Quote, grid_aligned, millis, number


class Mode(str, Enum):
    RESEARCH = 'RESEARCH'
    PAPER = 'PAPER'
    DEMO_READ_ONLY = 'DEMO_READ_ONLY'
    DEMO_EXECUTION = 'DEMO_EXECUTION'
    LIVE_READ_ONLY = 'LIVE_READ_ONLY'
    LIVE_EXECUTION = 'LIVE_EXECUTION'


class MT5Unavailable(RuntimeError):
    """A sanitized error code that may be logged without vendor message details."""


def safe_mode(value):
    try:
        mode = Mode(value)
    except (ValueError, TypeError):
        raise MT5Unavailable('UNKNOWN_EXECUTION_MODE') from None
    if mode in (Mode.DEMO_EXECUTION, Mode.LIVE_EXECUTION):
        raise MT5Unavailable('BROKER_EXECUTION_HARD_DISABLED')
    return mode


def _running_terminal_paths():
    """Use process paths only, omitting command lines and account files."""
    if os.name != 'nt':
        return ()
    script = ('@(Get-Process -Name terminal64,terminal,metatrader64,metatrader '
              '-ErrorAction SilentlyContinue | Where-Object Path | '
              'Select-Object -ExpandProperty Path) | ConvertTo-Json -Compress')
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, timeout=5, check=False,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if not result.stdout.strip():
            return ()
        value = json.loads(result.stdout.decode('utf-8-sig', errors='replace'))
        return tuple(value if isinstance(value, list) else [value])
    except (OSError, subprocess.SubprocessError, ValueError):
        return ()


def environment_status():
    """Inspect prerequisites only. Does not import the SDK or launch a terminal."""
    try:
        sdk = find_spec('MetaTrader5') is not None
    except (ImportError, ValueError):
        sdk = False
    return {'windows': os.name == 'nt', 'sdk_discoverable': sdk,
            'running_terminal_path_count': len(_running_terminal_paths()),
            'attachment_attempted': False, 'broker_orders_enabled': False}


def _now_ms():
    return time_ns() // 1_000_000


@dataclass(frozen=True)
class AccountSnapshot:
    kind: str
    server: str
    company: str
    currency: str
    leverage: int
    margin_mode: int
    equity: float = field(repr=False)
    free_margin: float = field(repr=False)
    observed_at_ms: int


@dataclass(frozen=True)
class SymbolSnapshot:
    symbol: str
    server: str
    account_currency: str
    observed_at_ms: int
    visible: bool
    selected: bool
    trade_mode: int
    filling_mode: int
    order_mode: int
    trade_execution: int
    calculation_mode: int
    chart_mode: int
    contract_size: float
    point: float
    tick_size: float
    tick_value_loss: float
    tick_value_profit: float
    volume_min: float
    volume_max: float
    volume_step: float
    volume_limit: float
    stops_level_points: int
    freeze_level_points: int
    currency_base: str
    currency_profit: str
    currency_margin: str
    session_open: None = field(default=None, init=False)

    def market_filling_options(self):
        if type(self.trade_execution) is not int or type(self.filling_mode) is not int or self.filling_mode < 0:
            return ()
        if self.trade_execution in (0, 1):
            return ('FOK', 'IOC', 'RETURN')
        if self.trade_execution not in (2, 3):
            return ()
        options = tuple(name for flag, name in ((1, 'FOK'), (2, 'IOC')) if self.filling_mode & flag)
        return options if self.trade_execution == 2 else options + ('RETURN',)

    def supports_market_bracket(self, side):
        # MQL5 trade mode enum and SYMBOL_ORDER_MARKET/SL/TP flags.
        mode_ok = (type(side) is int and side in (-1, 1) and type(self.trade_mode) is int and
                   self.trade_mode in (4, 1 if side == 1 else 2))
        flags = 1 | 16 | 32
        return (self.visible and self.selected and mode_ok and type(self.order_mode) is int
                and self.order_mode >= 0 and self.order_mode & flags == flags
                and bool(self.market_filling_options()))


@dataclass(frozen=True)
class ExposureSnapshot:
    symbol: str
    observed_at_ms: int
    position_count: int
    pending_count: int
    long_volume: float
    short_volume: float


@dataclass(frozen=True)
class ReviewedTerms:
    """Locally reviewed, account-bound assumptions outside the SDK quote snapshot."""
    symbol: str
    server: str
    account_currency: str
    calculation_mode: int
    reviewed_at_ms: int
    commission_round_trip_per_lot: float
    linear_cfd_and_currency_verified: bool
    evidence: str


def linear_cfd_spec(symbol: SymbolSnapshot, terms: ReviewedTerms):
    """No conversion from vendor metadata to verified sizing without reviewed terms."""
    identity = (symbol.symbol, symbol.server, symbol.account_currency, symbol.calculation_mode)
    if identity != (terms.symbol, terms.server, terms.account_currency, terms.calculation_mode):
        raise MT5Unavailable('REVIEWED_TERMS_IDENTITY_MISMATCH')
    if (terms.linear_cfd_and_currency_verified is not True or not terms.evidence
            or not millis(terms.reviewed_at_ms) or not millis(symbol.observed_at_ms)
            or not number(terms.commission_round_trip_per_lot) or terms.commission_round_trip_per_lot < 0):
        raise MT5Unavailable('COSTS_OR_CURRENCY_CONVERSION_UNVERIFIED')
    positive = (symbol.contract_size, symbol.point, symbol.tick_size, symbol.tick_value_loss,
                symbol.tick_value_profit, symbol.volume_min, symbol.volume_max, symbol.volume_step)
    if (not all(number(x) and x > 0 for x in positive) or symbol.volume_max < symbol.volume_min
            or not millis(symbol.stops_level_points) or not millis(symbol.freeze_level_points)):
        raise MT5Unavailable('INCOMPLETE_SYMBOL_SPECIFICATION')
    return BrokerSpec(symbol.symbol, symbol.server, symbol.account_currency, True, terms.evidence,
                      min(symbol.observed_at_ms, terms.reviewed_at_ms), symbol.contract_size,
                      symbol.point, symbol.tick_size, symbol.tick_value_loss, symbol.volume_min,
                      symbol.volume_max, symbol.volume_step, symbol.stops_level_points,
                      symbol.freeze_level_points, terms.commission_round_trip_per_lot, True), symbol.tick_value_profit


class MT5Reader:
    """Use only through a checked, single-terminal read-only context."""
    def __init__(self, api, account, clock):
        self._api = api
        self._identity = self._account_identity(account)
        self._clock = clock
        self._closed = False

    @staticmethod
    def _account_identity(account):
        return (account.login, account.server, account.company, account.trade_mode, account.currency)

    def _account(self):
        if self._closed:
            raise MT5Unavailable('MT5_CONTEXT_CLOSED')
        try:
            terminal = self._api.terminal_info()
            account = self._api.account_info()
            if terminal is None or terminal.connected is not True or account is None:
                raise MT5Unavailable('MT5_DISCONNECTED')
            if self._account_identity(account) != self._identity:
                raise MT5Unavailable('MT5_ACCOUNT_CHANGED')
            return account
        except MT5Unavailable:
            raise
        except Exception:
            raise MT5Unavailable('MT5_ACCOUNT_READ_FAILED') from None

    def _read(self, function, *args, **kwargs):
        self._account()
        try:
            value = function(*args, **kwargs)
        except Exception:
            raise MT5Unavailable('MT5_DATA_READ_FAILED') from None
        self._account()
        if value is None:
            raise MT5Unavailable('MT5_DATA_UNAVAILABLE')
        return value

    def account(self):
        a = self._account()
        try:
            if (not number(a.equity) or not number(a.margin_free)
                    or type(a.leverage) is not int or type(a.margin_mode) is not int):
                raise MT5Unavailable('INVALID_MT5_ACCOUNT_SNAPSHOT')
            return AccountSnapshot('DEMO' if a.trade_mode == 0 else 'LIVE', a.server, a.company,
                                   a.currency, a.leverage, a.margin_mode, a.equity, a.margin_free,
                                   self._clock())
        except AttributeError:
            raise MT5Unavailable('INCOMPLETE_MT5_ACCOUNT_SNAPSHOT') from None

    def discover_symbols(self, text_filter=''):
        """Exact names discovered from current terminal. No automatic symbol selection."""
        rows = self._read(self._api.symbols_get)
        return tuple(sorted({r.name for r in rows if text_filter.casefold() in r.name.casefold()}))

    def symbol(self, exact_name):
        if not isinstance(exact_name, str) or not exact_name or exact_name.strip() != exact_name:
            raise MT5Unavailable('EXACT_SYMBOL_REQUIRED')
        r = self._read(self._api.symbol_info, exact_name)
        try:
            if (r.name != exact_name or getattr(r, 'custom', False) is not False
                    or type(r.visible) is not bool or type(r.select) is not bool):
                raise MT5Unavailable('SYMBOL_NOT_BROKER_EXACT_MATCH')
            return SymbolSnapshot(r.name, self._identity[1], self._identity[4], self._clock(),
                                  r.visible, r.select, r.trade_mode, r.filling_mode,
                                  r.order_mode, r.trade_exemode, r.trade_calc_mode, r.chart_mode,
                                  r.trade_contract_size, r.point, r.trade_tick_size,
                                  r.trade_tick_value_loss, r.trade_tick_value_profit,
                                  r.volume_min, r.volume_max, r.volume_step, r.volume_limit,
                                  r.trade_stops_level, r.trade_freeze_level, r.currency_base,
                                  r.currency_profit, r.currency_margin)
        except AttributeError:
            raise MT5Unavailable('INCOMPLETE_MT5_SYMBOL_SNAPSHOT') from None

    def quote(self, exact_name, *, max_age_ms=5000):
        s = self.symbol(exact_name)
        if not s.selected or not s.visible:
            raise MT5Unavailable('SYMBOL_NOT_SELECTED_IN_TERMINAL')
        r = self._read(self._api.symbol_info_tick, exact_name)
        if (not millis(max_age_ms) or not millis(getattr(r, 'time_msc', None))
                or not 0 <= self._clock() - r.time_msc <= max_age_ms):
            raise MT5Unavailable('STALE_OR_FUTURE_BID_ASK')
        if not number(r.bid) or not number(r.ask) or not 0 < r.bid <= r.ask:
            raise MT5Unavailable('INVALID_BID_ASK')
        return Quote(s.symbol, r.bid, r.ask, r.time_msc, s.server)

    def bars(self, exact_name, minutes, start_ms, end_ms):
        """Return a UTC half-open interval of completed native bars. Gaps remain gaps."""
        timeframes = {5: 'TIMEFRAME_M5', 15: 'TIMEFRAME_M15', 30: 'TIMEFRAME_M30',
                      60: 'TIMEFRAME_H1', 240: 'TIMEFRAME_H4'}
        if type(minutes) is not int or minutes not in timeframes:
            raise MT5Unavailable('UNSUPPORTED_MT5_TIMEFRAME')
        if (not millis(start_ms) or not millis(end_ms) or not start_ms < end_ms <= self._clock()
                or end_ms-start_ms > 366*86_400_000):
            raise MT5Unavailable('INVALID_MT5_HISTORY_WINDOW')
        s = self.symbol(exact_name)
        if not s.selected or not s.visible:
            raise MT5Unavailable('SYMBOL_NOT_SELECTED_IN_TERMINAL')
        try:
            start = datetime.fromtimestamp(start_ms/1000, timezone.utc)
            end = datetime.fromtimestamp(end_ms/1000, timezone.utc)
            raw = self._read(self._api.copy_rates_range, exact_name,
                             getattr(self._api, timeframes[minutes]), start, end)
            result = []
            for row in raw:
                at = int(row['time'])*1000
                if not start_ms <= at or at + minutes*60_000 > end_ms:
                    continue
                o, h, lo, c = (float(row[key]) for key in ('open', 'high', 'low', 'close'))
                if (not all(number(x) and x > 0 for x in (o, h, lo, c)) or lo > min(o, c)
                        or h < max(o, c) or result and at <= result[-1]['at_ms']):
                    raise MT5Unavailable('INVALID_MT5_BAR_SEQUENCE')
                result.append({'at_ms': at, 'open': o, 'high': h, 'low': lo, 'close': c,
                               'tick_volume': int(row['tick_volume']),
                               'reported_real_volume': int(row['real_volume']),
                               'reported_spread_points': int(row['spread'])})
            return tuple(result)
        except MT5Unavailable:
            raise
        except (AttributeError, KeyError, IndexError, OSError, OverflowError, TypeError, ValueError):
            raise MT5Unavailable('INVALID_MT5_HISTORY_RESPONSE') from None

    def exposure(self, exact_name):
        """Return symbol volumes and counts without order, position or client identifiers."""
        s = self.symbol(exact_name)
        positions = self._read(self._api.positions_get, symbol=s.symbol)
        pending = self._read(self._api.orders_get, symbol=s.symbol)
        volumes = [0., 0.]
        try:
            for row in positions:
                if (row.symbol != s.symbol or row.type not in (0, 1)
                        or not number(row.volume) or row.volume <= 0):
                    raise MT5Unavailable('INVALID_OPEN_POSITION_METADATA')
                volumes[row.type] += row.volume
            for row in pending:
                if (row.symbol != s.symbol or row.type not in (0, 1, 2, 3, 4, 5, 6, 7)
                        or not number(row.volume_current) or row.volume_current < 0):
                    raise MT5Unavailable('UNSUPPORTED_PENDING_ORDER_METADATA')
                volumes[row.type % 2] += row.volume_current
        except (AttributeError, TypeError):
            raise MT5Unavailable('INCOMPLETE_EXPOSURE_METADATA') from None
        return ExposureSnapshot(s.symbol, self._clock(), len(positions), len(pending), *volumes)

    def margin(self, side, quote, lots, *, max_age_ms=5000):
        """Read-only vendor estimate, excluding account-wide reservation accounting."""
        if (type(side) is not int or side not in (-1, 1) or not number(lots) or lots <= 0
                or quote.server != self._identity[1] or not millis(max_age_ms) or not millis(quote.at_ms)
                or not 0 <= self._clock() - quote.at_ms <= max_age_ms
                or not number(quote.bid) or not number(quote.ask) or not 0 < quote.bid <= quote.ask):
            raise MT5Unavailable('INVALID_MT5_MARGIN_REQUEST')
        s = self.symbol(quote.symbol)
        if not s.supports_market_bracket(side):
            raise MT5Unavailable('SYMBOL_CAPABILITY_MISMATCH')
        if (not grid_aligned(lots, s.volume_step) or not number(s.volume_min)
                or not number(s.volume_max) or not s.volume_min <= lots <= s.volume_max):
            raise MT5Unavailable('VOLUME_OFF_GRID_OR_OUTSIDE_BROKER_RANGE')
        order_type = self._api.ORDER_TYPE_BUY if side == 1 else self._api.ORDER_TYPE_SELL
        value = self._read(self._api.order_calc_margin, order_type, quote.symbol, lots,
                           quote.ask if side == 1 else quote.bid)
        if not number(value) or value <= 0:
            raise MT5Unavailable('INVALID_MT5_MARGIN_ESTIMATE')
        return value


_connection = Lock()


@contextmanager
def connect_readonly(terminal_path, *, expected_server, expected_company, mode,
                     timeout_ms=3000, _sdk=None, _running_paths=None, _clock=_now_ms):
    """Attach only to an explicitly selected, already-running terminal path.

    Vendor initialize may start the process if it exits during the attach race.
    No login parameters or terminal/watchlist modifications are passed.
    """
    selected = safe_mode(mode)
    if selected not in (Mode.DEMO_READ_ONLY, Mode.LIVE_READ_ONLY):
        raise MT5Unavailable('EXPLICIT_READ_ONLY_MODE_REQUIRED')
    if (not isinstance(expected_server, str) or not expected_server
            or not isinstance(expected_company, str) or not expected_company):
        raise MT5Unavailable('EXPECTED_BROKER_IDENTITY_REQUIRED')
    if not millis(timeout_ms) or not 1 <= timeout_ms <= 30_000:
        raise MT5Unavailable('INVALID_MT5_ATTACH_TIMEOUT')
    if not isinstance(terminal_path, (str, Path)) or not Path(terminal_path).is_absolute():
        raise MT5Unavailable('ABSOLUTE_TERMINAL_PATH_REQUIRED')
    path = Path(terminal_path).resolve()
    running = _running_terminal_paths() if _running_paths is None else _running_paths
    if not path.is_file() or path not in {Path(p).resolve() for p in running}:
        raise MT5Unavailable('SELECTED_TERMINAL_NOT_RUNNING')
    try:
        api = import_module('MetaTrader5') if _sdk is None else _sdk
    except (ImportError, OSError):
        raise MT5Unavailable('METATRADER5_SDK_UNAVAILABLE') from None
    if not _connection.acquire(blocking=False):
        raise MT5Unavailable('MT5_CONTEXT_ALREADY_ACTIVE')
    reader = None
    initialized = False
    try:
        try:
            initialized = api.initialize(str(path), timeout=timeout_ms) is True
            if not initialized:
                raise MT5Unavailable('MT5_ATTACH_FAILED')
            terminal = api.terminal_info()
            account = api.account_info()
            if terminal is None or terminal.connected is not True or account is None:
                raise MT5Unavailable('MT5_DISCONNECTED_OR_NO_ACCOUNT')
            expected_kind = 0 if selected is Mode.DEMO_READ_ONLY else 2
            if (account.server != expected_server or account.company != expected_company
                    or account.trade_mode != expected_kind or not account.currency or not account.login):
                raise MT5Unavailable('MT5_ACCOUNT_IDENTITY_OR_MODE_MISMATCH')
            reader = MT5Reader(api, account, _clock)
        except MT5Unavailable:
            raise
        except Exception:
            raise MT5Unavailable('MT5_ATTACH_OR_IDENTITY_READ_FAILED') from None
        yield reader
    finally:
        if reader is not None:
            reader._closed = True
        try:
            if initialized:
                api.shutdown()
        except Exception:
            raise MT5Unavailable('MT5_IPC_SHUTDOWN_FAILED') from None
        finally:
            _connection.release()


def main(argv=None):
    """Local operator probe. Only inspect after explicitly selecting a reader mode."""
    import argparse
    from dataclasses import asdict

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('probe')
    inspect = commands.add_parser('inspect')
    inspect.add_argument('--mode', required=True, choices=('DEMO_READ_ONLY', 'LIVE_READ_ONLY'))
    selection = inspect.add_mutually_exclusive_group(required=True)
    selection.add_argument('--symbol')
    selection.add_argument('--contains')
    args = parser.parse_args(argv)
    if args.command == 'probe':
        print(json.dumps(environment_status(), sort_keys=True))
        return 0
    try:
        with connect_readonly(os.environ.get('QIRVANTA_MT5_TERMINAL_PATH'),
                              expected_server=os.environ.get('QIRVANTA_MT5_EXPECTED_SERVER'),
                              expected_company=os.environ.get('QIRVANTA_MT5_EXPECTED_COMPANY'),
                              mode=args.mode) as reader:
            account = reader.account()
            result = {'status': 'READ_ONLY_METADATA_OBSERVED', 'mode': args.mode,
                      'account_kind': account.kind, 'account_currency': account.currency,
                      'leverage': account.leverage, 'margin_mode': account.margin_mode,
                      'broker_orders_enabled': False}
            if args.symbol:
                spec = reader.symbol(args.symbol)
                result['symbol'] = asdict(spec)
                result['market_filling_options'] = spec.market_filling_options()
                try:
                    quote = reader.quote(args.symbol)
                    result['quote'] = asdict(quote)
                    result['spread_price_units'] = quote.ask-quote.bid
                except MT5Unavailable as error:
                    result['quote_status'] = str(error)
            else:
                result['matches'] = reader.discover_symbols(args.contains)
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except (MT5Unavailable, OSError, ValueError, TypeError) as error:
        reason = str(error) if isinstance(error, MT5Unavailable) else 'INVALID_LOCAL_MT5_INPUT_OR_RESPONSE'
        print(json.dumps({'status': 'BLOCKED', 'reason': reason, 'broker_orders_enabled': False}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
