"""Verify TradingView RC2 exports and exact shared-window parity with pinned RC1/S5."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import zip_longest
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from gold_pro import rc1_qa as rc1
from gold_pro.rc1_qa import EvidenceError, require
from gold_pro.rc2_research import native_properties


BASELINE_ARCHIVE = rc1.ROOT / rc1.RELEASE / "evidence" / "native-logs.zip"
BASELINE_ARCHIVE_SHA = "b9119809625326e5e9111f8b9adb77d30908981f2d901b8ddac16833020cee97"
RC1_SOURCES = {
    "QIRVANTA_GOLD_PRO_RC1.pine": "e51308df4be72827dfdf4dc875a3dbea5b679a6bb859fe179909ba24c85e975f",
    "QIRVANTA_GOLD_PRO_RC1_AUDIT.pine": "dfb154e407d32cbfc90ac957788b54cfa5b4e2034421b4b3ac26abf039d59278",
}
MODES = {"parity": "S5 RAW Parity", "fixed": "RC2 Fixed SL",
         "be": "Experimental BE", "lock": "Experimental +0.25R"}
ROLES = ("indicator", *MODES, "research")
TIME_FIELDS = {"core": "known_time", "ready": "confirmed_ms", "native": "known_time", "alerts": "confirmed_ms"}
INDICATOR_SCHEMA = "qirvanta.gold-pro.rc2.v1"
NATIVE_SCHEMA = "qirvanta.gold-pro.rc2.audit.v1"
TARGETS = {"TP1", "TP2", "TP3"}
JSON_TAGS = {"RC2_ALERT", "RC2_NATIVE_ALERT", "RC2_NATIVE_PART", "RC2_AUDIT_SUMMARY", "RC2_NATIVE_ANOMALY"}


def extras(path):
    journal = defaultdict(list)
    with Path(path).open(encoding="utf-8-sig", newline="") as raw:
        for row in csv.DictReader(raw, strict=True):
            for line in row["Message"].splitlines():
                tag, separator, payload = line.partition("|")
                if not tag.startswith("RC2_"):
                    continue
                require(separator and payload, f"{tag}: missing payload")
                if tag in JSON_TAGS:
                    payload = rc1._json_dict(payload)
                else:
                    require(tag in {"RC2_EARLY", "RC2_FILTER"}, f"unrecognized RC2 tag {tag}")
                journal[tag].append(payload)
    return journal


def shared(left, right, streams):
    """Remove only the run-local first bar and index origin, never missing interior bars."""
    if left is None or right is None:
        return {"status": "MISSING_EVIDENCE"}
    start = max(left.context[0].opening, right.context[0].opening)
    end = min(left.context[-1].closing, right.context[-1].closing)
    runs = (left, right)
    bars = tuple([bar for bar in run.context if start <= bar.opening and bar.closing <= end] for run in runs)
    if not all(bars):
        return {"status": "NO_SHARED_WINDOW"}
    evidence = {
        "shared_first_open_ms": start, "shared_last_close_ms": end,
        "left_chart_first_open_ms": left.context[0].first_open,
        "right_chart_first_open_ms": right.context[0].first_open,
        "left_minus_right_bar_index": bars[0][0].index - bars[1][0].index,
        "loaded_warmup_origins_differ": left.context[0].first_open != right.context[0].first_open,
        "left_context_outside_overlap": len(left.context) - len(bars[0]),
        "right_context_outside_overlap": len(right.context) - len(bars[1]),
        "shared_gap_count": sum(b.opening > a.closing for a, b in zip(bars[0], bars[0][1:])),
    }
    comparison = {
        "context": tuple([(bar.index - run[0].index, *bar.canonical()[2:]) for bar in run] for run in bars),
    }
    for stream in streams:
        clock = TIME_FIELDS[stream]
        comparison[stream] = tuple(
            [row for row in getattr(run, stream) if start < row[clock] <= end] for run in runs
        )
    for stream, (a, b) in comparison.items():
        for index, (x, y) in enumerate(zip_longest(a, b), 1):
            if x != y:
                return {"status": "MISMATCH", "stream": stream, "row": index, "left": x, "right": y, **evidence}
    return {
        "status": "MATCH" if all(run.meta["end_marker_present"] for run in runs) else "MATCHED_EXCERPT",
        "rows": {name: len(pair[0]) for name, pair in comparison.items()}, **evidence,
    }


def fresh_comparison(left, right, streams):
    """Expose secondary probe context and observation discrepancies without treating them as parity."""
    strict = shared(left, right, streams)
    if strict["status"] != "MISMATCH":
        return strict
    start, end = strict["shared_first_open_ms"], strict["shared_last_close_ms"]
    a, b = ([v for v in run.context if start <= v.opening and v.closing <= end] for run in (left, right))
    if len(a) != len(b) or any(x.canonical()[2:8] != y.canonical()[2:8] for x, y in zip(a, b)):
        return strict
    htf = [(i, x.htf_closes, y.htf_closes) for i, (x, y) in enumerate(zip(a, b)) if x.htf_closes != y.htf_closes]
    full = next((i for i, (x, y) in enumerate(zip(a, b)) if None not in x.htf_closes + y.htf_closes), None)
    if htf and (full is None or any(i >= full or any(l != r and None not in (l, r) for l, r in zip(x, y))
                                    for i, x, y in htf)):
        return strict

    rows = {}
    core_observations = {}
    for stream in streams:
        time = TIME_FIELDS[stream]
        x, y = ([e for e in getattr(run, stream) if start < e[time] <= end] for run in (left, right))
        rows[stream] = {"left": len(x), "right": len(y), "equal": x == y}
        if x == y:
            continue
        if stream != "core":
            return strict
        by_left, by_right = ({row["event_id"]: row for row in side} for side in (x, y))
        common = by_left.keys() & by_right.keys()
        if [e for e in x if e["event_id"] in common] != [e for e in y if e["event_id"] in common]:
            return strict
        for name, own, other in (("left_only", x, by_right), ("right_only", y, by_left)):
            difference = [e for e in own if e["event_id"] not in other]
            if any(e["event"] not in {"WATCH", "EXPIRED", "CANCELLED"} for e in difference):
                return strict
            core_observations[name] = [{key: e[key] for key in ("event_id", "event", "side", "known_time", "price")}
                                       for e in difference]
    return {
        "status": "FRESH_PROBE_DISCREPANCY",
        "strict": strict,
        "equal_ohlc_and_gap_sequence": True,
        "htf_left_edge_availability_differences": len(htf),
        "first_complete_htf_bar_open_ms": a[full].opening if full is not None else None,
        "event_streams": rows,
        "unmatched_observations": core_observations,
    }


def utc(stamp):
    value = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=stamp)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def batches(records, schema, timestamp):
    seen = set()
    latest = 0
    for record in records:
        events = record.get("events")
        require(record.get("schema") == schema and isinstance(events, list) and events, f"{schema}: invalid batch")
        require(all(isinstance(event, dict) for event in events), f"{schema}: invalid event")
        known = rc1.epoch(events[0].get(timestamp), timestamp)
        require(known > latest and all(event.get(timestamp) == known for event in events),
                f"{schema}: repeated, unordered or mixed-bar batch")
        latest = known
        for event in events:
            key = rc1.nonempty(event.get("event_id"), "event ID")
            require(key not in seen, f"{schema}: duplicate event {key}")
            seen.add(key)
            yield event


def early_events(rows, run, timeframe):
    events = {}
    indices = {bar.closing: i for i, bar in enumerate(run.context)}
    for row in rows:
        fields = row.split("|")
        require(len(fields) == 6 and fields[:3] == ["1", rc1.SYMBOL, str(timeframe)], "RC2_EARLY: bad context")
        known, direction, price = rc1.pipe_int(fields[3]), fields[4], rc1.number(fields[5], "early price")
        index = indices.get(known)
        require(index is not None and direction in {"BUY", "SELL"} and known not in events,
                "RC2_EARLY: invalid or duplicate observation")
        bar = run.context[index]
        require(price == bar.ohlc[3] and (price - bar.ohlc[0]) * (1 if direction == "BUY" else -1) > 0,
                "RC2_EARLY: close price or candle direction differs")
        require(index == 0 or bar.opening == run.context[index - 1].closing,
                "RC2_EARLY: observation after a missing interval")
        if index >= 12:
            prior = run.context[index - 12:index]
            breaks = price > max(b.ohlc[1] for b in prior) if direction == "BUY" else price < min(b.ohlc[2] for b in prior)
            require(breaks, "RC2_EARLY: no causal break of prior 12 bars")
        events[known] = direction
    return events


def filter_events(rows, run):
    by_id = {e["event_id"]: e for e in run.core}
    context = {bar.closing: bar for bar in run.context}
    ready = {(by_id[e["source_id"]]["plan_id"], e["confirmed_ms"]): e for e in run.ready}
    states = {}
    for row in rows:
        version, divider, tail = row.partition("|")
        fields = tail.rsplit("|", 3)
        require(version == "1" and divider and len(fields) == 4, "RC2_FILTER: invalid record")
        plan_id, stamp, decision, raw_ratio = fields
        key = plan_id, rc1.pipe_int(stamp)
        ratio = rc1.number(raw_ratio, "filter ratio")
        require(key in ready and key not in states and decision in {"PASS", "REJECTED"} and ratio > 0,
                "RC2_FILTER: invalid or duplicate READY reference")
        bar = context[key[1]]
        atr = rc1.json_number(by_id[ready[key]["source_id"]]["atr_at_signal"], "filter ATR")
        require(atr > 0 and abs(max(bar.ohlc[1] - bar.ohlc[2], Decimal("0.001")) / atr - ratio)
                <= Decimal("0.000001"), "RC2_FILTER: ratio differs from confirmed candle range and S5 ATR")
        cutoff = Decimal("1.75")
        require(abs(ratio - cutoff) <= Decimal("0.00000001") or (ratio > cutoff) == (decision == "REJECTED"),
                "RC2_FILTER: decision differs from declared threshold")
        states[key] = decision
    require(not states or states.keys() == ready.keys(), "RC2_FILTER: incomplete coverage of raw READY plans")
    return {ready[key]["event_id"] for key, decision in states.items() if decision == "REJECTED"}


def indicator_checks(run, journal, timeframe, research=False):
    require(not any(journal[tag] for tag in JSON_TAGS - {"RC2_ALERT"}), "indicator contains native audit events")
    early = early_events(journal["RC2_EARLY"], run, timeframe)
    rejected = filter_events(journal["RC2_FILTER"], run)
    require(research or not (early or journal["RC2_FILTER"]), "default indicator enabled a research option")
    core = {event["event_id"]: event for event in run.core}
    plans = {event["event_id"]: event for event in run.ready}
    default, early_alerts = [], []
    for event in batches(journal["RC2_ALERT"], INDICATOR_SCHEMA, "confirmed_ms"):
        if event.get("model") == "RC2_EARLY":
            known = event["confirmed_ms"]
            direction = early.get(known)
            source = f"{rc1.SYMBOL}|{timeframe}|RC2_EARLY|{direction}|{known}"
            require(research and direction and rc1.PAYLOAD_FIELDS <= event.keys() and event["symbol"] == rc1.SYMBOL
                    and event["timeframe"] == str(timeframe) and event["source_id"] == source
                    and event["event_id"] == f"{source}|WATCH" and event["state"] == "WATCH"
                    and event["direction"] == direction and event["confirmation_utc"] == utc(known)
                    and event["provisional"] is True and all(event[key] is None for key in rc1.PLAN_FIELDS),
                    "RC2_EARLY: WATCH payload differs from its confirmed observation")
            rc1.nonempty(event["reason"], "early alert reason")
            early_alerts.append(event)
        else:
            rc1._validate_payload(event, core, timeframe)
            if event["state"] == "READY":
                require(event == plans.get(event["event_id"]), "RC2_ALERT: READY differs from the raw plan")
            default.append(event)
    if not research or run.alerts:
        require(tuple(default) == run.alerts, "RC2_ALERT: S5 payloads differ from RC1 compatibility rows")
    expected = {f"{row['event_id']}|RC1|{rc1.ALERT_STATES[row['event']]}" for row in run.core
                if row["event"] in rc1.ALERT_STATES} | (plans.keys() - rejected)
    require({row["event_id"] for row in default} == expected, "RC2_ALERT: S5 payload coverage differs")
    require(not early_alerts or len(early_alerts) == len(early), "RC2_EARLY: incomplete opt-in WATCH alert coverage")
    return {"status": "VERIFIED" if default or early_alerts else "NO_EXAMPLES",
            "s5_alerts": len(default), "early_observations": len(early),
            "early_alerts": len(early_alerts), "filter_rejected": len(rejected)}


def native_expected(run, parts, mode):
    """Read the emulator's observed order, preserving a stop's old value until its update."""
    active = None
    entered = set()
    for source in run.native:
        kind, plan_id = source["event"], source["plan_id"]
        price, initial = source["price"], source["initial_sl"]
        state, leg = "", None
        if kind == "NATIVE_ENTRY":
            require(active is None and plan_id not in entered, "duplicate or overlapping native campaign entry")
            entered.add(plan_id)
            active = {"plan": plan_id, "entry": rc1.json_number(price, "native entry"),
                      "stop": rc1.json_number(initial, "initial SL"), "stopped": False,
                      "quantity": Decimal(0), "tp1": False}
            state = "ENTRY"
        elif kind.removeprefix("NATIVE_EXIT_").isdigit():
            require(active and active["plan"] == plan_id, "native fill without tracked entry")
            active["quantity"] += rc1.json_number(source["quantity_fraction"], "native partial quantity")
            note = source["reason"]
            order = parts[source["event_id"]]["exit_order_id"]
            if note == "SL" or note in TARGETS:
                require(order.startswith(plan_id + "|"), "exit order ID differs from campaign")
                leg = order.removeprefix(plan_id + "|")
                require(leg in {"T1", "T2", "T3"}, "unknown bracket leg")
                if note in TARGETS:
                    require(not active["stopped"] and leg == "T" + note[-1], "TP after SL or wrong bracket leg")
                active["stopped"] |= note == "SL"
                active["tp1"] |= note == "TP1"
                state = note
        elif kind == "NATIVE_STOP_SCHEDULED":
            require(active and active["plan"] == plan_id and active["tp1"]
                    and source["effective_time"] is None and mode != MODES["fixed"],
                    "unexpected or retroactive scheduled stop")
            price = rc1.json_number(price, "scheduled stop")
            initial = rc1.json_number(initial, "initial SL")
            side = source["side"]
            require((price - active["stop"]) * side > 0, "scheduled stop did not tighten")
            fraction = {MODES["parity"]: Decimal("0.05"), MODES["be"]: Decimal(0),
                        MODES["lock"]: Decimal("0.25")}[mode]
            target = active["entry"] + (active["entry"] - initial) * fraction
            require(abs(price - target) <= Decimal("0.00100001"), "scheduled stop differs from audit policy")
            active["stop"] = price
            state = "BE" if mode == MODES["be"] else "PROFIT_LOCK"
        elif kind == "NATIVE_CLOSED":
            require(active and active["plan"] == plan_id
                    and abs(active["quantity"] - 1) <= Decimal("0.000001")
                    and source["risk_basis"] != "INCOMPLETE_TRADE_LOG", "incomplete native campaign closure")
            state = "EXIT"
        if state:
            yield source, state, leg, active["entry"], active["stop"]
        if state == "EXIT":
            active = None


def native_checks(path, run, journal, timeframe, mode):
    require(not any(journal[tag] for tag in ("RC2_ALERT", "RC2_EARLY", "RC2_FILTER")), "audit contains indicator research rows")
    require(not journal["RC2_NATIVE_ANOMALY"], "native strategy emitted a lifecycle anomaly")
    properties = native_properties(path, run, timeframe)
    if properties["status"] == "NO_RC2_STRATEGY_PROPERTY_EXPORT":
        return {"status": "MISSING_NATIVE_PROPERTIES"}
    require(properties["status"] == "RECONCILED" and properties["native_strategy_properties"]["mode"] == mode,
            "audit summary does not match the requested stop policy")
    parts = {record["source_id"]: record for record in journal["RC2_NATIVE_PART"]}
    expected = native_expected(run, parts, mode)
    observed = batches(journal["RC2_NATIVE_ALERT"], NATIVE_SCHEMA, "known_ms")
    states = Counter()
    for item, event in zip_longest(expected, observed):
        require(item is not None and event is not None, "native alert batches omit or add an execution event")
        source, state, leg, entry, stop = item
        plan = source["plan_id"]
        known = source["known_time"]
        scheduled = state in {"BE", "PROFIT_LOCK"}
        at = rc1.epoch(known if scheduled else source["effective_time"], "native alert time")
        key = f"{plan}|RC2|{mode}|{state}" + (f"|{leg}" if leg else "")
        expected_fields = {
            "event_id": key, "source_id": source["event_id"], "plan_id": plan,
            "symbol": rc1.SYMBOL, "timeframe": str(timeframe), "mode": mode,
            "direction": "BUY" if source["side"] == 1 else "SELL", "state": state, "leg": leg,
            "event_ms": at, "event_utc": utc(at), "known_ms": known, "known_utc": utc(known),
            "entry": entry, "entry_reference": source["reference"], "sl": stop, "initial_sl": source["initial_sl"],
            "tp1": source["tp1"], "tp2": source["tp2"], "tp3": source["tp3"], "event_price": source["price"],
        }
        require(all(field in event and event[field] == value for field, value in expected_fields.items())
                and event.get("simulated") is True and event.get("applies_from_next_tick") is scheduled,
                f"native alert differs from actual ledger or stop policy: {key}")
        reason = rc1.nonempty(event.get("reason"), "native alert reason")
        require((scheduled and "next available tick" in reason) or (not scheduled and reason == source["reason"]),
                "native alert reason differs from source")
        states[state] += 1
    return {"status": "VERIFIED" if states else "NO_EXAMPLES", "states": dict(states), **{
        key: value for key, value in properties.items() if key != "status"
    }}


def verify_frame(native_dir, baseline_dir, timeframe):
    runs, result = {}, {}
    for role in ROLES:
        path = native_dir / f"{role}_M{timeframe}.csv"
        try:
            run = rc1.read_export(path, timeframe, "indicator" if role in {"indicator", "research"} else "audit")
            journal = extras(path)
            qa = native_checks(path, run, journal, timeframe, MODES[role]) if role in MODES else (
                indicator_checks(run, journal, timeframe, research=role == "research")
            )
            runs[role] = run
            result[role] = {"status": "PARSED", **run.meta, "rc2": qa}
        except FileNotFoundError:
            result[role] = {"status": "MISSING_EVIDENCE", "file": path.name}
        except (OSError, UnicodeError, csv.Error, ValueError, KeyError) as exc:
            result[role] = {"status": "INVALID_EXPORT", "file": path.name, "reason": str(exc)}
    baseline = {
        role: rc1.read_export(baseline_dir / f"{role}_M{timeframe}.csv", timeframe, role)
        for role in ("indicator", "audit", "reference")
    }
    comparisons = {}
    for role in ROLES:
        streams = ("core", "ready")
        if role == "parity":
            streams += ("native",)
        if role == "indicator":
            streams += ("alerts",)
        targets = ("indicator",) if role in {"indicator", "research"} else ("audit", "reference")
        for target in targets:
            compare = fresh_comparison if role == "research" else shared
            comparisons[f"{role}_vs_rc1_{target}"] = compare(runs.get(role), baseline[target], streams)
    for role in MODES:
        comparisons[f"indicator_vs_{role}"] = shared(runs.get("indicator"), runs.get(role), ("core", "ready"))
    comparisons["research_vs_indicator"] = fresh_comparison(
        runs.get("research"), runs.get("indicator"), ("core", "ready")
    )
    fresh = {}
    for name in ("rc1", "s5"):
        current_path = native_dir / f"{name}_M{timeframe}.csv"
        if not current_path.is_file():
            continue
        label = f"{name}_current"
        try:
            current = rc1.read_export(current_path, timeframe, "audit")
            fresh[name] = current
            result[label] = {"status": "PARSED", **current.meta}
            compare = fresh_comparison if name == "s5" else shared
            comparisons[f"indicator_vs_{label}"] = compare(runs.get("indicator"), current, ("core", "ready"))
            comparisons[f"parity_vs_{label}"] = compare(runs.get("parity"), current, ("core", "ready", "native"))
            comparisons[f"fixed_vs_{label}"] = compare(runs.get("fixed"), current, ("core", "ready"))
        except (OSError, UnicodeError, csv.Error, ValueError, KeyError) as exc:
            result[label] = {"status": "INVALID_EXPORT", "file": current_path.name, "reason": str(exc)}
    if {"rc1", "s5"} <= fresh.keys():
        comparisons["rc1_current_vs_s5_current"] = fresh_comparison(
            fresh["rc1"], fresh["s5"], ("core", "ready", "native")
        )
    aligned = {}
    for role in ("aligned_parity", "aligned_rc1", "aligned_indicator"):
        path = native_dir / f"{role}_M{timeframe}.csv"
        if not path.is_file():
            continue
        try:
            is_indicator = role == "aligned_indicator"
            run = rc1.read_export(path, timeframe, "indicator" if is_indicator else "audit")
            result[role] = {"status": "PARSED", **run.meta}
            if role == "aligned_parity":
                result[role]["rc2"] = native_checks(path, run, extras(path), timeframe, MODES["parity"])
            elif is_indicator:
                result[role]["rc2"] = indicator_checks(run, extras(path), timeframe)
            aligned[role] = run
            comparisons[f"{role}_vs_s5_current"] = shared(
                run, fresh.get("s5"), ("core", "ready") if is_indicator else ("core", "ready", "native")
            )
        except (OSError, UnicodeError, csv.Error, ValueError, KeyError) as exc:
            result[role] = {"status": "INVALID_EXPORT", "file": path.name, "reason": str(exc)}
    if {"aligned_parity", "aligned_rc1"} <= aligned.keys():
        comparisons["aligned_parity_vs_aligned_rc1"] = shared(
            aligned["aligned_parity"], aligned["aligned_rc1"], ("core", "ready", "native")
        )
    if "aligned_indicator" in aligned:
        for role in ("aligned_parity", "aligned_rc1"):
            comparisons[f"aligned_indicator_vs_{role}"] = shared(
                aligned["aligned_indicator"], aligned.get(role), ("core", "ready")
            )
    return {
        "exports": result, "comparisons": comparisons,
        "no_opportunities_in_default_export": runs["indicator"].ready == () if "indicator" in runs else None,
    }


def report(native_dir, baseline_archive=BASELINE_ARCHIVE):
    native_dir, baseline_archive = Path(native_dir), Path(baseline_archive)
    require(rc1.digest((rc1.ROOT / rc1.BASELINE).read_bytes()) == rc1.BASELINE_SHA, "immutable S5 source SHA changed")
    for name, expected in RC1_SOURCES.items():
        require(rc1.digest((rc1.ROOT / rc1.RELEASE / name).read_bytes()) == expected,
                f"immutable RC1 source SHA changed: {name}")
    require(rc1.digest(baseline_archive.read_bytes()) == BASELINE_ARCHIVE_SHA, "immutable RC1 archive SHA changed")
    with TemporaryDirectory() as temp, ZipFile(baseline_archive) as archive:
        baseline = Path(temp)
        for frame in rc1.FRAMES:
            for role in ("indicator", "audit", "reference"):
                name = f"{role}_M{frame}.csv"
                (baseline / name).write_bytes(archive.read(f"strict/{name}"))
        frames = {f"M{frame}": verify_frame(native_dir, baseline, frame) for frame in rc1.FRAMES}
    exports = [item for frame in frames.values() for role, item in frame["exports"].items() if role in ROLES]
    current = [item for frame in frames.values() for role, item in frame["exports"].items() if role not in ROLES]
    comparisons = [item for frame in frames.values() for item in frame["comparisons"].values()]
    failed = any(item["status"] == "INVALID_EXPORT" for item in (*exports, *current)) or any(
        item["status"] == "MISMATCH" for item in comparisons
    )
    covered = all(item["status"] == "MATCH" for item in comparisons) and all(
        item.get("rc2", {}).get("status") == "VERIFIED" for item in exports
    )
    return {
        "status": "FAILED" if failed else "VERIFIED" if covered else "PARTIALLY_COMPLETE",
        "baseline_archive_sha256": BASELINE_ARCHIVE_SHA,
        "baseline_s5_sha256": rc1.BASELINE_SHA,
        "baseline_rc1_sources": RC1_SOURCES,
        "rc2_sources_on_disk": {
            path.name: rc1.digest(path.read_bytes())
            for path in sorted((rc1.ROOT / "releases" / "gold-pro-rc2").glob("QIRVANTA_GOLD_PRO_RC2*.pine"))
        },
        "frames": frames,
        "scope": "Exact observed CSV comparisons on each shared window. No live delivery or unseen performance inference.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-dir", type=Path, required=True)
    parser.add_argument("--baseline-archive", type=Path, default=BASELINE_ARCHIVE)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        result = report(args.native_dir, args.baseline_archive)
    except (EvidenceError, OSError, KeyError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")
    rendered = json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n"
    if args.output:
        args.output.write_bytes(rendered.encode("utf-8"))
    else:
        print(rendered, end="")
    return {"VERIFIED": 0, "PARTIALLY_COMPLETE": 1, "FAILED": 2}[result["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
