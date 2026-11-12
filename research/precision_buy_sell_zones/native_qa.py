"""Compare native Precision Zones Pine journals with the independent causal replay."""

import argparse
from collections import Counter
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from io import StringIO
from itertools import zip_longest
import json
from pathlib import Path

from gold_pro.rc1_qa import _json_dict
from research.direct_trend_breakout.shadow_native import read_native as read_control
from research.direct_trend_visibility.native_qa import journal_rows
from .replay import INVALIDATED, PROTOCOL_SHA256, REJECTION, TICK, TOUCH, replay, read_protocol


OHLC = ("open", "high", "low", "close")
SOURCE_FIELDS = {"symbol", "timeframe", "index", "time", "time_close", *OHLC}
EVENT_FIELDS = {"event_id", "zone_id", "state", "known_ms", "bar_index", "atr", "side", "origin_open_ms",
                "formed_ms", "proximal", "distal", "break_level", "pivot_known_ms", "fvg", "observation_only", "notification"}
NO_ORDER_FIELDS = {"entry", "sl", "tp1", "tp2", "tp3", "order", "quantity", "trade_permission"}
NUMERIC_FIELDS = {"atr", "proximal", "distal", "break_level", "atr14", "pivot_high", "pivot_low"}
TOLERANCE = Decimal("0.0000001")


@dataclass(frozen=True)
class Native:
    bars: tuple
    market: tuple
    events: tuple
    alert_intents: tuple
    last_confirmed_index: int
    file_sha256: str


def utc(stamp):
    return datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def timestamp(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Expected the TradingView log timestamp timezone")
    return round(stamp.timestamp() * 1000)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_zone_native(path, frame, symbol):
    raw = path.read_bytes()
    rows = csv.DictReader(StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
    require(rows.fieldnames == ["Date", "Message"], f"{path.name}: unexpected Pine Logs CSV format")
    bars, market, events, intents, marker = [], [], [], [], None
    for row in rows:
        for line in row["Message"].splitlines():
            tag, _, payload = line.partition("|")
            if tag not in {"PZ_MARKET", "PZ_EVENT", "PZ_ALERT_INTENT", "PZ_END"}:
                continue
            item = _json_dict(payload)
            logged_open = timestamp(row["Date"])
            if tag == "PZ_MARKET":
                require(SOURCE_FIELDS <= item.keys(), "Incomplete PZ_MARKET row")
                require(item["symbol"] == symbol and type(item["timeframe"]) is int and item["timeframe"] == frame,
                        "PZ_MARKET symbol/timeframe mismatch")
                require(type(item["index"]) is int and item["index"] == len(bars), "PZ_MARKET missing or duplicate source index")
                require(type(item["time"]) is int and type(item["time_close"]) is int
                        and item["time"] == logged_open and item["time_close"] - item["time"] == frame * 60_000,
                        "PZ_MARKET close or chart log timestamp mismatch")
                require("tick" not in item or item["tick"] == Decimal(str(TICK)), "Unexpected OANDA price tick")
                bars.append({"time": item["time"], "time_close": item["time_close"], **{k: float(item[k]) for k in OHLC}})
                market.append(item)
            elif tag == "PZ_EVENT":
                require(EVENT_FIELDS <= item.keys() and not NO_ORDER_FIELDS & item.keys()
                        and item["event_id"] == f"{item['zone_id']}|{item['state']}",
                        "Incomplete PZ_EVENT or invalid event ID")
                events.append((logged_open, item))
            elif tag == "PZ_ALERT_INTENT":
                intents.append((logged_open, item))
            else:
                require(marker is None and item.keys() == {"index", "time_close"}, "Invalid/repeated PZ_END")
                marker = logged_open, item
    require(bars and marker is not None, "Missing PZ_MARKET or PZ_END evidence")
    marker_open, end = marker
    require(type(end["index"]) is int and 0 <= end["index"] < len(bars) and
            bars[end["index"]]["time"] == marker_open and bars[end["index"]]["time_close"] == end["time_close"],
            "PZ_END does not identify its historical candle")
    prior_index, seen = -1, set()
    for logged_open, item in events:
        index = item["bar_index"]
        require(type(index) is int and prior_index <= index < len(bars), "PZ_EVENT index order or range mismatch")
        require(bars[index]["time_close"] == item["known_ms"] and bars[index]["time"] == logged_open,
                "PZ_EVENT is detached from its confirmed candle")
        require(item["event_id"] not in seen and item["origin_open_ms"] < item["formed_ms"] <= item["known_ms"]
                and item["pivot_known_ms"] < item["formed_ms"] and type(item["fvg"]) is bool,
                "PZ_EVENT duplicate ID or unavailable geometry")
        seen.add(item["event_id"])
        prior_index = index
    return Native(tuple(bars), tuple(market), tuple(item for _, item in events), tuple(intents),
                  end["index"], sha256(raw).hexdigest())


def notification(state, side):
    direction = "BUY" if side > 0 else "SELL"
    return {TOUCH: f"RESEARCH {direction} ZONE TOUCHED", REJECTION: f"RESEARCH {direction} REJECTION CONFIRMED",
            INVALIDATED: "RESEARCH ZONE INVALIDATED"}.get(state, "")


def event_payload(event, zone):
    return {"event_id": f"{zone.id}|{event.state}", "zone_id": zone.id, "state": event.state,
            "known_ms": event.known_ms, "bar_index": event.bar_index, "atr": event.atr,
            "side": zone.side, "origin_open_ms": zone.origin_open_ms, "formed_ms": zone.known_ms,
            "proximal": zone.proximal, "distal": zone.distal, "break_level": zone.break_level,
            "pivot_known_ms": zone.pivot_known_ms, "fvg": zone.fvg, "observation_only": True,
            "notification": notification(event.state, zone.side)}


def field_error(name, native, expected):
    if name in NUMERIC_FIELDS and native is not None and expected is not None:
        if type(native) not in (Decimal, int):
            return {"native": native, "expected": expected}
        gap = abs(Decimal(str(expected)) - native)
        return gap if gap > TOLERANCE else None
    return None if type(native) is type(expected) and native == expected else {"native": native, "expected": expected}


def event_compare(native, expected):
    max_atr_delta = Decimal(0)
    for index, (actual, wanted) in enumerate(zip_longest(native, expected), 1):
        if actual is None or wanted is None:
            return {"status": "MISMATCH", "row": index, "native": actual, "expected": wanted}
        for name, value in wanted.items():
            error = field_error(name, actual[name], value)
            if error is not None:
                return {"status": "MISMATCH", "row": index, "field": name, "native": actual, "expected": wanted}
        if actual["atr"] is not None:
            max_atr_delta = max(max_atr_delta, abs(actual["atr"] - Decimal(str(wanted["atr"]))))
    return {"status": "MATCH" if expected else "NO_EVENT_EXAMPLES", "rows": len(expected),
            "max_atr_abs_difference": max_atr_delta, "numeric_abs_tolerance": TOLERANCE}


def input_compare(native, calculated):
    checked = Counter()
    for actual, wanted in zip(native, calculated):
        for name in ("atr14", "pivot_high", "pivot_low", "fvg_buy", "fvg_sell"):
            if name not in actual:
                continue
            expected = getattr(wanted, name)
            if field_error(name, actual[name], expected) is not None:
                return {"status": "MISMATCH", "source_index": actual["index"], "field": name,
                        "native": actual[name], "expected": expected}
            checked[name] += 1
    return {"status": "MATCH" if checked else "NO_EXTRA_INPUT_FIELDS", "rows": dict(checked)}


def alert_compare(native, frame, symbol):
    by_close = {}
    for event in native.events:
        if event["notification"]:
            by_close.setdefault(event["known_ms"], []).append(event)
    if not native.alert_intents:
        return {"status": "NO_INTENT_CAPTURED", "eligible_events": sum(map(len, by_close.values())),
                "push_delivery": "NOT_VERIFIED_BY_PINE_LOGS"}
    open_by_close = {bar["time_close"]: bar["time"] for bar in native.bars}
    seen = set()
    for opened, item in native.alert_intents:
        stamp = item.get("known_ms")
        require(item.get("schema") == "qirvanta.precision-zones.research.v1" and item.get("symbol") == symbol
                and str(item.get("timeframe")) == str(frame) and open_by_close.get(stamp) == opened
                and stamp not in seen and stamp in by_close and item.get("events") == by_close[stamp],
                "Invalid or duplicate observational alert intent")
        seen.add(stamp)
    require(seen == by_close.keys(), "Native alert intents omit eligible confirmed events")
    return {"status": "MATCH", "batches": len(seen), "events": sum(map(len, by_close.values())),
            "push_delivery": "NOT_VERIFIED_BY_PINE_LOGS"}


def prefix_checks(bars, frame, rules, complete):
    indices = {0, len(bars) - 1, *(event.bar_index for event in complete.events)}
    for index in sorted(indices):
        observed = replay(bars[:index + 1], frame, rules)
        expected = (tuple(zone for zone in complete.zones if zone.formed_index <= index),
                    tuple(event for event in complete.events if event.bar_index <= index),
                    complete.snapshots[:index + 1], complete.inputs[:index + 1])
        if (observed.zones, observed.events, observed.snapshots, observed.inputs) != expected:
            return {"status": "MISMATCH", "source_index": index}
    return {"status": "MATCH", "tested_prefixes": len(indices), "all_event_closes": True}


def market_control(path, frame, native):
    control = read_control(path)
    same = control.timeframe == frame and control.first_index == 0 and control.bars == native.bars
    by_close = {bar["time_close"]: bar for bar in control.bars}
    shared = [bar for bar in native.bars if bar["time_close"] in by_close]
    return {"status": "MATCH" if same else "NONIDENTICAL_CONTEXT", "control_sha256": control.sha256,
            "control_bars": len(control.bars), "control_first_loaded_open_utc": utc(control.bars[0]["time"]),
            "control_last_known_close_utc": utc(control.bars[-1]["time_close"]),
            "common_confirmed_bars": len(shared),
            "common_ohlc_differences": sum(bar != by_close[bar["time_close"]] for bar in shared),
            "scope": "OHLC and exact calculation origin against a protected-chart journal"}


def opposite_ready_cases(path, calculated):
    views = {snapshot.known_ms: snapshot for snapshot in calculated.snapshots}
    result = []
    for row in journal_rows(path)["DTV_STATE"]:
        permission = row["permission"]
        if permission not in {"BUY READY", "SELL READY"}:
            continue
        view = views[row["known_ms"]]
        opposite = view.sell if permission == "BUY READY" else view.buy
        if opposite is not None:
            zone = opposite.zone
            result.append({"known_utc": utc(view.known_ms), "protected_permission": permission,
                           "opposite_zone_id": zone.id, "zone_status": opposite.status,
                           "proximal": zone.proximal, "distal": zone.distal})
    return {"scope": "Independent cross-check of matched journals. No permission changes or on-chart coupling.",
            "cases": result}


def frame_report(path, frame, rules, control_path=None):
    native = read_zone_native(path, frame, rules["symbol"])
    calculated = replay(native.bars, frame, rules)
    zones = {zone.id: zone for zone in calculated.zones}
    expected = tuple(event_payload(event, zones[event.zone_id]) for event in calculated.events)
    report = {"file": path.name, "sha256": native.file_sha256, "symbol": rules["symbol"], "frame": frame,
              "first_loaded_open_utc": utc(native.bars[0]["time"]), "last_known_close_utc": utc(native.bars[-1]["time_close"]),
              "historical_last_confirmed_close_utc": utc(native.bars[native.last_confirmed_index]["time_close"]),
              "bars": len(native.bars), "native_events": len(native.events), "python_events": len(expected),
              "event_counts": dict(Counter(row["state"] for row in native.events)),
              "event_parity": event_compare(native.events, expected), "market_input_parity": input_compare(native.market, calculated.inputs),
              "prefix_invariance": prefix_checks(native.bars, frame, rules, calculated),
              "alert_intents": alert_compare(native, frame, rules["symbol"])}
    if control_path is not None:
        report["protected_chart_market"] = market_control(control_path, frame, native)
        if report["protected_chart_market"]["status"] == "MATCH" and report["event_parity"]["status"] == "MATCH":
            report["opposite_ready_cases"] = opposite_ready_cases(control_path, calculated)
    report["examples"] = {state: [row for row in native.events if row["state"] == state][-3:]
                          for state in ("FORMATION_DETECTED", "FIRST_TOUCH", "REJECTION_CONFIRMED", "INVALIDATED", "EXPIRED")}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-dir", type=Path, required=True, help="Pine exports zone-M5/M15/M30.csv")
    parser.add_argument("--control-dir", type=Path, help="Optional same-load protected baseline-M5/M15/M30.csv")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rules = read_protocol()
    frames = {}
    for name in rules["timeframes"]:
        label = f"M{name}"
        source = args.native_dir / f"zone-{label}.csv"
        control = args.control_dir / f"baseline-{label}.csv" if args.control_dir else None
        if not source.is_file():
            frames[label] = {"status": "MISSING_EVIDENCE", "file": source.name}
            continue
        try:
            frames[label] = frame_report(source, int(name), rules, control)
        except (ValueError, KeyError, OSError, csv.Error) as exc:
            frames[label] = {"status": "INVALID_EVIDENCE", "reason": str(exc)}
    required = ("event_parity", "market_input_parity", "prefix_invariance")
    if args.control_dir:
        required += ("protected_chart_market",)
    passed = all(row.get("bars", 0) >= 1199
                 and all(row.get(check, {}).get("status") == "MATCH" for check in required)
                 and row.get("alert_intents", {}).get("status") != "MISMATCH"
                 for row in frames.values())
    result = {"status": "MATCH" if passed else "INCOMPLETE_OR_MISMATCH",
              "protocol_sha256": PROTOCOL_SHA256, "frames": frames}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n",
                           encoding="utf-8", newline="\n")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
