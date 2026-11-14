"""Validate and stitch fixed pre-seal H1 native TradingView captures."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import csv
from hashlib import sha256
import io
import json
from pathlib import Path
import re

from gold_pro import rc1_qa as rc1


SCHEMA = "qirvanta.h1-native-capture.v1"
BASELINE_COMMIT = "37b903f46a2d878d9d0757da134620c22f2d067d"
PRIMARY_FRAME = 15
PRIMARY_DEPTH = 5000
PRIMARY_FIRST_OPEN_MS = 1788172200000
PRIMARY_SCORE_START_MS = 1794355200000
PRIMARY_LAST_CLOSE_MS = 1794607200000
PRESEAL_MS = 1794787200000
MAX_WINDOW_BARS = 350
TOLERANCE = Decimal("0.00000005")
HTF_EMA_EPSILON = Decimal("0.001")
FIXED_COSTS = {"spread_price": .35, "slippage_ticks": 2, "fee_price": 0,
               "funding_long_per_day": 0, "funding_short_per_day": 0}
FRAMES = {5: "60", 15: "240", 30: "D"}
BOOK_NAMES = ("M1", "M2", "M3", "RAW", "FILTERED")
BOOK_PHASES = (0, 1, 2)
AMOUNTS = ("gross_r", "cost_r", "net_r")
BOOK_EVENT_TAGS = {"QUEUED", "SIM_ENTRY", "CANCELLED", "CLOSED", "CAPACITY", "AMBIGUOUS",
                   "SIM_SL", "SIM_TIME", "SIM_TP1", "SIM_TP2", "SIM_TP3", "TIME_PENDING", "STOP_UPDATE"}
CORE_EVENT_TAGS = rc1.CORE_STATES | {"EVIDENCE"}
COOLDOWN_REASON = "\u041f\u0430\u0443\u0437\u0430 \u0441\u043b\u0435\u0434 \u043f\u043e\u0442\u0432\u044a\u0440\u0434\u0435\u043d \u043c\u043e\u0434\u0435\u043b"
ACCEPTED_REASON = "\u0423\u0421\u041b\u041e\u0412\u0418\u042f\u0422\u0410 \u0421\u0410 \u0418\u0417\u041f\u042a\u041b\u041d\u0415\u041d\u0418"
FIELDS = {
    "H1_META": "version lane symbol frame requested_start_ms requested_end_ms first_open_ms current_index current_close_ms bar_count event_count",
    "H1_END": "version lane symbol frame requested_start_ms requested_end_ms first_open_ms current_index current_close_ms bar_count event_count",
    "H1_LAST": "version lane symbol frame first_open_ms last_index last_close_ms",
    "H1_BAR": "version lane symbol frame index open_ms close_ms open high low close prev_close gap warmup ready session_pass date_pass news_paused atr_pass acceptable_bar",
    "H1_HTF": "version lane close_ms core_frame source_close1_ms source_close3_ms close1 ema50_1 ema50_3 atr_mean rsi adx",
    "H1_STATE": "version lane close_ms side before_phase before_born_ms before_anchor before_level before_accepted_index after_phase after_born_ms after_anchor after_level after_accepted_index raw stock_cool exception_used reason same_side_last_fill_index same_side_last_fill_ms previous_accepted_plan_id qualified_cancel_plan_id cancellation_known_ms candidate_seed_ms global_paused global_paused_at family_paused family_paused_at",
    "H1_BOOK": "version lane close_ms book phase plan_id entry risk stop remaining hit1 hit2 hit3 fill_index fill_ms lock_index exit_next_open day_key day_net day_streak entered closed positive negative zero cancelled capacity_skipped ambiguous gross costs net gains losses peak drawdown hold_bars",
}
INT_FIELDS = set(
    "frame requested_start_ms requested_end_ms first_open_ms current_index current_close_ms bar_count event_count last_index last_close_ms index open_ms close_ms source_close1_ms source_close3_ms side before_phase before_born_ms before_accepted_index after_phase after_born_ms after_accepted_index same_side_last_fill_index same_side_last_fill_ms cancellation_known_ms candidate_seed_ms global_paused_at family_paused_at phase fill_index fill_ms lock_index day_key day_streak entered closed positive negative zero cancelled capacity_skipped ambiguous".split()
)
NUM_FIELDS = set(
    "open high low close prev_close close1 ema50_1 ema50_3 atr_mean rsi adx before_anchor before_level after_anchor after_level entry risk stop remaining day_net gross costs net gains losses peak drawdown hold_bars".split()
)
FLAGS = set(
    "gap warmup ready session_pass date_pass news_paused atr_pass acceptable_bar raw stock_cool exception_used global_paused family_paused hit1 hit2 hit3 exit_next_open".split()
)
ENCODED = {"reason", "plan_id", "previous_accepted_plan_id", "qualified_cancel_plan_id"}
COUNT_FIELDS = ("entered", "closed", "positive", "negative", "zero", "cancelled",
                "capacity_skipped", "ambiguous", "hold_bars")
BOOK_AMOUNTS = ("gross", "costs", "net", "gains", "losses", "peak", "drawdown")


def _utc(stamp):
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=stamp)).isoformat().replace("+00:00", "Z")


def _sha(data):
    return sha256(data).hexdigest()


def _hash(value, label):
    rc1.require(isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None,
                f"{label}: require lowercase SHA-256")
    return value


def _integer(value):
    if value == "null":
        return None
    rc1.require(re.fullmatch("-?(0|[1-9][0-9]*)", value) is not None, f"Expected canonical integer, got {value!r}")
    return int(value)


def _decimal(value):
    return None if value == "null" else rc1.number(value, "native numeric payload")


def _flag(value, *, optional=False):
    rc1.require(value in ("true", "false") or optional and value in ("NA", "null"),
                f"Invalid native boolean {value!r}")
    return None if value in ("NA", "null") else value == "true"


def _record(tag, body):
    fields = FIELDS[tag].split()
    values = body.split("|")
    rc1.require(len(fields) == len(values), f"{tag}: expected {len(fields)} fields, got {len(values)}")
    row = dict(zip(fields, values))
    rc1.require(row.pop("version") == "1" and row["lane"] in ("A", "B"), f"{tag}: schema or lane differs")
    for key, value in row.items():
        if key in INT_FIELDS:
            row[key] = _integer(value)
        elif key in NUM_FIELDS:
            row[key] = _decimal(value)
        elif key in FLAGS:
            row[key] = _flag(value, optional=key == "stock_cool")
        elif key in ENCODED:
            row[key] = value.replace("~", "|")
    return row


def _read_window(path, raw=None):
    raw = path.read_bytes() if raw is None else raw
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
    rc1.require(reader.fieldnames == ["Date", "Message"], f"{path.name}: unexpected Pine Logs header")
    tags = defaultdict(list)
    order = []
    stamped_rows = []
    lane_rows = {"H1_E", "H1_READY"}
    for item in reader:
        rc1.require(set(item) == {"Date", "Message"} and item["Date"] and item["Message"] is not None,
                    f"{path.name}: malformed Pine Logs row")
        for message in item["Message"].splitlines():
            tag, _, body = message.partition("|")
            if tag not in FIELDS and tag not in lane_rows:
                rc1.require(not tag.startswith("H1_"), f"{path.name}: unknown capture row {tag}")
                continue
            if tag in lane_rows:
                lane, separator, payload = body.partition("|")
                rc1.require(lane in ("A", "B") and separator, f"{tag}: invalid wrapper")
                row = rc1._json_dict(payload)
                row["lane"] = lane
            else:
                row = _record(tag, body)
            tags[tag].append(row)
            order.append(tag)
            stamped_rows.append((tag, row))

    rc1.require(len(tags["H1_META"]) == len(tags["H1_END"]) == 1 and len(tags["H1_LAST"]) <= 1,
                f"{path.name}: missing or duplicate window metadata/footer")
    rc1.require(order[0] == "H1_META" and order[-1] in ("H1_END", "H1_LAST") and
                (not tags["H1_LAST"] or order[-2:] == ["H1_END", "H1_LAST"] or order[-2:] == ["H1_LAST", "H1_END"]),
                f"{path.name}: misplaced window boundary records")
    meta, end = tags["H1_META"][0], tags["H1_END"][0]
    bars = tags["H1_BAR"]
    rc1.require(0 < len(bars) <= MAX_WINDOW_BARS and (meta["bar_count"], meta["event_count"]) == (0, 0),
                f"{path.name}: native window exceeded capture cap or first counts differ")
    shared = ("lane", "symbol", "frame", "first_open_ms", "requested_start_ms", "requested_end_ms")
    rc1.require(tuple(meta[k] for k in shared) == tuple(end[k] for k in shared),
                f"{path.name}: native window origin, lane or requested bounds changed")
    rc1.require(meta["symbol"] == rc1.SYMBOL and meta["frame"] in FRAMES and
                type(meta["first_open_ms"]) is int and type(meta["requested_start_ms"]) is int and
                type(meta["requested_end_ms"]) is int and
                0 < meta["first_open_ms"] < meta["requested_start_ms"] <= meta["requested_end_ms"] < PRESEAL_MS,
                f"{path.name}: invalid window market or pre-seal clocks")
    first, last = bars[0], bars[-1]
    rc1.require((first["index"], first["close_ms"], first["close_ms"]) ==
                (meta["current_index"], meta["current_close_ms"], meta["requested_start_ms"]) and
                (last["index"], last["close_ms"], last["close_ms"]) ==
                (end["current_index"], end["current_close_ms"], end["requested_end_ms"]) and
                (end["bar_count"], end["event_count"]) == (len(bars), len(tags["H1_E"])),
                f"{path.name}: capture footer does not confirm exact boundaries or counts")
    rc1.require(all(row["lane"] == meta["lane"] for rows in tags.values() for row in rows),
                f"{path.name}: mixed capture lanes")
    active_close = None
    clock_key = {"H1_HTF": "close_ms", "H1_STATE": "close_ms", "H1_BOOK": "close_ms",
                 "H1_E": "known_time", "H1_READY": "confirmed_ms"}
    for tag, row in stamped_rows:
        if tag == "H1_BAR":
            active_close = row["close_ms"]
        elif tag in clock_key:
            rc1.require(active_close is not None and row.get(clock_key[tag]) == active_close,
                        f"{path.name}: {tag} not recorded inside corresponding confirmed candle")
    if tags["H1_LAST"]:
        witness = tags["H1_LAST"][0]
        rc1.require((witness["symbol"], witness["frame"], witness["first_open_ms"], witness["last_index"],
                     witness["last_close_ms"]) == (meta["symbol"], meta["frame"], meta["first_open_ms"],
                                                   last["index"], last["close_ms"]),
                    f"{path.name}: terminal historical witness differs")
    return {
        "lane": meta["lane"], "symbol": meta["symbol"], "frame": meta["frame"], "first_open_ms": meta["first_open_ms"],
        "start_close_ms": first["close_ms"], "end_close_ms": last["close_ms"],
        "first_index": first["index"], "last_index": last["index"],
        "bars": len(bars), "events": len(tags["H1_E"]), "has_historical_end": bool(tags["H1_LAST"]),
        "sha256": _sha(raw),
    }, tags


def inspect_window(path):
    """Validate the native CSV envelope and return manifest-building metadata."""
    return _read_window(Path(path))[0]


def _manifest(path):
    data = path.read_bytes()
    meta = rc1._json_dict(data.decode("utf-8-sig"))
    required = {
        "schema", "lane", "symbol", "frame_minutes", "chart_type", "chart_timezone", "source_sha256",
        "settings", "modeled_costs", "provenance", "first_open_ms", "fixed_end_close_ms", "scoring_start_ms",
        "calculated_bars", "windows",
    }
    rc1.require(required <= meta.keys() and meta["schema"] == SCHEMA, f"{path.name}: invalid capture manifest schema")
    rc1.require((meta["symbol"], meta["chart_type"], meta["chart_timezone"]) == (rc1.SYMBOL, "standard", "UTC")
                and meta["lane"] in ("A", "B") and type(meta["frame_minutes"]) is int and meta["frame_minutes"] in FRAMES,
                f"{path.name}: manifest market, chart or lane differs")
    rc1.require(type(meta["calculated_bars"]) is int and meta["calculated_bars"] >= PRIMARY_DEPTH,
                f"{path.name}: calculated bar count below the registered primary depth")
    for key in ("first_open_ms", "fixed_end_close_ms", "scoring_start_ms"):
        rc1.epoch(meta[key], key)
    rc1.require(meta["first_open_ms"] < meta["scoring_start_ms"] <= meta["fixed_end_close_ms"] < PRESEAL_MS,
                f"{path.name}: invalid or sealed date bounds")
    _hash(meta["source_sha256"], "collector source")
    expected_settings = {"core_source_commit": BASELINE_COMMIT, "price_feed": "standard", "strategy_mode": "S5",
                         "automatic_htf": True, "htf": FRAMES[meta["frame_minutes"]], "manual_news_pause": False,
                         "timed_news_window": False, "gap_filter": True, "evidence_shield": True}
    rc1.require(isinstance(meta["settings"], dict) and all(meta["settings"].get(k) == v and
                type(meta["settings"].get(k)) is type(v) for k, v in expected_settings.items()),
                "Native script settings are incomplete or differ from the registered control")
    provenance = meta["provenance"]
    rc1.require(isinstance(provenance, dict) and all(isinstance(provenance.get(k), str) and provenance[k].strip()
                for k in ("profile", "capture_id", "mode")), "Missing authorized native provenance fields")
    rc1.require(provenance["mode"] in ("add", "reload", "readd"), "Unknown native capture mode")
    costs = meta["modeled_costs"]
    rc1.require(isinstance(costs, dict) and FIXED_COSTS.keys() == costs.keys() and all(
        rc1.json_number(costs[key], key) == Decimal(str(expected)) for key, expected in FIXED_COSTS.items()
    ), "Collector costs differ from the frozen Visual V2 model")
    rc1.require(isinstance(meta["windows"], list) and meta["windows"], "No native CSVs in manifest")
    return meta, _sha(data)


def _window_path(root, name):
    rc1.require(isinstance(name, str) and name and not Path(name).is_absolute(), "Window path must be relative")
    path = (root / name).resolve()
    rc1.require(path.is_relative_to(root), "Window path leaves the capture directory")
    return path


def _collect(path, manifest):
    tags = defaultdict(list)
    windows = []
    seen = set()
    cursor = 0
    for item in manifest["windows"]:
        rc1.require(isinstance(item, dict) and {"path", "sha256", "start_close_ms", "end_close_ms", "bars", "events"} <= item.keys(),
                    "Malformed native file manifest entry")
        name = item["path"]
        csv_path = _window_path(path.parent, name)
        rc1.require(csv_path not in seen, "Manifest repeats the same native CSV")
        seen.add(csv_path)
        raw = csv_path.read_bytes()
        rc1.require(_sha(raw) == _hash(item["sha256"], name), f"{name}: native file SHA-256 changed")
        window, data = _read_window(csv_path, raw)
        rc1.require(all(window[key] == item[key] for key in ("start_close_ms", "end_close_ms", "bars", "events")),
                    f"{name}: false manifest calendar or row counts")
        rc1.require((window["lane"], window["symbol"], window["frame"], window["first_open_ms"], window["first_index"]) ==
                    (manifest["lane"], manifest["symbol"], manifest["frame_minutes"], manifest["first_open_ms"], cursor),
                    f"{name}: different run origin, lane or missing/overlapping chart indices")
        for tag in ("H1_BAR", "H1_HTF", "H1_STATE", "H1_BOOK", "H1_E", "H1_READY"):
            tags[tag].extend({k: v for k, v in row.items() if k != "lane"} for row in data[tag])
        cursor = window["last_index"] + 1
        windows.append({**window, "path": name})
    rc1.require(all(not row["has_historical_end"] for row in windows[:-1]) and windows[-1]["has_historical_end"] and
                tags["H1_BAR"][-1]["close_ms"] == manifest["fixed_end_close_ms"],
                "The final capture alone must reach the same observed historical endpoint")
    depth = manifest["calculated_bars"]
    rc1.require(len(tags["H1_BAR"]) in (depth - 1, depth), "Observed natural history does not match recorded Calculated bars")
    rc1.require(len(tags["H1_BAR"]) == cursor, "Stitched per-bar state count disagrees with native last index")
    return tags, windows


def _market(rows, manifest):
    previous = None
    frame = manifest["frame_minutes"]
    step = frame * 60_000
    for i, row in enumerate(rows):
        opening, close = row["open_ms"], row["close_ms"]
        rc1.require((row["index"], row["symbol"], row["frame"]) == (i, manifest["symbol"], frame),
                    "Invalid or disordered native candle index, symbol or timeframe")
        rc1.require(type(opening) is int and type(close) is int and opening % step == 0 and close == opening + step and close < PRESEAL_MS,
                    f"Misaligned or excluded chart candle at index {i}")
        o, h, low, c = (row[key] for key in ("open", "high", "low", "close"))
        rc1.require(all(key is not None for key in (o, h, low, c)) and 0 < low <= min(o, c) <= max(o, c) <= h,
                    f"Invalid native OHLC at index {i}")
        if previous is None:
            rc1.require(opening == manifest["first_open_ms"] and not row["gap"],
                        "Native window omits natural chart origin")
        else:
            rc1.require(opening >= previous["close_ms"] and row["prev_close"] == previous["close"] and
                        row["gap"] == (opening > previous["close_ms"]),
                        f"Native previous close or session gap differs at index {i}")
        previous = row
    by_close = {row["close_ms"]: row for row in rows}
    rc1.require(manifest["scoring_start_ms"] in by_close and manifest["fixed_end_close_ms"] == rows[-1]["close_ms"],
                "Registered score-start or final close absent from native market history")
    return by_close


def _context(rows, bars, manifest):
    rc1.require(tuple(row["close_ms"] for row in rows) == tuple(bars), "Missing or duplicated per-bar CORE HTF context")
    previous_source = (None, None)
    expected_frame = manifest["settings"]["htf"]
    seen_values = defaultdict(dict)
    for row in rows:
        chart = bars[row["close_ms"]]
        first, earlier = row["source_close1_ms"], row["source_close3_ms"]
        rc1.require(row["core_frame"] == expected_frame, "Unexpected selected CORE HTF resolution")
        for stamp in (first, earlier):
            rc1.require(stamp is None or type(stamp) is int and 0 < stamp <= chart["open_ms"],
                        f'CORE HTF clock uses information after bar open at {row["close_ms"]}')
        rc1.require(earlier is None or first is not None and earlier < first, "Invalid HTF source [3]/[1] order")
        for new, prior in zip((first, earlier), previous_source):
            rc1.require(prior is None or new is not None and new >= prior, "Confirmed HTF clock regressed")
        previous_source = first, earlier
        for stamp, field in ((first, "close1"), (first, "ema50_1"), (earlier, "ema50_3")):
            value = row[field]
            seen = seen_values[field]
            if stamp is not None and value is not None:
                rc1.require(stamp not in seen or seen[stamp] == value,
                            f"Confirmed HTF {field} changed within its source candle")
                seen[stamp] = value
        ready = all(row[key] is not None for key in ("close1", "ema50_1", "ema50_3"))
        require_clock = (row["close1"] is None or first is not None) and (row["ema50_1"] is None or first is not None) and (
            row["ema50_3"] is None or earlier is not None)
        warmed = chart["index"] > 250 and ready and all(row[key] is not None for key in ("atr_mean", "rsi", "adx"))
        rc1.require(require_clock and (chart["ready"], chart["warmup"]) == (ready, warmed),
                    f'CORE HTF tuple/clock cannot substantiate ready/warmup at {row["close_ms"]}')
        row["direction"] = (1 if row["close1"] > row["ema50_1"] > row["ema50_3"] else
                            -1 if row["close1"] < row["ema50_1"] < row["ema50_3"] else 0) if ready else None
    return rows


def _events(rows, ready, bars, manifest):
    ids, ready_ids = set(), set()
    by_close = defaultdict(list)
    by_id = {}
    prior, prior_ready = 0, 0
    for item in rows:
        rc1.require(rc1.CORE_FIELDS <= item.keys(), "Native event is missing protected fields")
        stamp = item["known_time"]
        book, tag = item["book"], item["event"]
        rc1.require(item["schema"] == "qirvanta.event.v1" and
                    item["core_sha256"] == rc1.CORE_SHA and item["strategy_key"] == "S5",
                    "Unexpected protected native event model or source SHA")
        rc1.require(type(stamp) is int and stamp in bars and prior <= stamp and
                    (item["symbol"], item["timeframe"]) == (manifest["symbol"], str(manifest["frame_minutes"])),
                    "Native event outside confirmed frame or out of order")
        candle = bars[stamp]
        rc1.require((item["bar_open_time"], item["bar_close_time"]) == (candle["open_ms"], stamp) and
                    (item["effective_time"] is None or
                     type(item["effective_time"]) is int and candle["open_ms"] <= item["effective_time"] <= stamp),
                    "Native event clock precedes the current chart candle or lies in the future")
        plan = rc1.nonempty(item["plan_id"], "native plan ID")
        identity = item["event_id"]
        rc1.require(identity not in ids and identity == f"{plan}|{book}|{tag}|{stamp}",
                    "Duplicate or malformed native event ID")
        rc1.require(book == "CORE" and tag in CORE_EVENT_TAGS or book in BOOK_NAMES and
                    (tag in BOOK_EVENT_TAGS or book == "RAW" and tag == "CONFLICT"),
                    "Native event name is invalid for the given book")
        is_marker = tag in {"EVIDENCE", "CONFLICT"}
        rc1.require(item["advisory_only"] is True and type(item["family"]) is int and type(item["side"]) is int and
                    (item["side"] == 0 if is_marker else item["side"] in (-1, 1) and item["family"] == 2),
                    "Native event data does not represent a valid S5 plan or advisory marker")
        if item["reference"] is not None:
            rc1.require(rc1.json_number(item["roundtrip_cost_price"], "native modeled cost price") == Decimal(".354"),
                        "Native plan modeled price cost differs from the frozen configuration")
        ids.add(identity)
        by_id[identity] = item
        by_close[stamp].append(item)
        prior = stamp
    for item in ready:
        rc1.require(rc1.PAYLOAD_FIELDS <= item.keys(), "Native READY is missing protected fields")
        stamp = item["confirmed_ms"]
        source = by_id.get(item["source_id"])
        side = 1 if item["direction"] == "BUY" else -1 if item["direction"] == "SELL" else None
        plan = {key: source.get(src) for key, src in rc1.PLAN_FIELDS.items()} if source else {}
        rc1.require(type(stamp) is int and stamp in bars and prior_ready <= stamp and
                    (item["symbol"], item["timeframe"], item["model"], item["state"], item["provisional"]) ==
                    (manifest["symbol"], str(manifest["frame_minutes"]), "S5", "READY", False) and
                    source is not None and (source["known_time"], source["book"], source["event"], source["side"]) ==
                    (stamp, "CORE", "CONFIRMED", side) and item["event_id"] == f'{source["event_id"]}|RC1|READY'
                    and item["confirmation_utc"] == _utc(stamp).replace(".000Z", "Z")
                    and all(item[k] == val for k, val in plan.items()) and item["event_id"] not in ready_ids,
                    "READY has no unique matching protected confirmation and original geometry")
        ready_ids.add(item["event_id"])
        prior_ready = stamp
    return by_close


def _book_counters(row, previous, events):
    tally = Counter(event["event"] for event in events)
    closed = [event for event in events if event["event"] == "CLOSED"]
    outcomes = [rc1.json_number(event["net_r"], "native CLOSED net risk units") for event in closed]
    expected = {
        key: tally[event] for key, event in (
            ("entered", "SIM_ENTRY"), ("closed", "CLOSED"), ("cancelled", "CANCELLED"),
            ("capacity_skipped", "CAPACITY"), ("ambiguous", "AMBIGUOUS")
        )
    }
    expected["positive"] = sum(value > TOLERANCE for value in outcomes)
    expected["negative"] = sum(value < -TOLERANCE for value in outcomes)
    uncertain = sum(abs(value) <= TOLERANCE for value in outcomes)
    for name in COUNT_FIELDS:
        field = row[name]
        rc1.require(type(field) in (int, Decimal) and field >= previous[name], f"Native {name} counter regressed")
        if name in expected:
            observed = field - previous[name]
            maximum = expected[name] + (uncertain if name in ("positive", "negative") else 0)
            rc1.require(expected[name] <= observed <= maximum, f"Native {name} lacks a matching book event")
    rc1.require(row["closed"] == row["positive"] + row["negative"] + row["zero"] and row["closed"] <= row["entered"],
                "Native campaign closure counts do not partition by result")
    for key, event_field in (("gross", "gross_r"), ("costs", "cost_r"), ("net", "net_r")):
        expected_delta = sum((rc1.json_number(event[event_field], event_field) for event in closed), Decimal(0))
        rc1.require(abs(row[key] - previous[key] - expected_delta) <= TOLERANCE,
                    f"Native {key} does not reconcile to the closed campaign ledger")
    rc1.require(abs(row["gross"] - row["costs"] - row["net"]) <= TOLERANCE and
                abs(row["gains"] - row["losses"] - row["net"]) <= TOLERANCE and
                row["peak"] >= previous["peak"] and row["drawdown"] >= previous["drawdown"] and
                row["peak"] >= row["net"] - TOLERANCE and row["drawdown"] >= row["peak"] - row["net"] - TOLERANCE,
                "Native book equity, cumulative P&L or drawdown is inconsistent")


def _book_transition(row, previous, events, chart, by_index):
    rc1.require(isinstance(row["plan_id"], str) and row["phase"] in BOOK_PHASES and
                bool(row["plan_id"]) == bool(row["phase"]), "Invalid book slot or missing active plan")
    phase, plan = previous["phase"], previous["plan_id"]
    fractions = Decimal(0)
    current_stop = previous.get("stop")
    starting_remaining = previous.get("remaining") or Decimal(0)
    for event in events:
        kind, identity = event["event"], event["plan_id"]
        if kind == "CONFLICT":
            continue
        if kind == "QUEUED":
            rc1.require(phase == 0 and event["effective_time"] == chart["close_ms"], "Queued plan overwrote an occupied slot")
            phase, plan = 1, identity
        elif kind == "CAPACITY":
            rc1.require(phase != 0 and identity != plan, "Capacity witness lacks distinct slot occupant")
        elif kind == "SIM_ENTRY":
            rc1.require(phase == 1 and plan == identity and
                        event["effective_time"] == chart["open_ms"] and
                        rc1.json_number(event["quantity_fraction"], "entry fraction") == 1,
                        "Native fill lacked its pending plan or matching next open")
            phase = 2
            starting_remaining = Decimal(1)
            current_stop = event["initial_sl"]
        elif kind == "CANCELLED":
            rc1.require(phase == 1 and plan == identity and event["effective_time"] == chart["open_ms"],
                        "Zero-fill cancel lacked its pending plan and actual next open")
            phase, plan = 0, ""
        elif kind == "CLOSED":
            rc1.require(phase == 2 and plan == identity, "Native CLOSED lacked an open campaign")
            rc1.require(abs(starting_remaining - fractions) <= TOLERANCE, "Native CLOSED did not settle its remaining legs")
            phase, plan = 0, ""
        else:
            rc1.require(phase == 2 and plan == identity, "Native exit or risk event lacked the open campaign")
            if kind == "STOP_UPDATE":
                stop = rc1.json_number(event["price"], "tightened stop")
                rc1.require(current_stop is not None and (stop - current_stop) * event["side"] > 0,
                            "Native updated stop expanded risk or was not observable")
                current_stop = stop
            if kind in {"SIM_SL", "SIM_TIME", "SIM_TP1", "SIM_TP2", "SIM_TP3"}:
                fraction = rc1.json_number(event["quantity_fraction"], "native exit fraction")
                rc1.require(0 < fraction <= 1, "Invalid native exit fraction")
                fractions += fraction
    rc1.require((phase, plan) == (row["phase"], row["plan_id"]), "Book slot state does not match native event sequence")
    deferred = row["lock_index"]
    rc1.require(deferred is None or type(deferred) is int and deferred <= chart["index"] + 1,
                "Stop update clock is more than one chart bar ahead")
    if phase == 1:
        rc1.require(any(event["event"] == "QUEUED" and event["plan_id"] == plan for event in events),
                    "Pending order was carried past its next observed chart candle")
    if phase == 2:
        fill = by_index.get(row["fill_index"])
        rc1.require(fill is not None and fill["index"] <= chart["index"] and fill["open_ms"] == row["fill_ms"] and
                    all(row[key] is not None for key in ("entry", "risk", "stop", "remaining")) and
                    row["risk"] > 0 and row["stop"] == current_stop and Decimal(0) < row["remaining"] <= Decimal(1) and
                    abs(starting_remaining - fractions - row["remaining"]) <= TOLERANCE,
                    "Open campaign lacks causal effective fill or remaining risk state")


def _books(rows, bars, events):
    recorded = defaultdict(list)
    for row in rows:
        book = row["book"]
        rc1.require(book in BOOK_NAMES, "Unexpected native book")
        recorded[book].append(row)
    by_index = {bar["index"]: bar for bar in bars.values()}
    rc1.require(set(recorded) == set(BOOK_NAMES), "Native export is missing a book")
    by_book_clock = {
        (name, stamp): [event for event in events[stamp] if event["book"] == name]
        for stamp in events for name in BOOK_NAMES
    }
    for book, states in recorded.items():
        rc1.require(tuple(row["close_ms"] for row in states) == tuple(bars), f"{book}: incomplete native per-bar book state")
        previous = dict.fromkeys((*COUNT_FIELDS, *BOOK_AMOUNTS), Decimal(0))
        previous.update(phase=0, plan_id="", remaining=Decimal(1))
        for row in states:
            stamp = row["close_ms"]
            chart = bars[stamp]
            at_close = by_book_clock.get((book, stamp), ())
            rc1.require(all(row[key] is not None for key in (*COUNT_FIELDS, *BOOK_AMOUNTS, "remaining")),
                        f"{book}: missing native state at {stamp}")
            _book_counters(row, previous, at_close)
            _book_transition(row, previous, at_close, chart, by_index)
            previous = row
    return {book: tuple(states) for book, states in recorded.items()}


def _states(rows, bars, events, manifest):
    clocks = tuple((stamp, side) for stamp in bars for side in (1, -1))
    rc1.require(tuple((row["close_ms"], row["side"]) for row in rows) == clocks,
                "Capture lacks exactly two native formation states at each confirmed close")
    current = {
        side: {"phase": 0, "born_ms": None, "anchor": None, "level": None, "accepted_index": None,
               "last_id": "", "last_fill_index": None, "last_fill_ms": None, "queued_index": None,
               "qualified_cancel": "", "cancel_known": None} for side in (1, -1)
    }
    formation = ("phase", "born_ms", "anchor", "level", "accepted_index")
    cooldown = manifest["settings"].get("min_cooldown", 8)
    rc1.require(type(cooldown) is int and cooldown == 8, "Frozen original cooldown changed")
    for start in range(0, len(rows), 2):
        stamp = rows[start]["close_ms"]
        index = bars[stamp]["index"]
        at_close = events.get(stamp, ())
        shield_fields = ("global_paused", "global_paused_at", "family_paused", "family_paused_at")
        rc1.require(all(rows[start][k] == rows[start+1][k] for k in shield_fields), "Shared native evidence gate differs by side")
        for row in rows[start:start+2]:
            side = row["side"]
            state = current[side]
            side_events = [item for item in at_close if item["side"] == side]
            confirms = [item for item in side_events if item["book"] == "CORE" and item["event"] == "CONFIRMED"]
            rc1.require(tuple(row[f"before_{k}"] for k in formation) == tuple(state[k] for k in formation),
                        f"{stamp}: broken S5 formation prefix or chronology")
            rc1.require((row["previous_accepted_plan_id"], row["same_side_last_fill_index"], row["same_side_last_fill_ms"],
                         row["qualified_cancel_plan_id"], row["cancellation_known_ms"]) ==
                        (state["last_id"], state["last_fill_index"], state["last_fill_ms"],
                         state["qualified_cancel"], state["cancel_known"]),
                        f"{stamp}: native H1 memory appears unseeded, mismatched or ahead of its source event")
            raw, previous_accept = row["raw"], row["before_accepted_index"]
            stock = previous_accept is None or index - previous_accept > cooldown
            rc1.require(row["stock_cool"] == (stock if raw else None) and
                        (row["candidate_seed_ms"] is not None) == raw and
                        (not raw or row["before_born_ms"] is not None and row["candidate_seed_ms"] == row["before_born_ms"] < stamp),
                        f"{stamp}: native gate cooldown or candidate clock differs")
            rc1.require(row["before_phase"] in (0, 1) and row["after_phase"] in (0, 1) and len(confirms) <= 1,
                        f"{stamp}: invalid S5 formation phase or duplicate S5 confirmation")
            if row["exception_used"]:
                last_fill = state["last_fill_index"]
                cancelled_at, seed = state["cancel_known"], row["candidate_seed_ms"]
                rc1.require(manifest["lane"] == "B" and raw and not stock and len(confirms) == 1 and
                            bool(state["last_id"]) and state["last_id"] == state["qualified_cancel"] and
                            confirms[0]["plan_id"] != state["last_id"] and
                            cancelled_at is not None and cancelled_at < seed < stamp and
                            (last_fill is None or index - last_fill > cooldown),
                            f"{stamp}: H1 exception exceeds registered causally known zero-fill scope")
            if confirms:
                rc1.require(raw and (stock or row["exception_used"]) and row["reason"] == ACCEPTED_REASON and
                            row["after_accepted_index"] == index,
                            f"{stamp}: accepted S5 confirmation does not match native decision state")
                state.update(last_id=confirms[0]["plan_id"], queued_index=None, qualified_cancel="", cancel_known=None)
            else:
                rc1.require(row["after_accepted_index"] == previous_accept,
                            f"{stamp}: S5 accepted clock moved without a native confirmation")
            state.update({key: row[f"after_{key}"] for key in formation})
            for event in side_events:
                if event["book"] != "RAW":
                    continue
                matched = event["plan_id"] == state["last_id"] and bool(state["last_id"])
                kind = event["event"]
                if kind == "SIM_ENTRY":
                    state.update(last_fill_index=index, last_fill_ms=event["effective_time"])
                    if matched:
                        state.update(qualified_cancel="", cancel_known=None)
                elif kind == "QUEUED" and matched:
                    state["queued_index"] = index
                elif (kind == "CANCELLED" and matched and state["queued_index"] == index - 1 and
                      event["effective_time"] == bars[stamp]["open_ms"]):
                    state.update(qualified_cancel=event["plan_id"], cancel_known=stamp)
    return rows


def validate_export(manifest_path):
    """Stitch and validate one native lane without reshaping or inferring missing events."""
    path = Path(manifest_path).resolve()
    manifest, digest = _manifest(path)
    tags, windows = _collect(path, manifest)
    bars = _market(tags["H1_BAR"], manifest)
    htf = _context(tags["H1_HTF"], bars, manifest)
    events = _events(tags["H1_E"], tags["H1_READY"], bars, manifest)
    books = _books(tags["H1_BOOK"], bars, events)
    states = _states(tags["H1_STATE"], bars, events, manifest)
    history_sha = _sha(json.dumps(
        [_ohlc(bar) for bar in bars.values()], ensure_ascii=False, separators=(",", ":"), default=str
    ).encode("utf-8"))
    summary = {
        "capture_id": manifest["provenance"]["capture_id"], "lane": manifest["lane"],
        "first_open_ms": manifest["first_open_ms"], "first_close_ms": next(iter(bars)),
        "last_close_ms": manifest["fixed_end_close_ms"], "score_start_ms": manifest["scoring_start_ms"],
        "source_sha256": manifest["source_sha256"], "manifest_sha256": digest,
        "market_history_sha256": history_sha,
        "distinct_csv_files": len({row["sha256"] for row in windows}),
        "confirmed_bars": len(bars), "window_count": len(windows),
        "frame_minutes": manifest["frame_minutes"], "event_count": len(tags["H1_E"]),
        "native_ready_count": len(tags["H1_READY"]),
        "raw_h1_exception_count": sum(row["exception_used"] for row in states),
    }
    return {
        "status": "VALIDATED_COMPLETE_NATIVE_CAPTURE", "manifest": manifest, "manifest_sha256": digest,
        "metadata": summary, "windows": tuple(windows), "bars": tuple(bars.values()),
        "bars_by_index": {row["index"]: row for row in bars.values()},
        "htf": tuple(htf), "state": tuple(states), "books": books,
        "events": tuple(tags["H1_E"]), "ready": tuple(tags["H1_READY"]),
    }


def _ohlc(row):
    return tuple(row[key] for key in ("open_ms", "close_ms", "open", "high", "low", "close"))


def _through(records, key, end):
    return tuple(row for row in records if row[key] < end)


def _primary(run):
    meta = run["manifest"]
    return (meta["frame_minutes"], meta["calculated_bars"], meta["first_open_ms"],
            meta["scoring_start_ms"], meta["fixed_end_close_ms"]) == (
                PRIMARY_FRAME, PRIMARY_DEPTH, PRIMARY_FIRST_OPEN_MS, PRIMARY_SCORE_START_MS, PRIMARY_LAST_CLOSE_MS
            ) and len(run["bars"]) == PRIMARY_DEPTH


def compare_runs(left, right, *, kind):
    """Compare exact repeats, fixed A/B captures, or a separately deeper A run."""
    if kind not in ("same", "ab", "depth"):
        raise ValueError("Unknown H1 comparison kind")
    a, b = left["manifest"], right["manifest"]
    if "capture_id" not in a["provenance"] or "capture_id" not in b["provenance"]:
        raise ValueError("Capture provenance absent")
    result = {
        "kind": kind, "left": left["metadata"], "right": right["metadata"],
        "left_manifest_sha256": left["manifest_sha256"], "right_manifest_sha256": right["manifest_sha256"],
        "diagnostic_only": True, "registered_primary_pair": _primary(left) and _primary(right),
    }
    bad = {**result, "status": "INVALID_COMPARISON"}
    context_fields = ("schema", "symbol", "frame_minutes", "chart_type", "chart_timezone",
                      "source_sha256", "settings", "modeled_costs", "fixed_end_close_ms", "scoring_start_ms")
    if (a["provenance"]["capture_id"] == b["provenance"]["capture_id"] or
        a["provenance"]["profile"] != b["provenance"]["profile"] or
        not all(a[k] == b[k] for k in context_fields)):
        return {**bad, "reason": "Capture IDs must differ, with identical source, market, settings, clock and cost inputs"}
    if kind == "depth":
        if a["lane"] != "A" or b["lane"] != "A" or not _primary(left) or len(right["bars"]) <= len(left["bars"]):
            return {**bad, "reason": "Independent depth check requires registered A at 5000 and distinct strictly deeper A"}
        n = len(left["bars"])
        if tuple(map(_ohlc, left["bars"])) != tuple(map(_ohlc, right["bars"][-n:])):
            return {**bad, "reason": "Deeper A lacks an exact OHLC tail and fixed matching end"}
        chart = {item["close_ms"]: item for item in left["bars"]}
        deep_chart = {item["close_ms"]: item for item in right["bars"]}
        rhs = {item["close_ms"]: item for item in right["htf"]}
        unstable = []
        n_warm = 0
        max_deltas = dict.fromkeys(("ema50_1", "ema50_3"), None)
        for shallow in left["htf"]:
            stamp = shallow["close_ms"]
            if stamp < a["scoring_start_ms"]:
                continue
            deep = rhs[stamp]
            close_and_sources = ("source_close1_ms", "source_close3_ms", "close1", "direction")
            why = [f for f in close_and_sources if shallow[f] != deep[f]]
            for key in ("ready", "warmup"):
                if chart[stamp][key] != deep_chart[stamp][key]:
                    why.append(key)
            n_warm += chart[stamp]["warmup"] and deep_chart[stamp]["warmup"]
            for name in max_deltas:
                x, y = shallow[name], deep[name]
                if x is None or y is None:
                    if x != y:
                        why.append(name)
                    continue
                delta = abs(x - y)
                max_deltas[name] = delta if max_deltas[name] is None else max(delta, max_deltas[name])
                if delta > HTF_EMA_EPSILON:
                    why.append(name)
            if why:
                unstable.append({"close_ms": stamp, "fields": why})
        scored = sum(key >= a["scoring_start_ms"] for key in chart)
        return {
            **result, "registered_primary_left": True, "overlap_bars_exact": n, "scored_closes": scored,
            "jointly_warmed_closes": n_warm, "max_observed_ema_absolute_delta": max_deltas,
            "ema_tolerance": HTF_EMA_EPSILON, "unstable_closes": unstable,
            "status": "VALIDATED_HTF_DEPTH_STABILITY" if not unstable and n_warm == scored else "DEPTH_STABILITY_UNESTABLISHED",
        }
    if ((a["first_open_ms"], a["calculated_bars"]) != (b["first_open_ms"], b["calculated_bars"]) or
        left["bars"] != right["bars"] or left["htf"] != right["htf"]):
        return {**bad, "reason": "Natural origin, exact complete OHLC, confirmed HTF or bar gates differ"}
    if kind == "same":
        if a["lane"] != b["lane"] or b["provenance"]["mode"] not in ("reload", "readd"):
            return {**bad, "reason": "Repeat must be the same lane and an independently captured reload/re-add"}
        keys = ("events", "ready", "state", "books")
        errors = [field for field in keys if left[field] != right[field]]
        return {**result, "status": "VALIDATED_EXACT_REPEAT" if not errors else "INVALID_COMPARISON", "mismatching_streams": errors}
    if not result["registered_primary_pair"] or (a["lane"], b["lane"]) != ("A", "B"):
        return {**bad, "reason": "A/B score requires registered lane A followed by lane B on exact fixed primary history"}
    activation = [row["close_ms"] for row in right["state"] if row["exception_used"]]
    first = min(activation, default=a["fixed_end_close_ms"] + 1)
    mismatches = [name for name, key in (("events", "known_time"), ("ready", "confirmed_ms"), ("state", "close_ms"))
                  if _through(left[name], key, first) != _through(right[name], key, first)]
    mismatches.extend(book for book in BOOK_NAMES if
                      _through(left["books"][book], "close_ms", first) != _through(right["books"][book], "close_ms", first))
    if mismatches:
        return {**bad, "reason": "Native lanes diverge before the first logged H1 exception", "mismatching_streams": mismatches}
    return {
        **result, "status": "VALIDATED_MATCHED_AB", "pre_exception_exact": True,
        "scoring_start_ms": a["scoring_start_ms"], "fixed_end_close_ms": a["fixed_end_close_ms"],
        "first_exception_ms": activation[0] if activation else None,
        "exception_closes": sorted(set(activation)), "exception_count": len(activation),
        "qualification": "Bounded same-input diagnostic. Pinned A parity, independently repeated lanes and depth stability are separate.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--compare", type=Path)
    parser.add_argument("--kind", choices=("same", "ab", "depth"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if bool(args.compare) != bool(args.kind):
        parser.error("--compare and --kind must be supplied together")
    try:
        run = validate_export(args.manifest)
        report = {"status": run["status"], **run["metadata"], "windows": run["windows"]}
        if args.compare:
            report = compare_runs(run, validate_export(args.compare), kind=args.kind)
    except (ValueError, OSError, KeyError, TypeError, IndexError, csv.Error) as exc:
        report = {"status": "INVALID_OR_MISSING_EVIDENCE", "reason": str(exc)}
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    return 0 if report["status"].startswith("VALIDATED_") else 1


if __name__ == "__main__":
    raise SystemExit(main())

