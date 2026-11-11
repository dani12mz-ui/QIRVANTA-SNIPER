"""Compare native closed-bar Direct Trend Breakout logs with the Python research scan."""

import argparse
from collections import Counter
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import io
from itertools import zip_longest
import json
from math import isclose
from pathlib import Path

from gold_pro.direct_trend_breakout import scan
from gold_pro.rc1_qa import _json_dict, digest, json_number, require
from research.data.loader import validate_bars


TOLERANCE = 2e-8
MARKET = "DT_MARKET"
SHADOW = "DT_SHADOW"
END = "DT_END"
OHLC = ("open", "high", "low", "close")


@dataclass(frozen=True)
class NativeExport:
    path: Path
    timeframe: int
    first_index: int
    bars: tuple[dict, ...]
    events: tuple[dict, ...]
    end: dict | None
    sha256: str
    repeated_batches: tuple[dict, ...]


def read_native(path):
    path = Path(path)
    data = path.read_bytes()
    rows = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""), strict=True)
    require(rows.fieldnames == ["Date", "Message"], "expected TradingView Pine Logs Date,Message CSV")
    bars, events, repeated = [], [], []
    first_index = timeframe = end = None
    market = previous_market = None
    market_events, previous_events = [], []

    def finish_batch():
        nonlocal first_index, timeframe, market, previous_market, market_events, previous_events
        if market is None:
            return
        record, raw = market
        index, frame = record.get("index"), record.get("timeframe")
        if first_index is None:
            first_index, timeframe = index, frame
        require(record.get("symbol") == "OANDA:XAUUSD" and type(frame) is int
                and frame in (5, 15, 30) and frame == timeframe, f"{MARKET}: invalid chart context")
        require(type(index) is int and index >= 0, f"{MARKET}: invalid bar index")
        require(all(type(record.get(key)) is int for key in ("time", "time_close"))
                and record["time_close"] - record["time"] == timeframe * 60_000,
                f"{MARKET}: wrong opening or closing timestamp")
        ordered_payloads = [payload for _, payload in market_events]
        if bars and index == first_index + len(bars) - 1:
            require(raw == previous_market and ordered_payloads == previous_events,
                    f"{MARKET}: inconsistent repeated confirmed batch at index {index}")
            repeated.append({"index": index, "time_close": record["time_close"], "events": len(ordered_payloads)})
        else:
            require(index == first_index + len(bars), f"{MARKET}: missing or out-of-order bar index")
            bars.append({**{key: record[key] for key in ("time", "time_close")},
                         **{key: float(json_number(record.get(key), key)) for key in OHLC}})
            events.extend(item for item, _ in market_events)
            previous_market, previous_events = raw, ordered_payloads
        market, market_events = None, []

    for row in rows:
        require(set(row) == {"Date", "Message"} and row["Date"] and row["Message"] is not None,
                "invalid Date,Message CSV row")
        for line in row["Message"].splitlines():
            tag, sep, payload = line.partition("|")
            if tag not in (MARKET, SHADOW, END):
                continue
            require(sep and payload, f"{tag}: missing JSON")
            record = _json_dict(payload)
            if tag == MARKET:
                finish_batch()
                market = record, payload
            elif tag == END:
                finish_batch()
                require(bars, f"{tag}: no preceding market bar")
                index, closed = first_index + len(bars) - 1, bars[-1]["time_close"]
                require(end is None and record == {"index": index, "time_close": closed},
                        f"{tag}: invalid or repeated end marker")
                end = record
            else:
                require(market is not None, f"{tag}: no preceding market bar")
                index, closed = market[0].get("index"), market[0].get("time_close")
                require(type(index) is int and type(closed) is int
                        and record.get("bar") == index and record.get("time_ms") == closed,
                        f"{tag}: event not logged after its confirmed market bar")
                market_events.append((record, payload))
    finish_batch()
    require(bars, f"{path}: no {MARKET} records")
    validate_bars(bars)
    return NativeExport(path, timeframe, first_index, tuple(bars), tuple(events), end, digest(data), tuple(repeated))


def same_number(observed, expected):
    return type(observed) in (int, float, Decimal) and isclose(
        float(observed), expected, abs_tol=TOLERANCE, rel_tol=0)


def event_differences(actual, expected, offset=0):
    for index, (observed, python) in enumerate(zip_longest(actual, expected), 1):
        if observed is None or python is None:
            return {"event": index, "native": observed, "expected": python}
        if observed.keys() != python.keys():
            return {"event": index, "missing_fields": sorted(python.keys() - observed.keys()),
                    "unexpected_fields": sorted(observed.keys() - python.keys())}
        for key, value in python.items():
            native = observed[key]
            value = value + offset if key == "bar" else value
            if isinstance(value, float):
                same = same_number(native, value)
            elif type(value) is int:
                same = type(native) is int and native == value
            elif isinstance(value, tuple):
                same = isinstance(native, (list, tuple)) and len(native) == len(value) and all(
                    same_number(a, b) for a, b in zip(native, value)
                )
            else:
                same = native == value
            if not same:
                return {"event": index, "field": key, "native": native, "expected": value}
    return None


def prefix_consistency(bars, events, plans):
    prefixes = {1, 14, 20, 24, len(bars), *range(64, len(bars), 64),
                *(event["bar"] + 1 for event in events)}
    tested = 0
    for length in sorted(n for n in prefixes if n <= len(bars)):
        short_events, short_plans = scan(bars[:length])
        if (short_events != [event for event in events if event["bar"] < length]
                or short_plans != [plan for plan in plans if plan.bar < length]):
            return {"status": "MISMATCH", "prefix_bars": length}
        tested += 1
    return {"status": "MATCH", "tested_prefixes": tested}


def compare_repeat(original, repeat):
    n = min(len(original.bars), len(repeat.bars))
    if original.timeframe != repeat.timeframe or original.first_index != repeat.first_index or not n:
        return {"status": "NONIDENTICAL_CONTEXT"}
    if original.bars[:n] != repeat.bars[:n]:
        return {"status": "NONIDENTICAL_CONTEXT"}
    last = original.first_index + n - 1
    before = [row for row in original.events if row["bar"] <= last]
    after = [row for row in repeat.events if row["bar"] <= last]
    if before != after:
        return {"status": "MISMATCH", "common_bars": n, "original_events": len(before), "repeat_events": len(after)}
    return {"status": "MATCH", "common_bars": n, "events": len(before),
            "repeat_file": repeat.path.name, "repeat_sha256": repeat.sha256}


def utc(stamp):
    return datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def comparison(path, repeat_path=None):
    native = read_native(path)
    expected, plans = scan(native.bars)
    difference = event_differences(native.events, expected, native.first_index)
    prefix = prefix_consistency(native.bars, expected, plans)
    repeat = compare_repeat(native, read_native(repeat_path)) if repeat_path else {"status": "MISSING_EVIDENCE"}
    if difference or prefix["status"] == "MISMATCH" or repeat["status"] == "MISMATCH":
        status = "MISMATCH"
    elif native.first_index != 0 or not native.end:
        status = "INCOMPLETE_HISTORY"
    elif not expected:
        status = "NO_EVENT_EXAMPLES"
    else:
        status = "MATCH"
    return {
        "status": status, "file": native.path.name, "sha256": native.sha256, "timeframe": native.timeframe,
        "bars": len(native.bars), "first_index": native.first_index,
        "first_open_utc": utc(native.bars[0]["time"]), "last_close_utc": utc(native.bars[-1]["time_close"]),
        "historical_end_index": native.end["index"] if native.end else None,
        "repeated_identical_confirmed_batches": native.repeated_batches,
        "native_events": len(native.events), "python_events": len(expected), "diagnostic_candidates": len(plans),
        "native_states": dict(Counter(event["state"] for event in native.events)),
        "buy_states": dict(Counter(event["state"] for event in native.events if event["side"] == 1)),
        "sell_states": dict(Counter(event["state"] for event in native.events if event["side"] == -1)),
        "first_difference": difference, "python_prefix_consistency": prefix, "native_repeat": repeat,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("native", type=Path)
    parser.add_argument("--repeat", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = comparison(args.native, args.repeat)
    rendered = json.dumps(result, indent=2, default=str) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
    else:
        print(rendered, end="")
    return 0 if result["status"] == "MATCH" else 1


if __name__ == "__main__":
    raise SystemExit(main())
