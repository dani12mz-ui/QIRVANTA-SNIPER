"""Strict access to the already observed archive; no downloading or imputation."""
import csv
from decimal import Decimal, InvalidOperation
import hashlib
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA = ROOT / 'diagnostics/protocol/datasets/B-ALL-FULL/bars.csv'
DEFAULT_SHA256 = '7015f6386d2627237c57ff8625d59cbe3c9b852e4742b786cbb4679db3d48c56'


def validate_bars(bars):
    """Reject missing OHLC/timestamps; preserve time gaps without manufacturing bars."""
    previous = None
    interval = None
    for i, b in enumerate(bars):
        for k in ('time', 'time_close', 'open', 'high', 'low', 'close'):
            if k not in b or isinstance(b[k], bool) or not isinstance(b[k], (int, float)) or not math.isfinite(b[k]):
                raise ValueError(f'Invalid {k} at bar {i}')
        if int(b['time']) != b['time'] or int(b['time_close']) != b['time_close']:
            raise ValueError('Timestamps must be integer UTC milliseconds')
        duration = b['time_close'] - b['time']
        if duration not in (300000, 900000, 1800000):
            raise ValueError('Research entry bars must be M5, M15 or M30')
        interval = duration if interval is None else interval
        if duration != interval:
            raise ValueError('Mixed bar durations')
        if min(b['open'], b['high'], b['low'], b['close']) <= 0 or not (b['low'] <= min(b['open'], b['close']) <= max(b['open'], b['close']) <= b['high']):
            raise ValueError(f'Invalid OHLC envelope at bar {i}')
        if previous is not None and b['time'] < previous['time_close']:
            raise ValueError('Duplicate, overlapping or backwards bars')
        previous = b
    return interval


def load_bars(path=None):
    source = DEFAULT_DATA if path is None else Path(path)
    if source.resolve() == DEFAULT_DATA.resolve() and hashlib.sha256(source.read_bytes()).hexdigest() != DEFAULT_SHA256:
        raise ValueError('Observed archive hash changed')
    with source.open(encoding='utf-8', newline='') as f:
        bars = []
        for row in csv.DictReader(f):
            bar = {k: (None if v in ('', 'null', 'NaN') else float(v)) for k, v in row.items() if k not in ('time','time_close')}
            for k in ('time', 'time_close'):
                try:
                    value=Decimal(row[k])
                except (KeyError,InvalidOperation,TypeError) as exc:
                    raise ValueError(f'{k} must contain exact integer UTC milliseconds') from exc
                if not value.is_finite() or value!=value.to_integral_value():
                    raise ValueError(f'{k} must contain exact integer UTC milliseconds')
                bar[k]=int(value)
            bars.append(bar)
    validate_bars(bars)
    return bars
