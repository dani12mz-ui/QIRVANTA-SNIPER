"""Join native WSB and QIRVANTA journals without inventing missing events."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import io
from itertools import combinations
import json
from math import isclose
from pathlib import Path

from gold_pro.rc1_qa import EvidenceError, _json_dict, read_export, require
from gold_pro.replay import Costs
from gold_wick.engine import scan


SYMBOL = "OANDA:XAUUSD"
LANES = ("original", "s5", "rc2-indicator", "rc2-audit", "wick")
RC_LANES = ("s5", "rc2-indicator", "rc2-audit")
PRICE_FIELDS = ("open", "high", "low", "close", "volume")
START = "2026-11-11T05:00:00Z"
EVENT = "2026-11-11T09:30:00Z"
END = "2026-11-11T16:00:00Z"
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
WICK_LENGTH = {"WICK_CTX": 24, "WICK_EVT": 17, "WICK_PLAN": 19, "WICK_EXEC": 10}


def utc_ms(value, *, allow_offset=False):
    parsed = datetime.fromisoformat(value)
    offset = parsed.utcoffset()
    require(offset is not None and (allow_offset or offset == timedelta(0)),
            f"Expected explicit {'offset' if allow_offset else 'UTC'} timestamp: {value}")
    return (parsed - EPOCH) // timedelta(milliseconds=1)


def utc(stamp):
    return (EPOCH + timedelta(milliseconds=stamp)).isoformat(timespec="seconds").replace("+00:00", "Z")


def read_rows(path):
    raw = path.read_bytes()
    source = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
    require(source.fieldnames == ["Date", "Message"], f"{path.name}: expected Pine Date,Message export")
    rows = defaultdict(list)
    for row in source:
        require(row["Date"] and row["Message"] is not None and None not in row, f"{path.name}: malformed CSV row")
        for line in row["Message"].splitlines():
            tag, sep, payload = line.partition("|")
            if tag.startswith(("WSB_", "WICK_", "RC1_", "RC2_")):
                require(sep and payload, f"{path.name}: missing {tag} payload")
                rows[tag].append((utc_ms(row["Date"], allow_offset=True), payload))
    return rows, sha256(raw).hexdigest()


def context(rows, frame, lane):
    tag, index_key = ("WSB_ORIGINAL_CTX", "index") if lane == "original" else ("WSB_FORENSIC", "bar_index")
    result = {}
    prior = None
    for date, payload in rows[tag]:
        bar = _json_dict(payload)
        if lane != "original":
            require(bar.get("schema") == 1 and bar.get("kind") == lane, f"{lane}: unexpected probe identity")
        require(bar["symbol"] == SYMBOL and str(bar["timeframe"]) == str(frame), f"{lane}: unexpected instrument/frame")
        opened, closed, index, first = (bar[key] for key in ("open_ms", "close_ms", index_key, "first_open_ms"))
        require(all(type(value) is int for value in (opened, closed, index, first)), f"{lane}: invalid timestamps")
        require(date == opened and 0 < first <= opened < closed <= opened + frame * 60_000 and index >= 0,
                f"{lane}: invalid bar availability")
        require(closed not in result, f"{lane}: duplicate context")
        if prior:
            require(prior["first_open_ms"] == first and prior[index_key] + 1 == index and prior["close_ms"] <= opened,
                    f"{lane}: missing, repeated or unordered native bars")
        o, h, low, c, vol = (bar[key] for key in PRICE_FIELDS)
        require(all(type(price) in (int, Decimal) for price in (o, h, low, c))
                and 0 < low <= min(o, c) <= max(o, c) <= h
                and (vol is None or type(vol) in (int, Decimal) and vol >= 0), f"{lane}: invalid OHLCV")
        for name in ("h1_close_ms", "h4_close_ms", "wick_h1_close_ms", "wick_h4_close_ms"):
            stamp = bar.get(name)
            require(stamp is None or type(stamp) is int and 0 < stamp <= opened, f"{lane}: future {name}")
        result[closed] = bar
        prior = bar
    require(result, f"{lane}: {tag} unavailable")
    return result


def assert_event_context(bar, date, event, lane):
    require(bar and bar["open_ms"] == date, f"{lane}: event is outside exported bar context")
    for key in ("open_ms", "close_ms", "index"):
        if key in event:
            require(event[key] == bar[key], f"{lane}: event {key} differs from its bar")


def original_events(rows, bars):
    settings = [_json_dict(raw) for _, raw in rows["WSB_ORIGINAL_SETTINGS"]]
    require(len(settings) == 1 and settings[0]["version"] == 1, "original: expected one verified settings record")
    events = defaultdict(list)
    forms = {}
    for event_type in ("FORMATION", "RESOLUTION", "PLAN"):
        for date, raw in rows[f"WSB_ORIGINAL_{event_type}"]:
            event = _json_dict(raw)
            when = event["close_ms"]
            bar = bars.get(when)
            assert_event_context(bar, date, event, "original")
            if event_type == "FORMATION":
                require(0 <= event["left_index"] <= event["index"] and event["bottom"] <= event["top"],
                        "original: invalid formation geometry")
                require(event["index"] not in forms, "original: duplicate formation index")
                forms[event["index"]] = event
            if event_type == "RESOLUTION":
                born = forms.get(event["formed_index"])
                require(born and born["close_ms"] <= when and born["top"] == event["top"]
                        and born["bottom"] == event["bottom"], "original: unresolved formation provenance")
                event["formation_known_ms"] = born["close_ms"]
            if event_type == "PLAN":
                direction = event["direction"]
                require(direction in ("BUY", "SELL") and event["entry"] == bar["close"]
                        and bar["bear_signal" if direction == "SELL" else "bull_signal"],
                        "original: plotted plan differs from bar direction or close")
            events[when].append({"stream": "WSB_ORIGINAL", "state": event_type, **event})
    for _, end in rows["WSB_ORIGINAL_END"]:
        require(int(end) == next(reversed(bars)), "original: cutoff does not match last exported close")
    return events, {"settings": settings[0], "end_marker_present": bool(rows["WSB_ORIGINAL_END"])}


def rc_events(path, rows, bars, lane, frame):
    export = read_export(path, frame, "indicator" if lane == "rc2-indicator" else "audit")
    require(len(export.context) == len(bars), f"{lane}: forensic and compatibility coverage differ")
    for ctx in export.context:
        bar = bars.get(ctx.closing)
        require(bar and (ctx.opening, ctx.index, ctx.first_open, ctx.ohlc, ctx.htf_closes) == (
            bar["open_ms"], bar["bar_index"], bar["first_open_ms"],
            tuple(bar[key] for key in PRICE_FIELDS[:4]), (bar["h1_close_ms"], bar["h4_close_ms"])),
            f"{lane}: forensic and compatibility contexts differ")
    events = defaultdict(list)
    for name, stream, time_key in (("CORE", export.core, "known_time"),
                                   ("READY", export.ready, "confirmed_ms"),
                                   ("NATIVE", export.native, "known_time")):
        for event in stream:
            direction = event.get("direction") or ("SELL" if event["side"] == -1 else "BUY")
            state = event.get("event") or event["state"]
            events[event[time_key]].append({"stream": name, **event, "state": state, "direction": direction})
    tag = "RC2_ALERT" if lane == "rc2-indicator" else "RC2_NATIVE_ALERT"
    for date, raw in rows[tag]:
        batch = _json_dict(raw)
        for event in batch["events"]:
            when = event.get("confirmed_ms") or event.get("known_ms")
            assert_event_context(bars.get(when), date, {}, lane)
            events[when].append({"stream": tag, **event})
    return events, {"end_marker_present": export.meta["end_marker_present"]}


def nullable(value, parse):
    return None if value == "NA" else parse(value)


def wick_events(rows, bars, frame):
    events = defaultdict(list)
    confirmed = set()
    for tag, expected in WICK_LENGTH.items():
        for date, raw in rows[tag]:
            fields = raw.split("|")
            require(len(fields) == expected - 1 and fields[0] == "1", f"wick: malformed {tag}")
            when = int(fields[4] if tag == "WICK_CTX" else fields[1])
            bar = bars.get(when)
            assert_event_context(bar, date, {}, "wick")
            if tag == "WICK_CTX":
                require(fields[1:4] == [SYMBOL, str(frame), str(date)] and when not in confirmed
                        and tuple(map(Decimal, fields[5:9])) == tuple(bar[key] for key in PRICE_FIELDS[:4])
                        and nullable(fields[10], Decimal) == bar["volume"],
                        "wick: native context differs from forensic context")
                confirmed.add(when)
                continue
            if tag == "WICK_EVT":
                event = {"state": fields[2], "formation_id": fields[3], "side": int(fields[4]),
                         "formation_known_ms": nullable(fields[5], int),
                         "top": nullable(fields[6], Decimal), "bottom": nullable(fields[7], Decimal),
                         "reason": fields[14]}
                require(event["formation_known_ms"] is None or event["formation_known_ms"] <= when,
                        "wick: future formation timestamp")
            elif tag == "WICK_PLAN":
                selected = fields[17]
                state = {"OK": "ENTRY_VALID", "POLICY_NOT_SELECTED": "POLICY_NOT_SELECTED"}.get(
                    selected, "RISK_REJECTED")
                event = {"state": state,
                         "plan_id": fields[2], "model": fields[3], "side": int(fields[4]),
                         **dict(zip(("entry", "sl", "tp1", "tp2", "tp3"),
                                    (nullable(value, Decimal) for value in fields[5:10]))),
                         "reason": selected, "geometry_reason": fields[16]}
            else:
                event = {"state": fields[2], "plan_id": fields[3],
                         "effective_time": nullable(fields[4], int), "price": nullable(fields[5], Decimal),
                         "quantity": nullable(fields[6], Decimal), "reason": fields[7],
                         "net_cash": nullable(fields[8], Decimal)}
                require(event["effective_time"] is None or event["effective_time"] <= when,
                        "wick: execution known before its effective timestamp")
            events[when].append({"stream": tag, **event})
    require(confirmed == bars.keys(), "wick: native and forensic coverage differ")
    return events, {"simulation_enabled": next(iter(bars.values()))["wick_sim_enabled"]}


def read_lane(path, lane, frame):
    rows, digest = read_rows(path)
    bars = context(rows, frame, lane)
    if lane == "original":
        events, details = original_events(rows, bars)
    elif lane == "wick":
        events, details = wick_events(rows, bars, frame)
    else:
        events, details = rc_events(path, rows, bars, lane, frame)
    first = next(iter(bars.values()))
    meta = {"status": "PARSED", "file": path.name, "sha256": digest, "bars": len(bars),
            "first_loaded_open_ms": first["first_open_ms"], "first_exported_close_ms": next(iter(bars)),
            "last_exported_close_ms": next(reversed(bars)), **details}
    return {"bars": bars, "events": events, "meta": meta}


def compare_lanes(left, right, window, exact_context=False):
    shared = sorted(set(window) & left["bars"].keys() & right["bars"].keys())
    fields = ("open_ms", "close_ms", *PRICE_FIELDS)
    differences = {}
    if exact_context:
        fields += ("h1_close_ms", "h4_close_ms", "h1_bias", "h4_bias", "warmup_pass")
    for when in shared:
        a, b = left["bars"][when], right["bars"][when]
        diff = {field: [a.get(field), b.get(field)] for field in fields if a.get(field) != b.get(field)}
        if diff:
            differences[utc(when)] = diff
    starts_match = left["meta"]["first_loaded_open_ms"] == right["meta"]["first_loaded_open_ms"]
    common = {"shared_bars": len(shared), "complete_window": len(shared) == len(window),
              "same_loaded_start": starts_match, "differences": differences}
    return {"status": "MATCH" if shared and not differences else "MISMATCH" if differences else "NO_OVERLAP",
            **common}


def compare_wick_model(original, wick, window, frame):
    if original is None or wick is None:
        return {"status": "MISSING_EVIDENCE"}
    tape_check = compare_lanes(original, wick, window)
    if tape_check["status"] != "MATCH" or not tape_check["complete_window"]:
        return {"status": "INCOMPLETE_OR_DIFFERENT_TAPE", "comparison": tape_check}
    first = wick["meta"]["first_loaded_open_ms"]
    tape = [bar for bar in original["bars"].values() if first <= bar["open_ms"]]
    if not tape or tape[0]["open_ms"] != first or tape[-1]["close_ms"] < window[-1]:
        return {"status": "MISSING_WARMUP_TAPE", "wick_first_loaded_open_ms": first}
    market = [{**{key: None if bar[key] is None else float(bar[key]) for key in PRICE_FIELDS},
               "time": bar["open_ms"], "time_close": bar["close_ms"]} for bar in tape]
    sample = next(iter(wick["bars"].values()))
    costs = Costs(tick=float(sample["mintick"]), pointvalue=float(sample["point_value"]))
    result = scan(market, costs=costs, minutes=frame)
    start, end = window[0], window[-1]
    native = [dict(event, time_ms=when) for when in window for event in wick["events"].get(when, ())]
    local = [event for event in result.events if start <= event["time_ms"] <= end]

    def key(event):
        form_id = event["formation_id"]
        first_known = event.get("formation_known_ms", event.get("first_known_ms"))
        top, bottom = (None if event[bound] is None else Decimal(str(event[bound])) for bound in ("top", "bottom"))
        return (event["time_ms"], event["state"], event["side"], event["reason"],
                form_id.replace(":", "|") if form_id not in (None, "NA") else None, first_known, top, bottom)

    native_sequence = [key(event) for event in native if event["stream"] == "WICK_EVT"]
    local_sequence = [key(event) for event in local]
    native_events, local_events = Counter(native_sequence), Counter(local_sequence)
    local_plans = {event["plan_id"]: event for event in local if event["plan_id"] is not None}
    native_plans = [event for event in native if event["stream"] == "WICK_PLAN"]
    differences = []
    for event in native_plans:
        match = local_plans.get(event["plan_id"].replace(":", "|"))
        if match is None:
            differences.append({"plan_id": event["plan_id"], "reason": "MISSING_LOCAL_PLAN"})
            continue
        reason = match["reason"] if match["state"] == "RISK_REJECTED" else "OK"
        if event["geometry_reason"] != reason:
            differences.append({"plan_id": event["plan_id"], "reason": "GEOMETRY_REASON_MISMATCH"})
        expected = (match["reference"], match["stop"], *(match["targets"] or (None,) * 3))
        for level, value in zip(("entry", "sl", "tp1", "tp2", "tp3"), expected):
            actual = event[level]
            if not (actual is value or actual is not None and value is not None
                    and isclose(float(actual), value, rel_tol=0, abs_tol=1e-8)):
                differences.append({"plan_id": event["plan_id"], "reason": "LEVEL_MISMATCH", "level": level})
    native_plan_ids = {event["plan_id"].replace(":", "|") for event in native_plans}
    equal = (native_sequence == local_sequence and native_plan_ids == local_plans.keys()
             and len(native_plans) == len(local_plans) and not differences)
    return {"status": "MATCH" if equal else "MISMATCH",
            "warmup_bars": sum(bar["close_ms"] < start for bar in tape),
            "native_lifecycle_events": sum(native_events.values()), "python_lifecycle_events": len(local),
            "native_risk_offers": len(native_plans), "python_risk_offers": len(local_plans),
            "native_only": list((native_events - local_events).elements()),
            "python_only": list((local_events - native_events).elements()), "risk_differences": differences}


def compare_original_repeat(directory, original, frame, window):
    path = directory / f"original-repeat_M{frame}.csv"
    if original is None or not path.is_file():
        return None
    try:
        repeat = read_lane(path, "original", frame)
    except (ValueError, csv.Error, UnicodeError, KeyError) as exc:
        return {"status": "INVALID_EXPORT", "file": path.name, "reason": str(exc)}
    result = compare_lanes(original, repeat, window)
    common = sorted(set(window) & original["bars"].keys() & repeat["bars"].keys())
    result["event_differences_utc"] = [
        utc(when) for when in common if original["events"].get(when, ()) != repeat["events"].get(when, ())
    ]
    result["same_settings"] = original["meta"]["settings"] == repeat["meta"]["settings"]
    if result["event_differences_utc"] or not result["same_settings"]:
        result["status"] = "MISMATCH"
    result["repeat"] = repeat["meta"]
    return result


def lane_window(run, window, signal_time):
    events = [{**event, "known_utc": utc(when), "known_ms": when}
              for when in window for event in run["events"].get(when, ())]
    counts = Counter(f"{event['stream']}|{event['state']}" for event in events)
    first_events = {}
    for event in events:
        direction = event.get("direction") or {-1: "SELL", 1: "BUY"}.get(event.get("side"))
        if direction not in ("BUY", "SELL"):
            continue
        key = f"{direction}|{event['stream']}|{event['state']}"
        first_events.setdefault(key, event)
    return {"meta": run["meta"], "at_signal_bar": run["bars"].get(signal_time),
            "at_signal_events": run["events"].get(signal_time, []),
            "counts": dict(counts), "first_directional_events": first_events, "events": events}


def original_signal(run, when):
    events = run["events"].get(when, [])
    resolutions = [event for event in events if event["state"] == "RESOLUTION"
                   and event["direction"] == "SELL" and event["reason"] == "SIGNAL"]
    formations = [
        form for resolution in resolutions for form in run["events"].get(resolution["formation_known_ms"], ())
        if form["state"] == "FORMATION" and form["index"] == resolution["formed_index"]
    ]
    return {"plans": [event for event in events if event["state"] == "PLAN" and event["direction"] == "SELL"],
            "resolutions": resolutions, "formations": formations}


def event_identity(events):
    return [event for event in events if event["stream"] in ("CORE", "READY")]


def frame_report(directory, frame, start, end, signal_time):
    runs, lanes = {}, {}
    window = tuple(range(start, end + 1, frame * 60_000))
    for lane in LANES:
        path = directory / f"{lane}_M{frame}.csv"
        if not path.is_file():
            lanes[lane] = {"meta": {"status": "MISSING_EVIDENCE", "file": path.name}}
            continue
        try:
            run = read_lane(path, lane, frame)
        except (EvidenceError, csv.Error, UnicodeError, KeyError, ValueError) as exc:
            lanes[lane] = {"meta": {"status": "INVALID_EXPORT", "file": path.name, "reason": str(exc)}}
            continue
        runs[lane] = run
        lanes[lane] = lane_window(run, window, signal_time)
    compared = {}
    for left, right in combinations(runs, 2):
        rc_pair = left in RC_LANES and right in RC_LANES
        result = compare_lanes(runs[left], runs[right], window, exact_context=rc_pair)
        if rc_pair:
            common = set(window) & runs[left]["bars"].keys() & runs[right]["bars"].keys()
            result["core_ready_difference_utc"] = [
                utc(when) for when in sorted(common) if event_identity(runs[left]["events"].get(when, ())) !=
                event_identity(runs[right]["events"].get(when, ()))
            ]
        compared[f"{left} vs {right}"] = result
    return {"lanes": lanes, "comparison": compared,
            "original_at_signal": original_signal(runs["original"], signal_time) if "original" in runs else None,
            "original_repeat": compare_original_repeat(directory, runs.get("original"), frame, window),
            "wick_python_parity": compare_wick_model(runs.get("original"), runs.get("wick"), window, frame),
            "expected_bars": len(window)}, runs


def describe(event):
    state = event["state"]
    side = event.get("direction") or {-1: "SELL", 1: "BUY"}.get(event.get("side"), "")
    reason = event.get("reason", "")
    return f"{state} {side}".rstrip() + (f" [{reason}]" if reason else "")


def describe_lane(run, when, default_state):
    if run is None or when not in run["bars"]:
        return ""
    bar = run["bars"][when]
    items = run["events"].get(when, ())
    # RC2 payload timestamps are already present in the structured event journal.
    visible = [describe(item) for item in items if "ALERT" not in item["stream"]]
    if not visible and bar.get(default_state):
        visible.append(str(bar[default_state]))
    return " | ".join(visible)


def write_timeline(path, runs, frame, start, end):
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("UTC_CLOSE", "WSB", "S5", "RC2", "QIRVANTA_WICK", "PRICE", "REASON", "RC2_AUDIT"))
        for when in range(start, end + 1, frame * 60_000):
            actual = next((run["bars"][when] for run in runs.values() if when in run["bars"]), {})
            s5 = runs.get("s5", {}).get("bars", {}).get(when, {})
            rc2 = runs.get("rc2-indicator", {}).get("bars", {}).get(when, {})
            writer.writerow((
                utc(when), describe_lane(runs.get("original"), when, "bear_signal"),
                describe_lane(runs.get("s5"), when, "sell_reason"),
                describe_lane(runs.get("rc2-indicator"), when, "decision_state_at_close"),
                describe_lane(runs.get("wick"), when, "wick_state"), actual.get("close", ""),
                " | ".join(filter(None, (s5.get("sell_reason"), rc2.get("decision_reason")))),
                describe_lane(runs.get("rc2-audit"), when, "native_last_status"),
            ))


def build_report(directory, start, end, signal_time, frames=(5, 15), csv_dir=None):
    require(start <= signal_time <= end and start < end, "Signal must fall inside the comparison window")
    output = {"schema": 1, "symbol": SYMBOL, "start_utc": utc(start), "end_utc": utc(end),
              "signal_utc": utc(signal_time), "frames": {}}
    for frame in frames:
        require(all(value % (frame * 60_000) == 0 for value in (start, end, signal_time)),
                f"M{frame}: window and signal must align to closed chart bars")
        report, runs = frame_report(directory, frame, start, end, signal_time)
        output["frames"][f"M{frame}"] = report
        if csv_dir is not None:
            csv_dir.mkdir(parents=True, exist_ok=True)
            write_timeline(csv_dir / f"timeline_M{frame}.csv", runs, frame, start, end)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--csv-dir", type=Path)
    parser.add_argument("--start", default=START)
    parser.add_argument("--end", default=END)
    parser.add_argument("--event", default=EVENT)
    parser.add_argument("--frames", type=int, choices=(5, 15, 30), nargs="+", default=(5, 15))
    args = parser.parse_args()
    report = build_report(args.logs_dir, utc_ms(args.start), utc_ms(args.end), utc_ms(args.event),
                          args.frames, args.csv_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes((json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n").encode("utf-8"))
    for frame, values in report["frames"].items():
        print(frame, {lane: data["meta"]["status"] for lane, data in values["lanes"].items()})
    invalid = any(data["meta"]["status"] == "INVALID_EXPORT"
                  for values in report["frames"].values() for data in values["lanes"].values())
    invalid_repeat = any((values["original_repeat"] or {}).get("status") == "INVALID_EXPORT"
                         for values in report["frames"].values())
    return 2 if invalid or invalid_repeat else 0


if __name__ == "__main__":
    raise SystemExit(main())
