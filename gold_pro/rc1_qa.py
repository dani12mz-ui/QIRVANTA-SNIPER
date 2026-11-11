"""Validate exported RC1 Pine Logs without reconstructing missing events or fills."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import io
from itertools import zip_longest
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BASELINE = Path("unified/experimental/QIRVANTA_SNIPER_PREMIUM_V2.pine")
BASELINE_SHA = "a920f15370b536f19def0205b21915e1a3d2e4ed4c76c80bd22f5e8aa6bf7c10"
CORE_SHA = "0d12e94769569a5af6c70061ae1ea4ff5cf4b41b1d8e0867abf739c70710c1d6"
RELEASE = Path("releases/gold-pro-rc1")
SYMBOL = "OANDA:XAUUSD"
FRAMES = (5, 15, 30)
ALERT_SCHEMA = "qirvanta.gold-pro.rc1.v1"
CORE_STATES = {"WATCH", "CONFIRMED", "CANCELLED", "EXPIRED", "REJECTED"}
ALERT_STATES = {"WATCH": "WATCH", "CONFIRMED": "CONFIRMED", "CANCELLED": "INVALIDATED", "EXPIRED": "EXPIRED"}
CORE_FIELDS = frozenset((
    "schema core_sha256 event_id event book plan_id strategy_key target_basis quality quality_note symbol timeframe "
    "family side bar_open_time bar_close_time effective_time known_time price quantity_fraction gross_r cost_r "
    "net_r cash_pnl risk_basis reference initial_sl tp1 tp2 tp3 evidence reason created_time atr_at_signal "
    "roundtrip_cost_price max_risk_atr obstacle required_room_r countertrend local_aligned efficiency_ratio "
    "filter_eligible advisory_only"
).split())
PLAN_FIELDS = {"entry": "reference", "sl": "initial_sl", "tp1": "tp1", "tp2": "tp2", "tp3": "tp3"}
PAYLOAD_FIELDS = {
    "model", "direction", "state", "confirmed_ms", "confirmation_utc", "reason",
    "event_id", "source_id", "symbol", "timeframe", "provisional", *PLAN_FIELDS,
}


class EvidenceError(ValueError):
    """The supplied export cannot substantiate its claimed run."""


def require(condition, message):
    if not condition:
        raise EvidenceError(message)


def number(value, name):
    require(type(value) in (int, Decimal, str), f"{name}: invalid number")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise EvidenceError(f"{name}: invalid number") from exc
    require(result.is_finite(), f"{name}: non-finite number")
    return result


def epoch(value, name):
    require(type(value) is int and value > 0, f"{name}: expected positive integer milliseconds")
    return value


def json_number(value, name):
    require(type(value) in (int, Decimal), f"{name}: expected a JSON number")
    return number(value, name)


def pipe_int(value):
    try:
        return int(value)
    except ValueError as exc:
        raise EvidenceError(f"invalid context integer {value!r}") from exc


def nonempty(value, name):
    require(isinstance(value, str) and value.strip(), f"{name}: expected nonempty text")
    return value


def encode(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def digest(data):
    return sha256(data).hexdigest()


@dataclass(frozen=True)
class Context:
    first_open: int
    index: int
    opening: int
    closing: int
    ohlc: tuple[Decimal, ...]
    htf_closes: tuple[int | None, ...]

    @classmethod
    def parse(cls, text, timeframe):
        fields = text.split("|")
        require(len(fields) == 13 and fields[:3] == ["1", SYMBOL, str(timeframe)],
                "RC1_CTX: unexpected schema, symbol or timeframe")
        first, index, opening, closing = map(pipe_int, fields[3:7])
        epoch(first, "first open")
        epoch(opening, "bar open")
        epoch(closing, "bar close")
        require(index >= 0 and first <= opening < closing <= opening + timeframe * 60_000,
                "RC1_CTX: invalid bar window")
        o, h, low, c = (number(value, "OHLC") for value in fields[7:11])
        require(0 < low <= min(o, c) <= max(o, c) <= h, "RC1_CTX: invalid OHLC")
        htf = tuple(None if value == "null" else pipe_int(value) for value in fields[11:])
        for stamp in htf:
            require(stamp is None or 0 < stamp <= opening,
                    "RC1_CTX: HTF data was unavailable at the start of the chart bar")
        return cls(first, index, opening, closing, (o, h, low, c), htf)

    def canonical(self):
        return (self.first_open, self.index, self.opening, self.closing,
                *(price.normalize() for price in self.ohlc), *self.htf_closes)


@dataclass(frozen=True)
class Export:
    context: tuple[Context, ...]
    core: tuple[dict, ...]
    ready: tuple[dict, ...]
    native: tuple[dict, ...]
    alerts: tuple[dict, ...]
    meta: dict


def _json_dict(message):
    def unique_object(pairs):
        obj = dict(pairs)
        require(len(obj) == len(pairs), "duplicate JSON field")
        return obj

    def reject_constant(value):
        raise EvidenceError(f"non-finite JSON constant {value}")

    try:
        obj = json.loads(message, parse_float=Decimal, object_pairs_hook=unique_object,
                         parse_constant=reject_constant)
    except json.JSONDecodeError as exc:
        raise EvidenceError(f"malformed Pine JSON at character {exc.pos}") from exc
    require(isinstance(obj, dict), "expected JSON object")
    return obj


def _validate_events(rows, book, bars, timeframe):
    ids = set()
    last_known = 0
    for row in rows:
        require(CORE_FIELDS <= row.keys(), f"{book}: missing {sorted(CORE_FIELDS - row.keys())}")
        require(row.get("schema") == "qirvanta.event.v1" and row.get("core_sha256") == CORE_SHA,
                f"{book}: unexpected core schema or source pin")
        require(row.get("book") == book and row.get("strategy_key") == "S5"
                and type(row.get("family")) is int and row["family"] == 2
                and type(row.get("side")) is int and row["side"] in (-1, 1)
                and row.get("advisory_only") is True,
                f"{book}: unexpected S5 event identity")
        require(row.get("symbol") == SYMBOL and row.get("timeframe") == str(timeframe),
                f"{book}: unexpected event symbol or timeframe")
        kind = nonempty(row.get("event"), "event kind")
        require(kind in CORE_STATES if book == "CORE" else kind.startswith("NATIVE_"),
                f"{book}: unexpected event kind {kind}")
        known = epoch(row.get("known_time"), "known time")
        ctx = bars.get(known)
        require(ctx and row.get("bar_open_time") == ctx.opening and row.get("bar_close_time") == ctx.closing,
                f"{book}: event outside the exported confirmed-bar context")
        effective = row.get("effective_time")
        require((effective is None and book == "NATIVE")
                or (epoch(effective, "effective time") <= known and (book == "NATIVE" or effective == known)),
                f"{book}: non-causal event time")
        event_id = nonempty(row.get("event_id"), "event ID")
        plan_id = nonempty(row.get("plan_id"), "plan ID")
        require(event_id == f"{plan_id}|{book}|{kind}|{known}" and event_id not in ids,
                f"{book}: duplicate or unstable event ID {event_id!r}")
        require(known >= last_known, f"{book}: event order moved backward in time")
        if book == "NATIVE":
            require(kind != "NATIVE_LEDGER_INCOMPLETE", "NATIVE: strategy reported an incomplete execution journal")
            closed = kind == "NATIVE_CLOSED" or kind.removeprefix("NATIVE_EXIT_").isdigit()
            if kind == "NATIVE_ENTRY" or closed:
                require(json_number(row["price"], "native fill price") > 0
                        and json_number(row["quantity_fraction"], "native fill fraction") > 0,
                        "NATIVE: nonpositive fill price or quantity")
            if closed:
                json_number(row["cash_pnl"], "native net cash")
        ids.add(event_id)
        last_known = known


def _validate_payload(payload, core_by_id, timeframe):
    require(PAYLOAD_FIELDS <= payload.keys(), f"alert: missing {sorted(PAYLOAD_FIELDS - payload.keys())}")
    require(payload["model"] == "S5" and payload["symbol"] == SYMBOL
            and payload["timeframe"] == str(timeframe), "alert: unexpected S5 model, symbol or timeframe")
    source = core_by_id.get(nonempty(payload["source_id"], "alert source ID"))
    require(source is not None, "alert: source CORE event is absent")
    require(payload["direction"] == ("BUY" if source["side"] == 1 else "SELL"),
            "alert: direction differs from the source")
    known = epoch(payload["confirmed_ms"], "alert confirmed time")
    require(known == source["known_time"], "alert: confirmation timestamp differs from source availability")
    stamp = nonempty(payload["confirmation_utc"], "alert UTC timestamp")
    try:
        parsed = datetime.fromisoformat(stamp.removesuffix("Z") + ("+00:00" if stamp.endswith("Z") else ""))
        expected = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=known)
    except (ValueError, OverflowError) as exc:
        raise EvidenceError("alert: invalid UTC timestamp") from exc
    require(parsed.utcoffset() == timedelta(0) and parsed == expected,
            "alert: UTC timestamp does not match confirmed milliseconds")
    nonempty(payload["reason"], "alert reason")
    event_id = nonempty(payload["event_id"], "alert event ID")
    state = payload["state"]
    require(event_id == f'{source["event_id"]}|RC1|{state}', "alert: unstable event ID")
    if state == "READY":
        require(source["event"] == "CONFIRMED" and payload["provisional"] is False,
                "READY: requires a non-provisional CORE confirmation")
        prices = {key: json_number(payload[key], key) for key in PLAN_FIELDS}
        require(all(value > 0 and value == json_number(source.get(PLAN_FIELDS[key]), PLAN_FIELDS[key])
                    for key, value in prices.items()), "READY: plan levels differ from CORE confirmation")
        side = source["side"]
        levels = [prices[key] for key in ("sl", "entry", "tp1", "tp2", "tp3")]
        require(all((right - left) * side > 0 for left, right in zip(levels, levels[1:])),
                "READY: stop/entry/target order does not match direction")
    else:
        require(state in ALERT_STATES.values() and state == ALERT_STATES.get(source["event"])
                and payload["provisional"] is (state == "WATCH"),
                "alert: state or provisional flag differs from the CORE event")
        require(payload["reason"] == source.get("reason"), "alert: reason differs from the CORE event")
        require(all(payload[key] is None for key in PLAN_FIELDS), "alert: non-READY event contains plan prices")


def _validate_payloads(ready, batches, core, timeframe, role):
    core_by_id = {row["event_id"]: row for row in core}
    ready_by_id = {}
    ready_sources = set()
    previous_ready = 0
    for payload in ready:
        _validate_payload(payload, core_by_id, timeframe)
        event_id, source = payload["event_id"], payload["source_id"]
        require(payload["state"] == "READY" and event_id not in ready_by_id and event_id not in core_by_id
                and source not in ready_sources, "duplicate READY or unexpected READY payload")
        require(payload["confirmed_ms"] > previous_ready, "READY: more than one plan per bar or out of chronological order")
        ready_by_id[event_id] = payload
        ready_sources.add(source)
        previous_ready = payload["confirmed_ms"]

    require(role == "indicator" or not batches, f"{role}: unexpectedly emitted user alert payloads")
    if role != "indicator":
        return ()
    alerts = []
    last_known = 0
    ids = set()
    expected_ids = {
        f'{row["event_id"]}|RC1|{ALERT_STATES[row["event"]]}' for row in core if row["event"] in ALERT_STATES
    } | ready_by_id.keys()
    for batch in batches:
        events = batch.get("events")
        require(batch.get("schema") == ALERT_SCHEMA and isinstance(events, list) and events,
                "alert: invalid batch schema or empty events")
        for payload in events:
            require(isinstance(payload, dict), "alert: expected event object")
            _validate_payload(payload, core_by_id, timeframe)
            event_id = payload["event_id"]
            known = payload["confirmed_ms"]
            require(event_id not in ids, f"duplicate alert event ID {event_id!r}")
            require(known > last_known, "alert: duplicate batch or events out of chronological order")
            require(known == events[0]["confirmed_ms"], "alert: mixed confirmation times in one batch")
            if payload["state"] == "READY":
                require(payload == ready_by_id.get(event_id), "alert: READY payload differs from the READY journal")
            ids.add(event_id)
            alerts.append(payload)
        last_known = events[0]["confirmed_ms"]
    require(not batches or ids == expected_ids,
            "alert: emitted batches omit events from the CORE or READY journal")
    return tuple(alerts)


def read_export(path, timeframe, role):
    """Read a native Pine Logs Date,Message CSV, allowing unrelated diagnostic rows."""
    path = Path(path)
    raw = path.read_bytes()
    rows = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
    require(rows.fieldnames == ["Date", "Message"], "expected TradingView Pine Logs Date,Message CSV")
    context = []
    journal = defaultdict(list)
    end = None
    try:
        for csv_row in rows:
            require(set(csv_row) == {"Date", "Message"} and csv_row["Date"] and csv_row["Message"] is not None,
                    "invalid Date,Message CSV row")
            for line in csv_row["Message"].splitlines():
                tag, sep, message = line.partition("|")
                if not tag.startswith("RC1_"):
                    continue
                require(sep and message, f"empty {tag} record")
                require(end is None, f"{tag}: RC1 record follows the end marker")
                if tag == "RC1_CTX":
                    ctx = Context.parse(message, timeframe)
                    if context:
                        prev = context[-1]
                        require(ctx.first_open == prev.first_open and ctx.index == prev.index + 1
                                and ctx.opening >= prev.closing, "RC1_CTX: missing, repeated or out-of-order bars")
                    context.append(ctx)
                elif tag == "RC1_END":
                    end = message
                elif tag in ("RC1_CORE", "RC1_READY", "RC1_NATIVE", "RC1_ALERT"):
                    journal[tag].append(_json_dict(message))
                else:
                    raise EvidenceError(f"unrecognized RC1 record {tag!r}")
    except csv.Error as exc:
        raise EvidenceError(f"invalid Pine Logs CSV: {exc}") from exc
    require(context, "export has no RC1_CTX records")
    first, last = context[0], context[-1]
    starts_at_first = first.index == 0 and first.first_open == first.opening
    if end is not None:
        require(end == f"1|{SYMBOL}|{timeframe}|{last.index}|{last.closing}",
                "RC1_END does not match the last logged bar")
    bars = {bar.closing: bar for bar in context}
    core, ready, native = (tuple(journal[f"RC1_{name}"]) for name in ("CORE", "READY", "NATIVE"))
    require(role != "indicator" or not native, "indicator emitted strategy-native execution records")
    _validate_events(core, "CORE", bars, timeframe)
    _validate_events(native, "NATIVE", bars, timeframe)
    alerts = _validate_payloads(ready, journal["RC1_ALERT"], core, timeframe, role)
    counts = {
        "core": dict(Counter(row["event"] for row in core)),
        "ready": dict(Counter(row["direction"] for row in ready)),
        "native": dict(Counter(row["event"] for row in native)),
        "alerts": dict(Counter(f'{row["direction"]} {row["state"]}' for row in alerts)),
    }
    meta = {
        "file": path.name, "csv_sha256": digest(raw),
        "context_sha256": digest(encode([bar.canonical() for bar in context]).encode("utf-8")),
        "bars": len(context), "chart_first_open_ms": first.first_open,
        "first_logged_bar_index": first.index, "first_logged_open_ms": first.opening,
        "last_logged_bar_index": last.index, "last_logged_close_ms": last.closing,
        "starts_at_first_loaded_bar": starts_at_first, "end_marker_present": end is not None,
        "events": counts,
    }
    return Export(tuple(context), core, ready, native, alerts, meta)


def compare_exports(left, right, streams):
    if left is None or right is None:
        return {"status": "MISSING_EVIDENCE"}
    for stream in ("context", *streams):
        for index, (a, b) in enumerate(zip_longest(getattr(left, stream), getattr(right, stream))):
            if a != b:
                if stream == "context":
                    a, b = (row.canonical() if row is not None else None for row in (a, b))
                return {"status": "MISMATCH", "stream": stream, "row": index + 1, "left": a, "right": b}
    if not all(run.meta["end_marker_present"] for run in (left, right)):
        coverage = "NO_END_MARKER"
    elif not all(run.meta["starts_at_first_loaded_bar"] for run in (left, right)):
        coverage = "BOUNDED_WINDOW"
    else:
        coverage = "LOADED_HISTORY_TO_CUTOFF"
    return {
        "status": "MATCHED_EXCERPT" if coverage == "NO_END_MARKER" else "MATCH",
        "coverage": coverage, "rows": {stream: len(getattr(left, stream)) for stream in streams},
    }


def compare_frame(native_dir, timeframe):
    exports, meta = {}, {}
    for role in ("indicator", "audit", "reference"):
        path = native_dir / f"{role}_M{timeframe}.csv"
        if not path.is_file():
            exports[role] = None
            meta[role] = {"status": "MISSING_EVIDENCE", "file": path.name}
            continue
        try:
            exports[role] = read_export(path, timeframe, role)
            meta[role] = {"status": "PARSED", **exports[role].meta}
        except (OSError, UnicodeError, csv.Error, EvidenceError) as exc:
            exports[role] = None
            meta[role] = {"status": "INVALID_EXPORT", "file": path.name, "reason": str(exc)}
    indicator, audit, reference = (exports[role] for role in ("indicator", "audit", "reference"))
    alerts = {"status": "NO_EXAMPLES" if indicator else "MISSING_EVIDENCE"}
    if indicator and indicator.alerts:
        alerts = {"status": "PAYLOADS_VERIFIED", "rows": len(indicator.alerts)}
    return {
        "exports": meta,
        "indicator_vs_audit": compare_exports(indicator, audit, ("core", "ready")),
        "indicator_vs_reference": compare_exports(indicator, reference, ("core", "ready")),
        "audit_vs_reference": compare_exports(audit, reference, ("core", "ready", "native")),
        "alert_payloads": alerts,
        "native_execution_examples": 0 if audit is None else sum(row["event"] == "NATIVE_ENTRY" for row in audit.native),
        "strategy_report_metrics": "NOT_VALIDATED_BY_PINE_LOGS",
    }


def compare_visual_export(strict_path, final_path, timeframe):
    """Compare the final render build over the shared window without moving the strict window."""
    runs = {}
    evidence = {}
    for name, path in (("strict", strict_path), ("final", final_path)):
        try:
            run = read_export(path, timeframe, "indicator")
        except FileNotFoundError:
            return {"status": "MISSING_EVIDENCE", "export": name, "file": Path(path).name}
        except (OSError, UnicodeError, csv.Error, EvidenceError) as exc:
            return {"status": "INVALID_EXPORT", "export": name, "reason": str(exc)}
        runs[name] = run
        evidence[name] = run.meta

    strict, final = runs["strict"], runs["final"]
    if not all(run.meta["end_marker_present"] for run in (strict, final)):
        return {"status": "MISSING_EVIDENCE", "reason": "missing bounded END", **evidence}
    if strict.context[-1].closing != final.context[-1].closing:
        return {"status": "MISMATCH", "stream": "context", "reason": "bounded ends differ", **evidence}
    opening = max(strict.context[0].opening, final.context[0].opening)
    strict_bars, final_bars = ([bar for bar in run.context if bar.opening >= opening] for run in (strict, final))
    if not strict_bars or not final_bars:
        return {"status": "MISMATCH", "stream": "context", "reason": "no shared window", **evidence}
    result = {
        **evidence, "overlap_first_open_ms": opening, "overlap_last_close_ms": strict_bars[-1].closing,
        "final_minus_strict_bar_index": final_bars[0].index - strict_bars[0].index,
    }
    start_close = strict_bars[0].closing
    streams = {
        "context": tuple([(bar.index - bars[0].index, *bar.canonical()[2:]) for bar in bars]
                         for bars in (strict_bars, final_bars)),
    }
    for name, time_field in (("core", "known_time"), ("ready", "confirmed_ms"), ("alerts", "confirmed_ms")):
        streams[name] = tuple([row for row in getattr(run, name) if row[time_field] >= start_close]
                              for run in (strict, final))
    for name, (left, right) in streams.items():
        for index, (a, b) in enumerate(zip_longest(left, right), 1):
            if a != b:
                return {"status": "MISMATCH", "stream": name, "row": index, "left": a, "right": b, **result}
    return {"status": "MATCH", "rows": {name: len(pair[0]) for name, pair in streams.items()}, **result}


def report(native_dir, root=ROOT, *, visual_recheck_dir=None):
    root, native_dir = Path(root), Path(native_dir)
    require((root / BASELINE).is_file() and digest((root / BASELINE).read_bytes()) == BASELINE_SHA,
            "immutable Premium V2 baseline SHA-256 does not match")
    frames = {f"M{timeframe}": compare_frame(native_dir, timeframe) for timeframe in FRAMES}
    comparisons = [result for frame in frames.values() for name, result in frame.items() if "_vs_" in name]
    release_sources = {
        str(path.relative_to(root)): digest(path.read_bytes())
        for path in sorted((root / RELEASE).glob("QIRVANTA_GOLD_PRO_RC1*.pine"))
    }
    invalid = any(export["status"] == "INVALID_EXPORT" for frame in frames.values() for export in frame["exports"].values())
    if invalid or any(item["status"] == "MISMATCH" for item in comparisons):
        status = "PARITY_FAILED"
    elif all(item["status"] == "MATCH" for item in comparisons) and all(
        frame["alert_payloads"]["status"] == "PAYLOADS_VERIFIED" and frame["native_execution_examples"]
        for frame in frames.values()
    ):
        status = "PINE_LOG_PARITY_VERIFIED"
    else:
        status = "PARTIALLY_COMPLETE"
    result = {
        "status": status, "baseline": {"path": BASELINE.as_posix(), "sha256": BASELINE_SHA, "core_sha256": CORE_SHA},
        "release_sources_on_disk": release_sources, "frames": frames,
        "scope": "Supplied TradingView exports only. No compilation, alert delivery or Strategy Report metric is inferred.",
    }
    if visual_recheck_dir is not None:
        final_dir = Path(visual_recheck_dir)
        checks = {}
        for timeframe in FRAMES:
            filename = f"indicator_M{timeframe}.csv"
            checks[f"M{timeframe}"] = compare_visual_export(native_dir / filename, final_dir / filename, timeframe)
        result["visual_recheck"] = checks
        statuses = {check["status"] for check in checks.values()}
        if statuses & {"MISMATCH", "INVALID_EXPORT"}:
            result["status"] = "PARITY_FAILED"
        elif "MISSING_EVIDENCE" in statuses and status == "PINE_LOG_PARITY_VERIFIED":
            result["status"] = "PARTIALLY_COMPLETE"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-dir", type=Path, required=True, help="folder containing role_M5/M15/M30.csv exports")
    parser.add_argument("--visual-recheck-dir", type=Path,
                        help="folder of final-render indicator_M5/M15/M30.csv exports for shared-window comparisons")
    parser.add_argument("--output", type=Path, help="write the JSON report to this path")
    args = parser.parse_args()
    try:
        result = report(args.native_dir, visual_recheck_dir=args.visual_recheck_dir)
    except EvidenceError as exc:
        parser.exit(1, f"{exc}\n")
    rendered = json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if result["status"] == "PINE_LOG_PARITY_VERIFIED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
