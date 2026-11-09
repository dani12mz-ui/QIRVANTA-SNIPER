"""Immutable broker-native history, separate from the forward quote store."""
from hashlib import sha256
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .common import canonical, finite, milliseconds
from .market import TIMEFRAMES, _now_ms, stream_name

_FIELDS = {'at_ms', 'open', 'high', 'low', 'close', 'tick_volume', 'reported_real_volume',
           'reported_spread_points'}
_MAX_ROWS = 100_000
_MAX_BYTES = 48 * 1024 * 1024


def _window(minutes, start_ms, end_ms, retrieved_ms):
    if type(minutes) is not int or minutes not in TIMEFRAMES:
        raise ValueError('INVALID_BROKER_CACHE_TIMEFRAME')
    for value in (start_ms, end_ms, retrieved_ms):
        milliseconds(value)
    duration = minutes * 60_000
    if not 0 < end_ms - start_ms <= min(366 * 86_400_000, _MAX_ROWS * duration) or end_ms > retrieved_ms:
        raise ValueError('INVALID_BROKER_CACHE_WINDOW')
    return duration


def _validate_rows(rows, duration, start_ms, end_ms):
    if len(rows) > _MAX_ROWS:
        raise ValueError('BROKER_CACHE_ROW_LIMIT_EXCEEDED')
    previous_close = start_ms
    for row in rows:
        if not isinstance(row, dict) or set(row) != _FIELDS:
            raise ValueError('INVALID_BROKER_CACHE_ROW')
        at_ms = milliseconds(row['at_ms'])
        for key in ('open', 'high', 'low', 'close'):
            finite(row[key], positive=True)
        for key in ('tick_volume', 'reported_real_volume', 'reported_spread_points'):
            if type(row[key]) is not int or row[key] < 0:
                raise ValueError('INVALID_BROKER_CACHE_VOLUME_OR_SPREAD')
        if (not previous_close <= at_ms < at_ms + duration <= end_ms
                or not row['low'] <= min(row['open'], row['close']) <= max(row['open'], row['close']) <= row['high']):
            raise ValueError('INVALID_BROKER_CACHE_BAR')
        previous_close = at_ms + duration


def _gaps(rows, duration, start_ms, end_ms, session_calendar, server, symbol):
    left = start_ms
    for right in (row['at_ms'] for row in rows):
        if right > left:
            yield _gap(left, right, session_calendar, server, symbol)
        left = right + duration
    if end_ms > left:
        yield _gap(left, end_ms, session_calendar, server, symbol)


def _gap(left, right, calendar, server, symbol):
    closed = (calendar is not None and calendar.server == server and symbol in calendar.windows
              and calendar.coverage[0] <= left < right <= calendar.coverage[1]
              and not any(start < right and end > left for start, end in calendar.windows[symbol]))
    return {'start_ms': left, 'end_ms': right,
            'status': 'VERIFIED_SESSION_GAP' if closed else 'UNCLASSIFIED_COVERAGE_GAP'}


def _bytes(path):
    with Path(path).open('rb') as handle:
        contents = handle.read(_MAX_BYTES + 1)
    if len(contents) > _MAX_BYTES:
        raise ValueError('BROKER_CACHE_FILE_TOO_LARGE')
    return contents


def _read(manifest_path):
    manifest_path = Path(manifest_path)
    if manifest_path.name != 'manifest.json':
        raise ValueError('INVALID_BROKER_CACHE_MANIFEST_PATH')
    encoded = _bytes(manifest_path)
    if manifest_path.parent.name != sha256(encoded).hexdigest():
        raise ValueError('BROKER_CACHE_MANIFEST_HASH_MISMATCH')
    manifest = json.loads(encoded)
    if not isinstance(manifest, dict) or manifest.get('schema') != 1:
        raise ValueError('INVALID_BROKER_CACHE_MANIFEST')
    data = _bytes(manifest_path.with_name('bars.json'))
    if sha256(data).hexdigest() != manifest.get('dataset_sha256'):
        raise ValueError('BROKER_CACHE_DATA_HASH_MISMATCH')
    rows = json.loads(data)
    if not isinstance(rows, list) or len(rows) != manifest.get('rows'):
        raise ValueError('INVALID_BROKER_CACHE_ROW_COUNT')
    duration = _window(manifest['minutes'], manifest['requested_start_ms'], manifest['requested_end_ms'],
                       manifest['retrieved_ms'])
    _validate_rows(rows, duration, manifest['requested_start_ms'], manifest['requested_end_ms'])
    return manifest, rows


def archive_bars(reader, root, exact_name, minutes, start_ms, end_ms, *, source, retrieved_ms=_now_ms,
                 prior_manifest=None, session_calendar=None):
    """Capture immutable native MT5 bars and produce a content-addressed manifest."""
    duration = _window(minutes, start_ms, end_ms, end_ms)
    stream_name(source)
    stream_name(exact_name)
    spec = reader.symbol(exact_name)
    server = stream_name(spec.server)
    if spec.symbol != exact_name:
        raise ValueError('BROKER_CACHE_SYMBOL_MISMATCH')
    rows = reader.bars(exact_name, minutes, start_ms, end_ms)
    retrieved_ms = retrieved_ms() if callable(retrieved_ms) else retrieved_ms
    _window(minutes, start_ms, end_ms, retrieved_ms)
    _validate_rows(rows, duration, start_ms, end_ms)
    data = canonical(rows).encode('utf-8')
    dataset_hash = sha256(data).hexdigest()
    scope = (source, server, exact_name, minutes, start_ms, end_ms)
    predecessor = None
    revised = False
    if prior_manifest is not None:
        before, _ = _read(prior_manifest)
        scope_keys = ('source', 'server', 'symbol', 'minutes', 'requested_start_ms', 'requested_end_ms')
        if tuple(before[key] for key in scope_keys) != scope or before['retrieved_ms'] >= retrieved_ms:
            raise ValueError('BROKER_CACHE_REVISION_SCOPE_OR_TIME_MISMATCH')
        predecessor = Path(prior_manifest).parent.name
        revised = before['dataset_sha256'] != dataset_hash
    manifest = {
        'schema': 1, 'status': 'REVISED_HISTORY' if revised else 'BACKFILLED',
        'observation_class': 'BROKER_NATIVE_HISTORY_RETRIEVED_AFTER_THE_FACT',
        'source': source, 'server': server, 'symbol': exact_name, 'minutes': minutes,
        'timezone': 'UTC', 'retrieved_ms': retrieved_ms,
        'requested_start_ms': start_ms, 'requested_end_ms': end_ms, 'rows': len(rows),
        'first_bar_open_ms': rows[0]['at_ms'] if rows else None,
        'last_bar_open_ms': rows[-1]['at_ms'] if rows else None,
        'coverage_gaps': list(_gaps(rows, duration, start_ms, end_ms, session_calendar, server, exact_name)),
        'nominal_duration_ms': duration,
        'native_boundary_policy': 'PROVIDER_OPEN_PLUS_NOMINAL_DURATION_FOR_COVERAGE_ONLY',
        'session_evidence': session_calendar.evidence if session_calendar is not None else None,
        'volume_type': 'BROKER_REPORTED_TICK_AND_REAL_VOLUME_UNVERIFIED',
        'native_chart_mode_code': spec.chart_mode, 'dataset_sha256': dataset_hash,
        'previous_manifest_sha256': predecessor,
    }
    encoded = canonical(manifest).encode('utf-8')
    if max(len(data), len(encoded)) > _MAX_BYTES:
        raise ValueError('BROKER_CACHE_FILE_TOO_LARGE')
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    target = root / sha256(encoded).hexdigest()
    manifest_path = target / 'manifest.json'
    if not target.exists():
        with TemporaryDirectory(dir=root, prefix='.capture-') as scratch:
            directory = Path(scratch) / 'complete'
            directory.mkdir()
            (directory / 'bars.json').write_bytes(data)
            (directory / 'manifest.json').write_bytes(encoded)
            try:
                directory.rename(target)
            except OSError:
                if not target.is_dir():
                    raise
    _read(manifest_path)
    return manifest_path


def cached_bars(manifest_path, *, source, server, symbol, minutes, cutoff_ms, bid_only=False):
    """Read validated warmup candles known only after the recorded retrieval."""
    milliseconds(cutoff_ms)
    manifest, rows = _read(manifest_path)
    if (source, server, symbol, minutes) != tuple(manifest[key] for key in ('source', 'server', 'symbol', 'minutes')):
        raise ValueError('BROKER_CACHE_LOOKUP_IDENTITY_MISMATCH')
    if bid_only and (type(manifest.get('native_chart_mode_code')) is not int or manifest['native_chart_mode_code'] != 0):
        raise ValueError('BROKER_CACHE_NATIVE_BID_CHART_REQUIRED')
    if manifest['retrieved_ms'] > cutoff_ms:
        return []
    duration = minutes * 60_000
    # Native UTC opens are retained unchanged. Restrict this adapter to the
    # research entry horizons where fixed close intervals are expected.
    if minutes not in (5, 15, 30) or any(row['at_ms'] % duration for row in rows):
        raise ValueError('BROKER_CACHE_ENTRY_BOUNDARIES_UNVERIFIED')
    return [{'time': row['at_ms'], 'time_close': row['at_ms'] + duration,
             **{key: row[key] for key in ('open', 'high', 'low', 'close')}} for row in rows]
