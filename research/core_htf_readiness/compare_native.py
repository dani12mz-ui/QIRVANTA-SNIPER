"""Audit confirmed CORE HTF snapshots and explicitly separate depth comparisons from parity."""

import argparse
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

from gold_pro import rc1_qa as rc1, rc2_qa as rc2
from research.direct_trend_breakout import shadow_native
from research.direct_trend_visibility.native_qa import journal_rows, paper_evidence
from research.luminous_visual_v2.compare_native import confirmed_history
from research.professional_signal_selector.native_qa import FRAMES, SHADOW_TAGS, log_messages


TAG = "CORE_HTF"
SEALED_START = 1794787200000
CONTROL = "37b903f46a2d878d9d0757da134620c22f2d067d"
CONTROL_SHA256 = "77feedcbef84e77b9270ec43840be5b823d838300d0a09d5ddb4fec7acc089f4"
FIELDS = (
    "version symbol frame close_ms core_frame htf_close ema50 ema50_prev ema200 atr_mean rsi adx "
    "ready_HTF warmup raw_phase raw_plan_id last_buy_bar last_sell_bar "
    "global_paused family_paused filtered_day_net filtered_day_streak"
).split()
HTF_NUMERIC = ("htf_close", "ema50", "ema50_prev")
LOCAL_NUMERIC = ("atr_mean", "rsi", "adx")
TOLERANCE = Decimal("0.001")
FRAMES_RESOLUTION = {5: "60", 15: "240", 30: "D"}
ACCEPTED_REASON = "\u0423\u0421\u041b\u041e\u0412\u0418\u042f\u0422\u0410 \u0421\u0410 \u0418\u0417\u041f\u042a\u041b\u041d\u0415\u041d\u0418"


def utc(stamp):
    return shadow_native.utc(stamp)


def timestamp(value):
    clock = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if clock.tzinfo != timezone.utc:
        raise argparse.ArgumentTypeError("window must be in UTC")
    return int(clock.timestamp() * 1000)


def integer(value):
    return None if value == "null" else int(value)


def number(value):
    return None if value == "null" else rc1.number(value, "CORE_HTF number")


def flag(value):
    rc1.require(value in ("true", "false"), f"CORE_HTF invalid boolean {value!r}")
    return value == "true"


def trend(value):
    if not value["ready_HTF"]:
        return None
    a, b, c = (value[key] for key in HTF_NUMERIC[:3])
    return 1 if a > b > c else -1 if a < b < c else 0


def core_row(line, frame, context, ledger, bars_by_index):
    parts = line.split("|")
    rc1.require(len(parts) == len(FIELDS) + 1, f"CORE_HTF expects {len(FIELDS)} fields, got {len(parts) - 1}")
    row = dict(zip(FIELDS, parts[1:]))
    rc1.require((row["version"], row["symbol"], row["frame"], row["core_frame"])
                == ("1", rc1.SYMBOL, str(frame), FRAMES_RESOLUTION[frame]),
                "CORE_HTF unexpected journal schema, symbol, timeframe or automatic HTF resolution")
    for field in ("close_ms", "raw_phase", "last_buy_bar", "last_sell_bar", "filtered_day_streak"):
        row[field] = integer(row[field])
    for field in (*HTF_NUMERIC, "ema200", *LOCAL_NUMERIC, "filtered_day_net"):
        row[field] = number(row[field])
    for field in ("ready_HTF", "warmup", "global_paused", "family_paused"):
        row[field] = flag(row[field])
    rc1.require(row["ema200"] is None, "EMA200 must remain intentionally unobserved")
    stamp = rc1.epoch(row["close_ms"], "CORE_HTF close")
    rc1.require(stamp in context, f"CORE_HTF lacks protected RC1_CTX at {stamp}")
    bar = context[stamp]
    readiness = all(row[key] is not None for key in HTF_NUMERIC[:3])
    expected_warmup = bar.index > 250 and readiness and all(row[key] is not None for key in LOCAL_NUMERIC)
    rc1.require((row["ready_HTF"], row["warmup"]) == (readiness, expected_warmup),
                f"CORE_HTF readiness/warmup does not follow the protected predicate at {utc(stamp)}")
    row["raw_plan_id"] = row["raw_plan_id"].replace("~", "|")
    rc1.require((row["raw_phase"], row["raw_plan_id"]) == (ledger[stamp]["phase"], ledger[stamp]["plan_id"]),
                f"CORE_HTF paper state differs from protected DTV_LEDGER at {utc(stamp)}")
    for name in ("buy", "sell"):
        index = row[f"last_{name}_bar"]
        rc1.require(index is None or 0 <= index <= bar.index and index in bars_by_index,
                    f"CORE_HTF accepted {name} bar outside causal prefix at {utc(stamp)}")
        row[f"last_{name}_close_ms"] = None if index is None else bars_by_index[index]["time_close"]
    row["core_trend"] = trend(row)
    return row


def historical_candles(messages, frame):
    history = confirmed_history(messages)
    bars = {}
    for line in history:
        tag, _, body = line.partition("|")
        if tag != "DT_MARKET":
            continue
        row = rc1._json_dict(body)
        stamp = row["time_close"]
        rc1.require(row["symbol"] == rc1.SYMBOL and row["timeframe"] == frame, "DT_MARKET symbol/frame differs")
        rc1.require(row["index"] == len(bars) and stamp not in bars, "DT_MARKET incomplete historical prefix")
        opening, closing, o, h, low, c = candle_value(row)
        rc1.require(type(opening) is int and type(closing) is int and opening % (frame * 60_000) == 0
                    and closing == opening + frame * 60_000 and closing < SEALED_START
                    and (not bars or opening >= next(reversed(bars))), "DT_MARKET misordered, misaligned or protected close")
        rc1.require(0 < low <= min(o, c) <= max(o, c) <= h, "DT_MARKET invalid OHLC")
        bars[stamp] = row
    rc1.require(bars and rc1._json_dict(history[-1].partition("|")[2]) == {
        "index": len(bars) - 1, "time_close": next(reversed(bars))
    }, "DT_END does not describe the complete loaded prefix")
    return history, bars


def candle_value(row):
    return (row["time"], row["time_close"], *(rc1.json_number(row[k], k) for k in shadow_native.OHLC))


def history_digest(bars):
    payload = "\n".join("|".join(format(value.normalize(), "f") if isinstance(value, Decimal) else str(value)
                                 for value in candle_value(row)) for row in bars.values())
    return sha256(payload.encode("utf-8")).hexdigest()


def tag_rows(messages, prefix):
    return tuple(line.partition("|")[2] for line in messages if line.partition("|")[0] == prefix)


def cross_check_eda(messages, rows, context):
    """S5 formation state remains in EDA; corroborate CORE and its logged transition at each close."""
    shared = {}
    for line in tag_rows(messages, "EDA_BAR"):
        part = line.split("|")
        rc1.require(len(part) >= 31 and part[0] == "1", "Malformed EDA_BAR compatibility row")
        stamp = int(part[3])
        rc1.require(stamp in rows and stamp not in shared, "EDA_BAR outside or duplicated in CORE_HTF window")
        row = rows[stamp]
        rc1.require((flag(part[10]), flag(part[11]), part[12], number(part[18]),
                     int(part[27]), part[28].replace("~", "|")) ==
                    (row["warmup"], row["ready_HTF"], row["core_frame"], row["atr_mean"],
                     row["raw_phase"], row["raw_plan_id"]), f"CORE_HTF differs from EDA_BAR at {utc(stamp)}")
        shared[stamp] = True
    sides = {}
    for line in tag_rows(messages, "EDA_SIDE"):
        part = line.split("|")
        rc1.require(len(part) >= 22 and part[0] == "1", "Malformed EDA_SIDE compatibility row")
        stamp, side = int(part[3]), int(part[4])
        key = (stamp, side)
        rc1.require(stamp in rows and side in (-1, 1) and key not in sides, "EDA_SIDE outside or duplicate in CORE_HTF window")
        sides[key] = part
    rc1.require(shared.keys() == rows.keys() and sides.keys() == {(s, d) for s in rows for d in (1, -1)},
                "EDA formation rows do not cover the complete CORE_HTF window")
    for (stamp, side), part in sides.items():
        previous_index = integer(part[10])
        accepted = flag(part[16]) and part[17] == ACCEPTED_REASON
        expected = context[stamp].index if accepted else previous_index
        key = "last_buy_bar" if side == 1 else "last_sell_bar"
        rc1.require(rows[stamp][key] == expected, f"CORE_HTF S5 cooldown state disagrees with EDA_SIDE at {utc(stamp)}")
    return sides


def load_run(path, frame, start, end, *, protected_only=False):
    messages = log_messages(path)
    history, market = historical_candles(messages, frame)
    by_index = {row["index"]: row for row in market.values()}
    market_first = next(iter(market.values()))
    rc1.require(market_first["time"] <= start and end <= next(reversed(market)),
                "Audit window lies outside historical journal origin/cutoff")
    expected = tuple(stamp for stamp in market if start <= stamp <= end)
    run = rc1.read_export(path, frame, "indicator")
    rc1.require(run.meta["end_marker_present"], "Missing RC1_END")
    context = {bar.closing: bar for bar in run.context}
    rc1.require(expected and tuple(context) == expected, "RC1_CTX differs from requested complete historical close set")
    for stamp, bar in context.items():
        rc1.require((bar.first_open, bar.index, bar.opening, bar.closing, *bar.ohlc) ==
                    (market_first["time"], market[stamp]["index"], *candle_value(market[stamp])),
                    f"RC1_CTX chart origin/index/OHLC differs from DT_MARKET at {utc(stamp)}")
    protected = journal_rows(path)
    raw = paper_evidence(protected, run.context, frame)
    rc1.require(raw is not None, "Missing protected RAW PAPER ledger")
    ledger = {item["known_ms"]: item for item in raw[0]}
    rc1.require(tuple(item["known_ms"] for item in protected["DTV_STATE"]) == expected,
                "Missing protected DTV_STATE clocks")
    rc2.indicator_checks(run, rc2.extras(path), frame, research=True)
    s5 = {"CORE": run.core, "READY": run.ready, "ALERT": run.alerts,
          "RAW": raw[1], "LEDGER": raw[0], "STATE": protected["DTV_STATE"]}
    capture = {"path": path, "frame": frame, "logs": messages, "history": history, "market": market,
               "context": context, "s5": s5}
    rows = {}
    for line in messages:
        if line.startswith(TAG + "|"):
            rc1.require(not protected_only, "Unexpected probe tag in protected control")
            row = core_row(line, frame, context, ledger, by_index)
            stamp = row["close_ms"]
            rc1.require(stamp not in rows, f"Duplicate CORE_HTF at {utc(stamp)}")
            rows[stamp] = row
    if protected_only:
        return {**capture, "rows": {}, "sides": {}}
    rc1.require(tuple(rows) == expected, "CORE_HTF missing or disordered in requested close set")
    sides = cross_check_eda(messages, rows, context)
    return {**capture, "rows": rows, "sides": sides}


def run_details(run, source_sha):
    market, rows = run["market"], run["rows"]
    first = next(iter(market.values()))
    first_ready = next((stamp for stamp, row in rows.items() if row["ready_HTF"]), None)
    first_warmed = next((stamp for stamp, row in rows.items() if row["warmup"]), None)
    return {
        "file": run["path"].name, "log_sha256": rc1.digest(run["path"].read_bytes()), "source_sha256": source_sha,
        "chart_first_open_ms": first["time"], "chart_first_open_utc": utc(first["time"]),
        "chart_last_historical_close_ms": next(reversed(market)), "chart_last_historical_close_utc": utc(next(reversed(market))),
        "prefix_confirmed_bars": len(market), "prefix_ohlc_sha256": history_digest(market),
        "audited_closes": len(run["context"]), "raw_core_observation_closes": len(rows),
        "ready_HTF_closes": sum(row["ready_HTF"] for row in rows.values()) if rows else None,
        "warmed_closes": sum(row["warmup"] for row in rows.values()) if rows else None,
        "first_observed_ready_ms": first_ready, "first_observed_warmed_ms": first_warmed,
        "unavailable_by_field": {key: sum(row[key] is None for row in rows.values()) for key in (*HTF_NUMERIC, *LOCAL_NUMERIC)} if rows else None,
        "ema200": "UNOBSERVED_NOT_A_GATE",
        "core_trends": dict(Counter("MISSING" if row["core_trend"] is None else str(row["core_trend"]) for row in rows.values())),
        "s5_stream_counts": {tag: len(value) for tag, value in run["s5"].items()},
        "raw_event_types": dict(Counter(item["event"] for item in run["s5"]["RAW"])),
    }


def field_audit(left, right, fields, allowed_delta=None):
    stamps = left.keys() & right.keys()
    result = {}
    for field in fields:
        different = [(stamp, left[stamp][field], right[stamp][field]) for stamp in sorted(stamps)
                     if left[stamp][field] != right[stamp][field]]
        missing = [(stamp, a, b) for stamp, a, b in different if a is None or b is None]
        failures = different if allowed_delta is None else [
            (stamp, a, b) for stamp, a, b in different if a is None or b is None or abs(a - b) > allowed_delta
        ]
        delta = [abs(a - b) for _, a, b in different if type(a) is Decimal and type(b) is Decimal]
        result[field] = {
            "differences": len(different), "one_sided_missing": len(missing), "required_bound_failed": len(failures),
            "both_present": sum(left[s][field] is not None and right[s][field] is not None for s in stamps),
            "maximum_observed_absolute_delta": max(delta, default=None),
            "first_difference": {"at_ms": different[0][0], "left": different[0][1], "right": different[0][2]} if different else None,
        }
    return result


def comparable_events(left, right):
    result = {}
    for tag, a in left["s5"].items():
        b = right["s5"][tag]
        event_name = {"CORE": "event", "RAW": "event"}.get(tag)
        result[tag] = {"exact": a == b, "counts": [len(a), len(b)],
                       "examples": "OBSERVED" if a or b else "NONE_OBSERVED"}
        if event_name:
            result[tag]["event_counts"] = [dict(Counter(row[event_name] for row in lane)) for lane in (a, b)]
            by_id = [{row["event_id"]: row for row in lane} for lane in (a, b)]
            common = by_id[0].keys() & by_id[1].keys()
            result[tag]["shared_event_ids"] = len(common)
            result[tag]["unmatched_event_ids"] = [len(lane.keys() - common) for lane in by_id]
            result[tag]["changed_fields_across_shared_events"] = dict(sorted(Counter(
                k for identity in common
                for k in by_id[0][identity].keys() | by_id[1][identity].keys()
                if by_id[0][identity].get(k) != by_id[1][identity].get(k)
            ).items()))
    side_fields = (5, 6, 8, 9, 11, 12, 14, 15, 16, 17, 21)
    ledger_numeric = ("entered", "closed", "net", "gross", "costs", "drawdown", "capacity_skipped")
    result["LEDGER"]["first_and_last_cumulative"] = [
        [{"known_ms": row["known_ms"], **{key: row[key] for key in ledger_numeric}}
         for row in (lane["s5"]["LEDGER"][0], lane["s5"]["LEDGER"][-1])]
        for lane in (left, right)
    ]
    result["LEDGER"]["closed_event_totals_in_window"] = [
        {"campaigns": len(closed), **{key: sum(row[key] for row in closed) for key in ("gross_r", "net_r", "cost_r")}}
        for lane in (left, right)
        for closed in ([row for row in lane["s5"]["RAW"] if row["event"] == "CLOSED"],)
    ]
    left_side, right_side = ({key: tuple(row[i] for i in side_fields) for key, row in lane["sides"].items()}
                             for lane in (left, right))
    result["FORMATION_STATE"] = {"exact": left_side == right_side, "counts": [len(left_side), len(right_side)],
                                 "scope": "Phase, absolute origin timestamp, raw trigger, actual reason, cooldown."}
    return result


def immutable_records(run, additional_tags=()):
    exceptions = SHADOW_TAGS | set(additional_tags)
    return tuple(line for line in run["logs"] if line.partition("|")[0] not in exceptions)


def protected_comparison(left, right, left_source_sha, right_source_sha):
    """Compare same-window control without CORE_HTF against instrumented run."""
    ignored = {TAG}
    if not any(line.startswith("EDA_BAR|") for line in left["logs"]):
        ignored |= {"EDA_BAR", "EDA_SIDE"}
    originals, probes = (immutable_records(run, ignored) for run in (left, right))
    same = left["history"] == right["history"] and originals == probes
    return {
        "status": "ADDITIVE_PROBE_PROTECTED_PARITY" if same else "PROTECTED_STREAM_MISMATCH",
        "excluded_probe_only_tags": sorted(ignored),
        "left": run_details(left, left_source_sha), "right": run_details(right, right_source_sha),
        "confirmed_market_and_shadow_prefix_exact": left["history"] == right["history"],
        "protected_message_counts": [len(originals), len(probes)],
        "execution_coverage": "SIM_ENTRY_EXERCISED" if any(r["event"] == "SIM_ENTRY" for r in left["s5"]["RAW"])
        else "NO_SIMULATED_ENTRY_EXAMPLES",
        "raw_core_values_only_observed_in_right": True,
    }


def compare(left, right, left_source_sha, right_source_sha, expect):
    if expect == "protected":
        return protected_comparison(left, right, left_source_sha, right_source_sha)
    common = tuple(sorted(left["market"].keys() & right["market"].keys()))
    conflicts = [stamp for stamp in common if candle_value(left["market"][stamp]) != candle_value(right["market"][stamp])]
    same_cutoff = next(reversed(left["market"])) == next(reversed(right["market"]))
    ar, br = left["rows"], right["rows"]
    overlap_keys = tuple(left["market"]) == tuple(right["market"])
    same_history = overlap_keys and not conflicts
    s5 = comparable_events(left, right)
    same_journals = all(row["exact"] for row in s5.values())
    config = "SOURCE_HASH_UNWITNESSED" if not left_source_sha or not right_source_sha else (
        "SAME_SOURCE" if left_source_sha == right_source_sha else "SEPARATE_SOURCE_HASHES"
    )
    report = {
        "left": run_details(left, left_source_sha), "right": run_details(right, right_source_sha),
        "common_confirmed_ohlc": {"closes": len(common), "first_close_ms": common[0] if common else None,
                                  "last_close_ms": common[-1] if common else None, "ohlc_conflicts": len(conflicts),
                                  "first_conflict_ms": conflicts[0] if conflicts else None,
                                  "same_end_marker_close": same_cutoff, "identical_loaded_history": same_history},
        "s5": s5, "source_provenance": config,
        "alert_scope": "Protected Pine alert payloads in logs. Server delivery was not exercised by this comparator.",
        "timestamp_scope": "CORE_HTF is witnessed at chart close. A dedicated source timestamp for the original HTF raw tuple is not logged.",
    }
    if not common or conflicts or not same_cutoff or tuple(ar) != tuple(br):
        return {**report, "status": "NONCOMPARABLE_MARKET_OR_CUTOFF"}
    if (expect == "same" and not same_history) or (expect == "depth" and same_history):
        return {**report, "status": "UNEXPECTED_HISTORY_PAIR"}
    if same_history:
        full_native = (left["history"] == right["history"] and immutable_records(left) == immutable_records(right))
        status = "SAME_HISTORY_MISMATCH" if ar != br or not same_journals or not full_native else (
            "SAME_HISTORY_NATIVE_MATCH" if config == "SAME_SOURCE" else "SAME_HISTORY_PAYLOAD_MATCH_SOURCE_UNWITNESSED"
        )
        return {**report, "status": status, "all_existing_payloads_and_core_HTF_exact": full_native,
                "core_HTF_mismatch_closes": sum(ar[k] != br[k] for k in ar)}
    if common != tuple(min((left["market"], right["market"]), key=len)):
        return {**report, "status": "NONCOMPARABLE_UNMATCHED_SHALLOW_PREFIX"}
    price = {**field_audit(ar, br, ("htf_close",)), **field_audit(ar, br, ("ema50", "ema50_prev"), TOLERANCE)}
    exact = field_audit(ar, br, ("ready_HTF", "warmup", "core_trend", "raw_phase", "raw_plan_id", "last_buy_close_ms",
                                 "last_sell_close_ms", "global_paused", "family_paused", "filtered_day_net", "filtered_day_streak"))
    required = ("ready_HTF", "warmup", "core_trend")
    shared_warmed = sum(ar[stamp]["warmup"] and br[stamp]["warmup"] for stamp in ar)
    core_agrees = all(not item["required_bound_failed"] for item in price.values()) and all(
        not exact[key]["required_bound_failed"] for key in required
    )
    state_drift = any(exact[key]["required_bound_failed"] for key in exact if key not in required)
    status = "DEPTH_SENSITIVITY_MISMATCH" if not core_agrees or not same_journals or state_drift else (
        "NO_SHARED_WARMED_TUPLE" if not shared_warmed else "DEPTH_SENSITIVITY_NO_OBSERVED_TRADE_DRIFT"
    )
    return {**report, "status": status, "shared_warmed_closes": shared_warmed, "htf_close_requires_exact_match": True,
            "ema_absolute_tolerance": str(TOLERANCE), "htf_price_fields": price, "exact_state_fields": exact,
            "local_warmup_numeric_sensitivity": field_audit(ar, br, LOCAL_NUMERIC),
            "any_observed_s5_state_drift": state_drift,
            "interpretation": "Different loaded origins are a sensitivity experiment; agreement in this window does not prove general parity."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--frame", type=int, choices=FRAMES, required=True)
    parser.add_argument("--start", type=timestamp, default="2026-11-11T00:00:00Z")
    parser.add_argument("--end", type=timestamp, default="2026-11-11T17:30:00Z")
    parser.add_argument("--expect", choices=("same", "depth", "protected"), required=True)
    parser.add_argument("--left-source", type=Path)
    parser.add_argument("--right-source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.start <= args.end < SEALED_START:
        parser.error("expected chronological pre-seal window")
    try:
        a = load_run(args.left, args.frame, args.start, args.end, protected_only=args.expect == "protected")
        b = load_run(args.right, args.frame, args.start, args.end)
        source = [None if p is None else rc1.digest(p.read_bytes()) for p in (args.left_source, args.right_source)]
        analysis = compare(a, b, *source, args.expect)
    except (OSError, ValueError, KeyError, TypeError, IndexError) as exc:
        analysis = {"status": "INVALID_OR_MISSING_EVIDENCE", "reason": str(exc)}
    result = {"control_commit": CONTROL, "control_pine_sha256": CONTROL_SHA256,
              "frame": args.frame, "audited_utc": [utc(args.start), utc(args.end)], **analysis}
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    return 0 if result["status"] in {"SAME_HISTORY_NATIVE_MATCH", "ADDITIVE_PROBE_PROTECTED_PARITY", "DEPTH_SENSITIVITY_NO_OBSERVED_TRADE_DRIFT"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

