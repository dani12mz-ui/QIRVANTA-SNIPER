"""Reconcile the November 10 screenshot against native per-close diagnostics."""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
from hashlib import sha256
import io
import json
import math
from pathlib import Path
import re
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "docs/premium_v2_evidence/native_pair_20261110_1430Z_logs.zip"
REFERENCE = ROOT / "docs/premium_v2_evidence/QIRVANTA_PREMIUM_V2_final_test_draft.jpg"
TRACE_FIELDS = (
    "time", "time_close", "bar_index", "open", "high", "low", "close", "volume",
    "atr", "atr_mean", "bar_range", "ema_fast", "ema_slow", "rsi", "adx",
    "efficiency", "range20_high", "range20_low", "atr_percentile", "position_size",
    "native_stop", "warmup", "entry_frame", "tv_context_ready", "core_htf_ready",
    "valid_atr", "acceptable_bar", "time_gap", "date_pass", "news_paused", "session_pass",
    "require_h1_match", "require_fvg", "require_premium_discount", "h1_bias", "h4_bias",
    "portfolio", "buy_reason", "sell_reason", "buy_phase", "buy_born", "buy_level",
    "buy_anchor", "sell_phase", "sell_born", "sell_level", "sell_anchor", "selected_plan",
    "native_plan", "native_status", "closed_parts", "net_profit",
)
EVENT_FIELDS = (
    "time_close", "effective_time", "event", "book", "plan_id", "side", "reason",
    "price", "reference", "stop", "tp1", "tp2", "tp3", "quantity_fraction", "cash_pnl", "net_r",
)
INTEL_FIELDS = (
    "time", "time_close", "open", "high", "low", "close", "prior_atr", "adx",
    "ema21", "ema55", "momentum", "atr_ratio", "acceleration", "h1_bias", "h4_bias",
    "h1_close", "h4_close", "displacement_up", "displacement_down", "exhaustion",
    "candidate_side", "candidate_grade", "swing_high", "swing_low", "pending_side",
    "last_structure", "warning",
)


def epoch(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def utc(value):
    if value is None:
        return None
    return datetime.fromtimestamp(int(value) / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


CAPTURE = epoch("2026-11-10T14:52:12Z")
WINDOW_START = epoch("2026-11-09T18:00:00Z")
DAY_START = epoch("2026-11-10T00:00:00Z")
MINTICK = 0.001
SLIP = 2 * MINTICK
ROUND_TRIP_FEE = 2 * 0.175


def messages(raw):
    for record in csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))):
        yield from record["Message"].splitlines()


def scalar(value):
    if value in {"NaN", "na", "null"}:
        return None
    if value in {"true", "false"}:
        return value == "true"
    try:
        return int(value)
    except ValueError:
        try:
            parsed = float(value)
            return parsed if math.isfinite(parsed) else None
        except ValueError:
            return value


def split_record(fields, values, kind):
    if len(fields) != len(values):
        raise ValueError(f"{kind} has {len(values)} values, expected {len(fields)}")
    return dict(zip(fields, values))


def read_s5(path, minutes):
    raw = path.read_bytes()
    bars, events = {}, defaultdict(list)
    for line in messages(raw):
        for token, fields in (("S5_TRACE", TRACE_FIELDS), ("S5_EVENT", EVENT_FIELDS)):
            if token + "|" not in line:
                continue
            frame, payload = line.split(token + "|", 1)[1].split("|", 1)
            if frame != str(minutes):
                raise ValueError(f"Unexpected {token} timeframe {frame} in {path}")
            record = split_record(fields, json.loads(payload), token)
            if token == "S5_TRACE":
                if record["time_close"] in bars:
                    raise ValueError(f"Repeated close in {path}")
                bars[record["time_close"]] = record
            else:
                events[record["time_close"]].append(record)
    if not bars:
        raise ValueError(f"No S5 trace records in {path}")
    return [bars[k] for k in sorted(bars)], events, sha256(raw).hexdigest()


def read_intel(path, minutes):
    raw = path.read_bytes()
    bars, events = {}, defaultdict(list)
    for line in messages(raw):
        if "V21_INTEL_BAR|" in line:
            frame, *payload = line.split("V21_INTEL_BAR|", 1)[1].split("|")
            if frame != str(minutes):
                raise ValueError(f"Unexpected V2 Intelligence timeframe {frame} in {path}")
            record = split_record(INTEL_FIELDS, [scalar(v) for v in payload], "V21_INTEL_BAR")
            if record["time_close"] in bars:
                raise ValueError(f"Repeated close in {path}")
            bars[record["time_close"]] = record
        for token in ("V2_CANDIDATE", "V2_OBSERVATION"):
            if token + "|" in line:
                details = dict(part.split("=", 1) for part in line.split(token + "|", 1)[1].split("|") if "=" in part)
                close = details.get("known_close")
                if close is None:
                    raise ValueError(f"Missing knowledge time in {line}")
                if details.get("tf") != str(minutes) or details.get("symbol") != "OANDA:XAUUSD":
                    raise ValueError(f"Unexpected Intelligence source in {line}")
                events[int(close)].append({"record_type": token, **{k: scalar(v) for k, v in details.items()}})
    if not bars:
        raise ValueError(f"No Intelligence bar records in {path}")
    return [bars[k] for k in sorted(bars)], events, sha256(raw).hexdigest()


def native_summary(minutes):
    filename = f"v2_S5_M{minutes}.csv"
    with ZipFile(ARCHIVE) as zipped:
        raw = zipped.read(filename)
    rows = [json.loads(line) for line in messages(raw) if line.startswith("{")]
    run = next(x for x in rows if x["kind"] == "run")
    snapshot = next(x for x in rows if x["kind"] == "snapshot")
    submitted = {r["plan_id"]: r for r in rows if r["kind"] == "submitted"}
    exits = defaultdict(list)
    for row in rows:
        if row["kind"] == "closed":
            exits[row["entry_id"]].append(row)
    selected = [p for p in submitted.values() if p["submitted_time"] >= DAY_START]
    latest_sell = max((p for p in submitted.values() if p["side"] == -1), key=lambda p: p["submitted_time"])
    if latest_sell not in selected:
        selected.insert(0, latest_sell)
    campaigns = []
    for plan in selected:
        parts = exits[plan["plan_id"]]
        campaigns.append({
            "plan": plan,
            "submitted_utc": utc(plan["submitted_time"]),
            "closed_utc": utc(max((p["exit_time"] for p in parts), default=None)),
            "parts": parts,
            "net_usd": round(sum(p["profit"] for p in parts), 8),
            "commission_usd": round(sum(p["commission"] for p in parts), 8),
        })
    return {
        "source_file": filename,
        "csv_sha256": sha256(raw).hexdigest(),
        "run": run,
        "snapshot": snapshot,
        "run_first_open_utc": utc(run["first_bar_time"]),
        "run_end_utc": utc(run["cutoff"]),
        "selected_campaigns": campaigns,
        "nov10_submissions": sum(p["submitted_time"] >= DAY_START for p in submitted.values()),
    }


def s5_status(bar, events, side):
    side_text = "sell" if side == -1 else "buy"
    side_events = [e for e in events if e["side"] == side]
    for kind, state in (
        ("NATIVE_SUBMITTED", "SUBMITTED"),
        ("NATIVE_BLOCKED", "NATIVE_BLOCKED"),
        ("REJECTED", "REJECTED"),
        ("CONFIRMED", "CONFIRMED"),
        ("WATCH", "WATCH"),
        ("EXPIRED", "EXPIRED"),
        ("CANCELLED", "CANCELLED"),
    ):
        if any(e["event"] == kind for e in side_events):
            return state
    if not bar["warmup"] or not bar["entry_frame"]:
        return "WARMUP_OR_FRAME"
    if bar[side_text + "_phase"]:
        return "WAITING_RECLAIM"
    return "NO_RAW_TRIGGER"


def compare_range(path, minutes, base, base_events):
    extended, extended_events, digest = read_s5(path, minutes)
    extended_by_close = {row["time_close"]: row for row in extended}
    shared = set(base) & set(extended_by_close)
    fields = (
        "open", "high", "low", "close", "warmup", "entry_frame", "tv_context_ready",
        "core_htf_ready", "valid_atr", "acceptable_bar", "time_gap", "date_pass",
        "news_paused", "session_pass", "require_h1_match", "require_fvg",
        "require_premium_discount", "h1_bias", "h4_bias", "buy_reason", "sell_reason",
        "buy_phase", "buy_born", "buy_level", "buy_anchor", "sell_phase", "sell_born",
        "sell_level", "sell_anchor", "selected_plan", "native_plan", "native_status",
        "position_size",
    )
    counts = Counter()
    day = Counter()

    def decision_event(event):
        kind = re.sub(r"^NATIVE_EXIT_\d+$", "NATIVE_EXIT", event["event"])
        return (kind,) + tuple(event[k] for k in EVENT_FIELDS if k != "event")

    for close in sorted(shared):
        a, b = base[close], extended_by_close[close]
        diff = [key for key in fields if a[key] != b[key]]
        for key in diff:
            counts[key] += 1
            if DAY_START < close <= CAPTURE:
                day[key] += 1
        base_here = base_events.get(close, ())
        extended_here = extended_events.get(close, ())
        if base_here != extended_here:
            changed = ("native_exit_ordinals_only"
                       if list(map(decision_event, base_here)) == list(map(decision_event, extended_here))
                       else "event_payload")
            counts[changed] += 1
            if DAY_START < close <= CAPTURE:
                day[changed] += 1
    return {
        "source_csv_sha256": digest,
        "visible_session_shared_bars": sum(DAY_START < close <= CAPTURE for close in shared),
        "shared_exported_bars": len(shared),
        "base_bar_index_at_first_shared": base[min(shared)]["bar_index"] if shared else None,
        "extended_bar_index_at_first_shared": extended_by_close[min(shared)]["bar_index"] if shared else None,
        "differing_fields_entire_shared_export": dict(counts),
        "differing_fields_visible_nov10": dict(day),
        "extended_visible_nov10_warm_bars": sum(extended_by_close[close]["warmup"] for close in shared if DAY_START < close <= CAPTURE),
        "extended_visible_nov10_context_bars": sum(extended_by_close[close]["tv_context_ready"] for close in shared if DAY_START < close <= CAPTURE),
        "scope": "Comparison of causal states and complete event payloads on common exported closes. Native exit label ordinals, floating indicator values, bar index and cumulative strategy history may differ by calculation range.",
    }


def tick_up(price):
    return round(math.ceil(price / MINTICK - 1e-8) * MINTICK, 8)


def scenario(bars, index, end, prior_atr):
    if index + 1 > end or index < 2 or prior_atr is None or prior_atr <= 0:
        return None
    bar, nxt = bars[index], bars[index + 1]
    if nxt["time"] != bar["time_close"]:
        return {"status": "GAP_NO_ASSUMED_FILL"}
    entry = nxt["open"] - SLIP
    stop = tick_up(max(x["high"] for x in bars[index - 2:index + 1]) + 0.10 * prior_atr)
    risk = stop - entry
    period = bars[index + 1:end + 1]
    raw_mfe = max(0.0, entry - min(x["low"] for x in period))
    raw_mae = max(0.0, max(x["high"] for x in period) - entry)
    evidence = {
        "confirmation_utc": utc(bar["time_close"]),
        "next_open_utc": utc(nxt["time"]),
        "next_open": nxt["open"],
        "modeled_fill": round(entry, 8),
        "structural_stop": stop,
        "initial_risk": round(risk, 8),
        "full_path_mfe": round(raw_mfe, 8),
        "full_path_mae": round(raw_mae, 8),
        "mfe_less_baseline_exit_cost": round(raw_mfe - SLIP - ROUND_TRIP_FEE, 8),
        "mfe_less_stress_spread": round(raw_mfe - SLIP - ROUND_TRIP_FEE - 0.35, 8),
    }
    if risk <= 0 or risk > 3.5 * prior_atr:
        return {"status": "RISK_OUTSIDE_3_5_ATR_DIAGNOSTIC_CAP", **evidence}
    target = round(math.floor((entry - 2 * risk) / MINTICK + 1e-8) * MINTICK, 8)
    achieved = 0.0
    for row in period:
        stop_hit = row["high"] >= stop
        tp_hit = row["low"] <= target
        if stop_hit:
            fill = max(row["open"], stop) + SLIP
            exit_label = "STOP_FIRST_AMBIGUITY" if tp_hit else "STOP"
        elif tp_hit:
            fill = target
            achieved = max(achieved, entry - fill)
            exit_label = "TARGET"
        else:
            achieved = max(achieved, entry - row["low"])
            continue
        break
    else:
        row = period[-1]
        fill = row["close"] + SLIP
        exit_label = "MARK_TO_CUTOFF"
    return {
        "status": "HYPOTHETICAL_BRACKET",
        **evidence,
        "target_2r": target,
        "favorable_excursion_through_exit_conservative": round(achieved, 8),
        "exit_label": exit_label,
        "exit_bar_close_utc": utc(row["time_close"]),
        "exit_price": round(fill, 8),
        "net_usd_per_unit": round(entry - fill - ROUND_TRIP_FEE, 8),
        "net_stress_usd_per_unit": round(entry - fill - ROUND_TRIP_FEE - 0.35, 8),
    }


def event_summary(bars, s5_by_close, intel_by_close):
    visible = [b for b in bars if WINDOW_START <= b["time"] and b["time_close"] <= CAPTURE]
    if not visible:
        return None
    peak_index = max(range(len(visible)), key=lambda i: visible[i]["high"])
    low_index = min(range(peak_index, len(visible)), key=lambda i: visible[i]["low"])
    peak, low = visible[peak_index], visible[low_index]
    markers = {}
    for i in range(peak_index + 1, low_index + 1):
        current, prior = visible[i], visible[i - 1]
        exact_range = s5_by_close.get(current["time_close"], {}).get("range20_low")
        if exact_range is None and i >= 20:
            exact_range = min(x["low"] for x in visible[i - 20:i])
        if exact_range is None:
            continue
        bearish_break = current["close"] < exact_range <= prior["close"] and current["close"] < current["open"]
        if not bearish_break:
            continue
        p_atr = intel_by_close.get(current["time_close"], {}).get("prior_atr")
        if p_atr is None:
            p_atr = s5_by_close.get(prior["time_close"], {}).get("atr")
        market_range = current["high"] - current["low"]
        displacement = (
            p_atr is not None
            and current["open"] - current["close"] >= p_atr
            and current["close"] - current["low"] <= 0.25 * market_range
        )
        point = {
            "bar_open_utc": utc(current["time"]),
            "known_close_utc": utc(current["time_close"]),
            "minutes_from_peak_bar_close": (current["time_close"] - peak["time_close"]) / 60_000,
            "close": current["close"],
            "prior20_low": exact_range,
            "prior_atr": p_atr,
            "s5_sell_reason": s5_by_close.get(current["time_close"], {}).get("sell_reason"),
            "scenario_to_last_closed_screenshot_bar": scenario(visible, i, len(visible) - 1, p_atr),
        }
        markers.setdefault("first_prior20_floor_break", point)
        if displacement:
            markers.setdefault("first_floor_break_with_displacement", point)
    return {
        "definition": "Within the fixed screenshot pre-roll starting 2026-11-09 18:00 UTC, use earliest highest completed-bar high and earliest lowest completed-bar low on or after its bar. Intrabar high/low timestamps are unresolved.",
        "bars": len(visible),
        "last_confirmed_close_utc": utc(visible[-1]["time_close"]),
        "peak_bar_open_utc": utc(peak["time"]),
        "peak_bar_close_utc": utc(peak["time_close"]),
        "peak_price": peak["high"],
        "trough_bar_open_utc": utc(low["time"]),
        "trough_bar_close_utc": utc(low["time_close"]),
        "trough_price": low["low"],
        "drawdown_price": round(peak["high"] - low["low"], 8),
        "drawdown_pct": round(100 * (peak["high"] - low["low"]) / peak["high"], 8),
        "bearish_markers": markers,
        "right_censored": True,
    }


def analyze(folder, minutes, output):
    result = {"archived_v2_native": native_summary(minutes)}
    s5_file = folder / f"missed_sell_S5_M{minutes}.csv"
    intel_file = folder / f"missed_sell_INTEL_M{minutes}.csv"
    s5_bars, s5_events, intel_bars, intel_events = [], {}, [], {}
    if s5_file.is_file():
        s5_bars, s5_events, result["s5_trace_sha256"] = read_s5(s5_file, minutes)
    if intel_file.is_file():
        intel_bars, intel_events, result["intel_trace_sha256"] = read_intel(intel_file, minutes)
    s5_by_close = {b["time_close"]: b for b in s5_bars}
    intel_by_close = {b["time_close"]: b for b in intel_bars}
    shared = set(s5_by_close) & set(intel_by_close)
    for close in shared:
        for field in ("time", "time_close", "open", "high", "low", "close"):
            if s5_by_close[close][field] != intel_by_close[close][field]:
                raise ValueError(f"S5 and Intelligence price tapes disagree at {utc(close)} for {field}")
    result["shared_ohlc_rows"] = len(shared)
    result["visible_event"] = event_summary(s5_bars or intel_bars, s5_by_close, intel_by_close)
    count = {side: Counter() for side in ("buy", "sell")}
    reject = {side: Counter() for side in ("buy", "sell")}
    native_block = Counter()
    intelligence = Counter()
    timeline = []
    for close in sorted(set(s5_by_close) | set(intel_by_close)):
        if not (WINDOW_START < close <= CAPTURE):
            continue
        sb, ib = s5_by_close.get(close), intel_by_close.get(close)
        se, ie = s5_events.get(close, []), intel_events.get(close, [])
        row = {"time_open_utc": utc((sb or ib)["time"]), "known_close_utc": utc(close), **(sb or ib)}
        if sb:
            for name, side in (("buy", 1), ("sell", -1)):
                status = s5_status(sb, se, side)
                row["s5_" + name + "_decision"] = status
                if close > DAY_START:
                    count[name][status] += 1
                if status == "REJECTED" and close > DAY_START:
                    reject[name][sb[name + "_reason"]] += 1
            for e in se:
                if e["event"] == "NATIVE_BLOCKED" and close > DAY_START:
                    native_block[e["reason"]] += 1
        if ib:
            for k, v in ib.items():
                if k not in {"time", "time_close", "open", "high", "low", "close"}:
                    row["intel_" + k] = v
            for e in ie:
                if e["record_type"] == "V2_CANDIDATE" and close > DAY_START:
                    intelligence[(e.get("event"), str(e.get("grade")), str(e.get("reasons")))] += 1
        row["s5_events"] = json.dumps(se, ensure_ascii=False, separators=(",", ":"))
        row["intel_events"] = json.dumps(ie, ensure_ascii=False, separators=(",", ":"))
        timeline.append(row)
    if timeline:
        fields = list(dict.fromkeys(k for row in timeline for k in row))
        with (output / f"missed_sell_M{minutes}_timeline.csv").open("w", newline="", encoding="utf-8") as stream:
            writer = csv.DictWriter(stream, fields, lineterminator="\n")
            writer.writeheader()
            writer.writerows(timeline)
    result["s5_day_decision_counts"] = {side: dict(n) for side, n in count.items()}
    result["s5_day_rejection_reasons"] = {side: dict(n) for side, n in reject.items()}
    result["s5_day_native_blocks"] = dict(native_block)
    result["s5_day_position_bars"] = sum(b["position_size"] != 0 for b in s5_bars if DAY_START < b["time_close"] <= CAPTURE)
    result["s5_day_warm_bars"] = sum(b["warmup"] for b in s5_bars if DAY_START < b["time_close"] <= CAPTURE)
    result["s5_day_context_bars"] = sum(b["tv_context_ready"] for b in s5_bars if DAY_START < b["time_close"] <= CAPTURE)
    result["s5_day_core_htf_ready_bars"] = sum(b["core_htf_ready"] for b in s5_bars if DAY_START < b["time_close"] <= CAPTURE)
    result["s5_day_bars"] = sum(DAY_START < b["time_close"] <= CAPTURE for b in s5_bars)
    extended_path = folder / f"missed_sell_S5_M{minutes}_extended.csv"
    if s5_bars and extended_path.is_file():
        result["calculation_range_comparison"] = compare_range(extended_path, minutes, s5_by_close, s5_events)
    result["intel_day_candidates"] = [
        {"event": kind, "grade": grade, "reason": why, "count": n}
        for (kind, grade, why), n in intelligence.items()
    ]
    result["s5_nonquiet_day"] = [
        {
            "close_utc": utc(b["time_close"]), "sell_reason": b["sell_reason"], "buy_reason": b["buy_reason"],
            "position_size": b["position_size"], "events": s5_events[b["time_close"]],
        }
        for b in s5_bars if DAY_START < b["time_close"] <= CAPTURE and s5_events.get(b["time_close"])
    ]
    result["intel_nonquiet_day"] = [
        {"close_utc": utc(close), "events": es}
        for close, es in sorted(intel_events.items()) if DAY_START < close <= CAPTURE
    ]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "reference": REFERENCE.name,
        "reference_sha256": sha256(REFERENCE.read_bytes()).hexdigest(),
        "visible_capture_utc": utc(CAPTURE),
        "price_scope": "OANDA:XAUUSD standard candles. Completed bars only. No inferred rightmost intrabar tick path.",
        "excursion_model": "Next open minus 2 ticks for a short. Commission 0.175 USD per fill per unit. Price stress adds 0.35 USD round-trip spread. Diagnostic stop at last 3 bars' high plus 0.10 prior ATR. 2R take-profit. Adverse-first for both touches. No claim of an executable broker fill.",
        "frames": {str(m): analyze(args.logs_dir, m, args.output_dir) for m in (5, 15)},
    }
    output = args.output_dir / "missed_sell_diagnostic.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(output)


if __name__ == "__main__":
    main()
