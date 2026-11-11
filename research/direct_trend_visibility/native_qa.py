"""Validate native TradingView journals for the directional visibility experiment."""

import argparse
import csv
import json
from pathlib import Path
from zipfile import ZipFile

from gold_pro import rc1_qa as rc1, rc2_qa as rc2
from gold_pro.rc1_qa import EvidenceError, require
from research.direct_trend_breakout import shadow_native
from research.direct_trend_breakout.forensic_timeline import ms, utc


ROOT = Path(__file__).resolve().parents[2]
FRAMES = (5, 15, 30)
AUDIT = ROOT / "releases/gold-pro-rc2/QIRVANTA_GOLD_PRO_RC2_AUDIT.pine"
AUDIT_SHA256 = "4ba04321a9ef5456e8fc61aa2b5b3ab6c44398a62b18e31b3041d9dda530d42b"
PRIOR_REPORT = ROOT / "research/direct_trend_breakout/evidence/base-native.json"
PRIOR_NATIVE = ROOT / "research/direct_trend_breakout/evidence/base-native-logs.zip"
RC2_INDICATOR_TAGS = ("RC2_ALERT", "RC2_EARLY", "RC2_FILTER")
RC2_AUDIT_TAGS = ("RC2_NATIVE_ALERT", "RC2_NATIVE_PART", "RC2_AUDIT_SUMMARY", "RC2_NATIVE_ANOMALY")
OBS_TAGS = ("DTV_STATE", "DTV_REVIEW", "DTV_ALERT", "DTV_LEDGER", "DTV_PAPER")
LEDGER_COUNTERS = ("entered", "closed", "capacity_skipped")
LEDGER_VALUES = ("net", "gross", "costs", "gains", "losses", "drawdown")
PERMISSIONS = {"BUY READY", "SELL READY", "WAIT", "RISK REJECTED", "POSITION OCCUPIED"}
READY_SIDES = {"BUY READY": 1, "SELL READY": -1}
SIDE_NAME = {1: "BUY", -1: "SELL"}
CASE_TIMES = {
    "bullish_retest": "00:15",
    "quiet": "11:30",
    "sell_watch": "11:45",
    "sell_break_s5_buy_watch": "12:00",
    "sell_retest_research": "12:30",
    "sell_consolidation_s5_buy_watch": "13:30",
}


def details(path, frame, role):
    run = rc1.read_export(path, frame, role)
    journal = rc2.extras(path)
    checks = (rc2.indicator_checks(run, journal, frame, research=True) if role == "indicator"
              else rc2.native_checks(path, run, journal, frame, rc2.MODES["fixed"]))
    return run, journal, {
        "file": path.name, "sha256": rc1.digest(path.read_bytes()),
        "closed_bars": len(run.context),
        "first_loaded_open_utc": utc(run.context[0].first_open),
        "first_exported_open_utc": utc(run.context[0].opening),
        "last_exported_close_utc": utc(run.context[-1].closing),
        **checks,
    }


def native_streams(left, right, tags):
    differences = [tag for tag in tags if left[tag] != right[tag]]
    return {"status": "MISMATCH" if differences else "MATCH",
            "mismatched_streams": differences, "rows": {tag: len(left[tag]) for tag in tags}}


def verify_pair(baseline, feature, frame, role):
    missing = [path.name for path in (baseline, feature) if not path.is_file()]
    if missing:
        return {"status": "MISSING_EVIDENCE", "files": missing}
    left, left_journal, left_meta = details(baseline, frame, role)
    right, right_journal, right_meta = details(feature, frame, role)
    streams = ("core", "ready", "alerts") if role == "indicator" else ("core", "ready", "native")
    tags = RC2_INDICATOR_TAGS if role == "indicator" else RC2_AUDIT_TAGS
    strict = rc1.compare_exports(left, right, streams)
    secondary = native_streams(left_journal, right_journal, tags)
    status = strict["status"]
    result = {
        "status": status, "baseline": left_meta, "visibility": right_meta,
        "core_ready_and_compatibility": strict, "rc2_native_streams": secondary,
    }
    if role == "indicator":
        paper = verify_paper(baseline, feature, frame, left, right)
        result["s5_raw_paper"] = paper
    if status == "MISMATCH" and strict.get("stream") == "context":
        common = rc2.shared(left, right, streams)
        result["shared_window"] = common
        if common["status"] in {"MATCH", "MATCHED_EXCERPT"}:
            result["status"] = "MATCHED_OVERLAP_ONLY"
        return result
    if secondary["status"] == "MISMATCH":
        result["status"] = "MISMATCH"
    if role == "indicator" and paper["status"] != "MATCH" and result["status"] == "MATCH":
        result["status"] = paper["status"]
    if role == "audit" and left_meta["status"] == "MISSING_NATIVE_PROPERTIES":
        result["status"] = "MISSING_NATIVE_PROPERTIES"
    return result


def verify_shadow(baseline, feature, frame):
    missing = [path.name for path in (baseline, feature) if not path.is_file()]
    if missing:
        return {"status": "MISSING_EVIDENCE", "files": missing}
    runs = []
    for path in (baseline, feature):
        try:
            run = shadow_native.read_native(path)
        except EvidenceError as exc:
            if "no DT_MARKET records" in str(exc):
                return {"status": "MISSING_SHADOW_EVIDENCE", "file": path.name}
            raise
        require(run.timeframe == frame, "shadow journal has unexpected chart interval")
        runs.append(run)
    left, right = runs
    exact = shadow_native.compare_repeat(left, right)
    result = {
        **exact, "baseline_events": len(left.events), "visibility_events": len(right.events),
        "baseline_first_open_utc": utc(left.bars[0]["time"]),
        "visibility_first_open_utc": utc(right.bars[0]["time"]),
    }
    if exact["status"] == "MATCH" and (not left.end or not right.end):
        result["status"] = "MATCHED_EXCERPT"
    return result


def audit_source_evidence():
    digest = rc1.digest(AUDIT.read_bytes())
    require(digest == AUDIT_SHA256, "fixed RC2 audit source differs from the pinned base")
    evidence = json.loads(PRIOR_REPORT.read_text(encoding="utf-8"))
    previous = {}
    with ZipFile(PRIOR_NATIVE) as archive:
        for frame in (5, 15):
            lane = evidence["frames"][f"M{frame}"]["lanes"]["audit-fixed"]
            require(lane["status"] == "VERIFIED", "prior fixed RC2 audit was not verified")
            require(rc1.digest(archive.read(lane["file"])) == lane["sha256"],
                    "prior fixed RC2 native journal digest differs from its report")
            previous[f"M{frame}"] = {
                "file": lane["file"], "sha256": lane["sha256"],
                "strategy_properties": lane["native_strategy_properties"],
            }
    return {
        "status": "PINNED_AUDIT_SOURCE_BYTE_IDENTICAL", "audit_sha256": digest,
        "prior_fixed_sl_native_results": previous,
        "scope": "The released audit strategy is pinned. Prior Strategy Tester results are separate from fresh paired replays.",
    }


def journal_rows(path):
    events = {tag: [] for tag in OBS_TAGS}
    with path.open(encoding="utf-8-sig", newline="") as raw:
        rows = csv.DictReader(raw, strict=True)
        require(rows.fieldnames == ["Date", "Message"], "expected TradingView Pine Logs Date,Message CSV")
        for row in rows:
            require(set(row) == {"Date", "Message"} and row["Message"] is not None, "invalid native CSV row")
            for line in row["Message"].splitlines():
                tag, separator, payload = line.partition("|")
                if tag in events:
                    require(separator and payload, f"{tag}: missing JSON")
                    events[tag].append(rc1._json_dict(payload))
    return events


def symbol_frame(row, frame, tag):
    require(row.get("symbol") == rc1.SYMBOL and str(row.get("frame")) == str(frame),
            f"{tag}: unexpected symbol or chart interval")


def source_kind(source):
    normalized = source.upper()
    if "SHADOW" in normalized or normalized == "DT" or normalized.startswith("DT "):
        return "SHADOW"
    if "EARLY" in normalized:
        return "EARLY"
    if "S5" in normalized or "RC2" in normalized:
        return "S5"
    return "OTHER"


def signal_stage(value):
    if value in {"WATCH", "RETEST WATCH", 1}:
        return "WATCH"
    if value in {"CONFIRMED", "BREAKOUT CONFIRMED", 2}:
        return "BREAKOUT CONFIRMED"
    raise EvidenceError(f"DTV_STATE: invalid directional stage {value!r}")


def signal_keys(signal, known):
    require(isinstance(signal, dict), "DTV_STATE: invalid signal row")
    source = rc1.nonempty(signal.get("source"), "directional source")
    side = signal.get("side")
    require(type(side) is int and side in SIDE_NAME, "DTV_STATE: invalid directional side")
    stage = signal_stage(signal.get("stage"))
    birth = rc1.epoch(signal.get("known_ms"), "observation known")
    expiry = rc1.epoch(signal.get("expires_ms"), "observation expiry")
    rc1.nonempty(signal.get("reason"), "directional reason")
    require(birth <= known < expiry, "DTV_STATE: future or stale directional observation")
    require(type(signal.get("research")) is bool, "DTV_STATE: missing research marker")
    kind = source_kind(source)
    require(kind != "OTHER", f"DTV_STATE: unrecognized directional source {source!r}")
    require(kind == "SHADOW" or not signal["research"], "DTV_STATE: legacy watch received a RESEARCH candidate marker")
    return kind, side, stage, birth


def direction_from_signals(keys):
    directions = {side for _, side, _, _ in keys}
    if len(directions) > 1:
        return "CONFLICT"
    if not directions:
        return "NONE"
    side, = directions
    stage = "BREAKOUT CONFIRMED" if any(key[2] == "BREAKOUT CONFIRMED" for key in keys) else "WATCH"
    return f"{SIDE_NAME[side]} {stage}"


def shadow_transitions(events):
    at_close, terminal = {}, {}
    for event in events:
        at_close.setdefault((event["time_ms"], event["side"]), []).append(event)
        if event["state"] in {"BREAKOUT CONFIRMED", "EXPIRED", "INVALIDATED"}:
            terminal.setdefault((event["side"], event["model"], event["watch_ms"]), []).append(event)
    return at_close, terminal


def require_shadow_origin(signal, key, shadow_events, terminal=None, as_of=None, frame=None):
    _, side, stage, known = key
    expected = {"WATCH", "RETEST WATCH"} if stage == "WATCH" else {"BREAKOUT CONFIRMED"}
    model = signal.get("model")
    events = shadow_events.get((known, side), ())
    require(any(event["state"] in expected and event["model"] == model for event in events),
            "DTV_STATE: shadow signal lacks native event at its declared confirmation close")
    candidate = stage == "BREAKOUT CONFIRMED" and any(
        event["state"] == "ENTRY CANDIDATE" and event["model"] == model for event in events
    )
    require(signal["research"] is candidate, "DTV_STATE: RESEARCH marker differs from native diagnostic candidate")
    if terminal is None:
        return
    if stage == "WATCH":
        ended = terminal.get((side, model, known), ())
        require(not any(event["time_ms"] <= as_of for event in ended),
                "DTV_STATE: shadow WATCH retained after an original terminal event")
        bars = 7
    else:
        linked = any(event["state"] == "RETEST WATCH" and event["origin_model"] == model
                     and event["breakout_ms"] == known for event in events)
        if linked:
            ended = terminal.get((side, "RETEST_CONTINUATION", known), ())
            require(not any(event["time_ms"] <= as_of and event["origin_model"] == model
                            and event["breakout_ms"] == known for event in ended),
                    "DTV_STATE: shadow confirmation retained after its linked retest ended")
        else:
            require(as_of == known, "DTV_STATE: unlinked shadow confirmation persisted beyond its confirmed close")
        bars = 7 if linked else 1
    require(signal["expires_ms"] == known + bars * frame * 60_000,
            "DTV_STATE: shadow expiry differs from original lifecycle window")


def verify_permissions(row, known, ready):
    permission = row.get("permission")
    require(permission in PERMISSIONS, f"DTV_STATE: invalid trade permission {permission!r}")
    source = rc1.nonempty(row.get("permission_source"), "permission source")
    require(source_kind(source) != "SHADOW", "DTV_STATE: shadow state was promoted into trade permission")
    require("permission_known_ms" in row, "DTV_STATE: missing permission confirmation timestamp")
    stamp = row["permission_known_ms"]
    if stamp is not None:
        stamp = rc1.epoch(stamp, "permission known")
    require(stamp is None or stamp <= known, "DTV_STATE: future permission timestamp")
    rc1.nonempty(row.get("permission_reason"), "permission reason")
    visible = row.get("plan_visible")
    require(type(visible) is bool and (not visible or permission in READY_SIDES),
            "DTV_STATE: plan levels visible without an actual READY")
    if permission in READY_SIDES:
        require(stamp == known and (known, READY_SIDES[permission]) in ready,
                "DTV_STATE: executable permission has no matching native S5 READY")
    if permission == "RISK REJECTED":
        require(row.get("s5_stage") == "RISK REJECTED",
                "DTV_STATE: risk rejection does not come from the legacy S5/RC2 stage")
    if permission == "POSITION OCCUPIED":
        require("PAPER" in source.upper() and (row.get("paper_phase") == 2 or row.get("paper_capacity") is True),
                "DTV_STATE: occupied permission has no observed S5 RAW PAPER position or capacity block")


def review_evidence(rows, by_close):
    for row in rows:
        known = row.get("known_ms")
        require(known in by_close, "DTV_REVIEW: no known snapshot at the selected close")
        require(row.get("review") is True and row.get("as_of_ms") == known,
                "DTV_REVIEW: selected snapshot is not explicitly historical")
        frozen = ("direction", "signals", "permission", "permission_source",
                  "permission_known_ms", "permission_reason", "plan_visible")
        require(all(field in row and row[field] == by_close[known][field] for field in frozen),
                "DTV_REVIEW: differs from the state actually known at the selected close")
    return {"status": "MATCH" if rows else "NO_REVIEW_JOURNAL", "rows": len(rows)}


def alert_evidence(rows, frame, by_close, shadow_events):
    seen = set()
    for row in rows:
        symbol_frame(row, frame, "DTV_ALERT")
        side = {"BUY": 1, "SELL": -1}.get(row.get("side"), row.get("side"))
        known = rc1.epoch(row.get("known_ms"), "observational alert known")
        require(type(side) is int and side in SIDE_NAME and row.get("observation_only") is True,
                "DTV_ALERT: expected an explicitly observational directional event")
        require(source_kind(rc1.nonempty(row.get("source"), "observational alert source")) == "SHADOW",
                "DTV_ALERT: expected Direct Trend Shadow source")
        require(known in by_close and (known, side) not in seen,
                "DTV_ALERT: alert intent outside coverage or duplicate at close")
        require(any(event["state"] == "BREAKOUT CONFIRMED" for event in shadow_events.get((known, side), ())),
                "DTV_ALERT: no matching confirmed shadow event at the same close")
        require(not {"entry", "stop", "targets", "sl", "tp1", "tp2", "tp3", "order", "permission", "action"} & row.keys(),
                "DTV_ALERT: observational payload contains executable details")
        seen.add((known, side))
    return {"status": "INTENT_VERIFIED" if rows else "NO_OBSERVATIONAL_ALERT_INTENT",
            "rows": len(rows), "delivery": "NOT_VERIFIED_BY_PINE_LOGS"}


def visibility_cases(by_close):
    result = {}
    for name, hour in CASE_TIMES.items():
        close = ms(f"2026-11-11T{hour}:00Z")
        row = by_close.get(close)
        if row is None:
            result[name] = {"status": "MISSING_CLOSE", "known_utc": utc(close)}
            continue
        expectations = {
            "bullish_retest": ("BUY BREAKOUT CONFIRMED", "RETEST_CONTINUATION", 1),
            "quiet": ("NONE", None, 0),
            "sell_watch": ("SELL WATCH", "STRUCTURAL_BREAK", -1),
            "sell_break_s5_buy_watch": ("CONFLICT", "STRUCTURAL_BREAK", -1),
            "sell_retest_research": ("SELL BREAKOUT CONFIRMED", "RETEST_CONTINUATION", -1),
            "sell_consolidation_s5_buy_watch": ("CONFLICT", "CONSOLIDATION_BREAK", -1),
        }
        direction, model, side = expectations[name]
        require(row["direction"] == direction, f"M15 {hour} UTC: expected directional state {direction}")
        expected_stage = "WATCH" if name == "sell_watch" else "BREAKOUT CONFIRMED"
        if model:
            require(any(signal.get("model") == model and signal["side"] == side
                        and signal_stage(signal["stage"]) == expected_stage for signal in row["signals"]),
                    f"M15 {hour} UTC: expected {model} {expected_stage}")
        if direction == "CONFLICT":
            require(any(source_kind(signal["source"]) == "S5" and signal["side"] == 1
                        and signal_stage(signal["stage"]) == "WATCH" for signal in row["signals"]),
                    f"M15 {hour} UTC: expected separate opposing S5 BUY WATCH")
        if name not in {"bullish_retest", "quiet"}:
            require(row["permission"] == "WAIT",
                    f"M15 {hour} UTC: expected WAIT from the original idle S5 RAW PAPER state")
        result[name] = {"status": "VERIFIED", "known_utc": utc(close), **row}
    return result


def paper_evidence(rows, context, frame):
    ledger, paper = rows["DTV_LEDGER"], rows["DTV_PAPER"]
    if not ledger:
        require(not paper, "DTV_PAPER: no corresponding ledger snapshots")
        return None
    ledger_times = []
    for row in ledger:
        symbol_frame(row, frame, "DTV_LEDGER")
        known = rc1.epoch(row.get("known_ms"), "RAW PAPER ledger close")
        ledger_times.append(known)
        require(type(row.get("phase")) is int and row["phase"] in {0, 1, 2}, "DTV_LEDGER: unknown book phase")
        if row["phase"] in {1, 2}:
            rc1.nonempty(row.get("plan_id"), "RAW PAPER plan ID")
        for field in LEDGER_COUNTERS:
            value = row.get(field)
            require(type(value) is int and value >= 0, f"DTV_LEDGER: invalid {field} counter")
        for field in LEDGER_VALUES:
            rc1.json_number(row.get(field), f"RAW PAPER {field}")
        require(row["entered"] >= row["closed"], "DTV_LEDGER: closed campaigns exceed entries")
    require(tuple(ledger_times) == tuple(bar.closing for bar in context), "DTV_LEDGER: incomplete or unordered ledger coverage")
    require(all(after[field] >= before[field] for before, after in zip(ledger, ledger[1:])
                for field in LEDGER_COUNTERS), "DTV_LEDGER: cumulative counter moved backward")
    known_closes = set(ledger_times)
    last = 0
    seen = set()
    for event in paper:
        require(event.get("symbol") == rc1.SYMBOL and str(event.get("timeframe")) == str(frame)
                and event.get("book") == "RAW", "DTV_PAPER: expected native S5 RAW PAPER journal event")
        known = rc1.epoch(event.get("known_time"), "RAW PAPER event known")
        key = rc1.nonempty(event.get("event_id"), "RAW PAPER event ID")
        require(known in known_closes and known >= last and key not in seen,
                "DTV_PAPER: duplicate or out-of-order event, or event outside ledger coverage")
        seen.add(key)
        last = known
    return ledger, paper


def verify_paper(baseline, feature, frame, left, right):
    a = paper_evidence(journal_rows(baseline), left.context, frame)
    b = paper_evidence(journal_rows(feature), right.context, frame)
    if a is None or b is None:
        return {"status": "MISSING_PAPER_EVIDENCE"}
    difference = [tag for tag, x, y in zip(("DTV_LEDGER", "DTV_PAPER"), a, b) if x != y]
    return {
        "status": "MISMATCH" if difference else "MATCH",
        "mismatched_streams": difference,
        "ledger_snapshots": len(a[0]), "execution_events": len(a[1]),
        "last_close": utc(a[0][-1]["known_ms"]),
        "final_ledger_r": {field: a[0][-1][field] for field in LEDGER_COUNTERS + LEDGER_VALUES},
        "scope": "Native indicator RAW PAPER events and ledger. P&L and drawdown are in R.",
    }


def quiet_window(context):
    end = ms("2026-11-11T11:30:00Z")
    start = end - 3_600_000
    bars = [bar for known, bar in context.items() if start < known <= end]
    if len(bars) != 4 or bars[0].opening != start or bars[-1].closing != end:
        return {"status": "MISSING_CONTEXT"}
    span = max(bar.ohlc[1] for bar in bars) - min(bar.ohlc[2] for bar in bars)
    move = bars[-1].ohlc[3] - bars[0].ohlc[0]
    ratio = abs(move) / span
    return {"status": "VERIFIED", "from_utc": utc(start), "through_utc": utc(end),
            "bars": len(bars), "range": span, "net_change": move, "net_to_range": ratio}


def verify_visibility(path, frame, review_path=None):
    if not path.is_file():
        return {"status": "MISSING_EVIDENCE", "file": path.name}
    rows = journal_rows(path)
    if not rows["DTV_STATE"]:
        return {"status": "MISSING_VISIBILITY_EVIDENCE", "file": path.name}
    run = rc1.read_export(path, frame, "indicator")
    context = {bar.closing: bar for bar in run.context}
    ready = {(event["confirmed_ms"], 1 if event["direction"] == "BUY" else -1) for event in run.ready}
    try:
        shadow = shadow_native.read_native(path)
    except EvidenceError as exc:
        if "no DT_MARKET records" not in str(exc):
            raise
        shadow = None
    shadow_events, terminal = shadow_transitions(shadow.events if shadow else ())
    early = rc2.early_events(rc2.extras(path)["RC2_EARLY"], run, frame)
    s5_watches = {(event["known_time"], event["side"]) for event in run.core if event["event"] == "WATCH"}
    paper = paper_evidence(rows, run.context, frame)
    ledger_by_close = {row["known_ms"]: row for row in paper[0]} if paper else {}
    capacity_by_close = {event["known_time"] for event in paper[1] if event["event"] == "CAPACITY"} if paper else set()
    by_close = {}
    for row in rows["DTV_STATE"]:
        symbol_frame(row, frame, "DTV_STATE")
        known = rc1.epoch(row.get("known_ms"), "dashboard known")
        require(row.get("review") is False and row.get("as_of_ms") == known,
                "DTV_STATE: canonical snapshot must use its own confirmed close")
        bar = context.get(known)
        require(bar is not None and bar.opening == row.get("open_ms"), "DTV_STATE: snapshot lacks closed bar context")
        require(known not in by_close and (not by_close or known > next(reversed(by_close))),
                "DTV_STATE: duplicate or out-of-order snapshot")
        signals = row.get("signals")
        require(isinstance(signals, list), "DTV_STATE: expected directional observation list")
        keys = [signal_keys(signal, known) for signal in signals]
        require(row.get("direction") == direction_from_signals(keys), "DTV_STATE: directional aggregation is inconsistent")
        for signal, key in zip(signals, keys):
            if key[0] == "SHADOW":
                require_shadow_origin(signal, key, shadow_events, terminal, known, frame)
            if key[0] == "S5":
                require(key[2] == "WATCH", "DTV_STATE: S5 reclaim confirmation mislabeled as a directional breakout")
                if key[3] in context:
                    require((key[3], key[1]) in s5_watches, "DTV_STATE: S5 WATCH lacks native CORE observation")
            if key[0] == "EARLY":
                require(key[2] == "WATCH", "DTV_STATE: EARLY observation mislabeled as a confirmed breakout")
                if key[3] in context:
                    require(early.get(key[3]) == SIDE_NAME[key[1]], "DTV_STATE: EARLY WATCH lacks native observation")
        if known in ledger_by_close and "paper_phase" in row:
            require(row["paper_phase"] == ledger_by_close[known]["phase"], "DTV_STATE: displayed PAPER phase differs from ledger")
        if row.get("permission") == "POSITION OCCUPIED":
            ledger = ledger_by_close.get(known)
            require(ledger is not None and (ledger["phase"] == 2 or known in capacity_by_close),
                    "DTV_STATE: no actual RAW PAPER occupancy evidence at displayed close")
        verify_permissions(row, known, ready)
        by_close[known] = row
    require(tuple(by_close) == tuple(context), "DTV_STATE: incomplete closed-bar dashboard coverage")
    cases = visibility_cases(by_close) if frame == 15 else {}
    case_status = "VERIFIED" if cases and all(case["status"] == "VERIFIED" for case in cases.values()) else (
        "INCOMPLETE_CASE_WINDOW" if cases else "NOT_M15")
    review = review_evidence(rows["DTV_REVIEW"], by_close)
    if review_path and review_path.is_file():
        separate = journal_rows(review_path)["DTV_REVIEW"]
        require(separate, "DTV_REVIEW: dedicated historical export omitted the selected snapshot")
        review = {**review_evidence(separate, by_close), "file": review_path.name,
                  "sha256": rc1.digest(review_path.read_bytes()),
                  "selected_closes_utc": [utc(row["known_ms"]) for row in separate]}
    return {
        "status": "VERIFIED" if case_status != "INCOMPLETE_CASE_WINDOW" else "INCOMPLETE_CASE_WINDOW",
        "file": path.name, "closed_bar_snapshots": len(by_close),
        "review": review,
        "observational_alert": alert_evidence(rows["DTV_ALERT"], frame, by_close, shadow_events),
        "case_status": case_status, "case_timeline": cases,
        **({"quiet_counterexample_market_window": quiet_window(context)} if frame == 15 else {}),
    }


def report(native_dir):
    frames = {}
    for frame in FRAMES:
        baseline = native_dir / f"baseline-M{frame}.csv"
        feature = native_dir / f"visibility-M{frame}.csv"
        checks = {
            "indicator": lambda: verify_pair(baseline, feature, frame, "indicator"),
            "shadow": lambda: verify_shadow(baseline, feature, frame),
            "visibility": lambda: verify_visibility(feature, frame, native_dir / f"visibility-review-M{frame}.csv"),
            "paired_fixed_sl_audit": lambda: verify_pair(native_dir / f"baseline-audit-M{frame}.csv",
                                                       native_dir / f"visibility-audit-M{frame}.csv", frame, "audit"),
        }
        frame_result = {}
        for name, check in checks.items():
            try:
                frame_result[name] = check()
            except (EvidenceError, ValueError, OSError, csv.Error, KeyError) as exc:
                frame_result[name] = {"status": "MISMATCH", "reason": str(exc)}
        frames[f"M{frame}"] = frame_result
    required = [item[name]["status"] for item in frames.values() for name in ("indicator", "shadow", "visibility")]
    optional = [item["paired_fixed_sl_audit"]["status"] for item in frames.values()]
    if "MISMATCH" in required + optional:
        status = "MISMATCH"
    elif all(state in {"MATCH", "VERIFIED"} for state in required):
        status = "MATCH"
    else:
        status = "PARTIALLY_VERIFIED"
    return {"status": status, "frames": frames, "fixed_sl_audit_source": audit_source_evidence()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-dir", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = report(args.native_dir)
    text = json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8", newline="\n")
    else:
        print(text, end="")
    return 1 if result["status"] == "MISMATCH" else 0


if __name__ == "__main__":
    raise SystemExit(main())
