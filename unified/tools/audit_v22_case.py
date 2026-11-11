"""Audit the frozen November 10 screenshot from V2.2 native Pine exports."""

import argparse
from collections import Counter
import csv
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
import gzip
import json
from pathlib import Path

from diagnostics.accounting.audit import audit as audit_journal, json_default, read_jsonl
from research.data.loader import validate_bars
from research.validation.native_report import utc
from unified.tools.audit_v22_intelligence import Bar, fingerprint, number, records
from unified.tools.build_tradingview_final import sha
from unified.tools.evaluate_v21_impulse import excursion, write_json
from unified.tools.evaluate_v22_execution import make_plan, simulate
from unified.tools.evaluate_v22_history import REGISTRATION, stress
from unified.tools.evaluate_v22_signals import Candidate, MODELS


def timestamp(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


START = timestamp("2026-11-09T18:00:00Z")
DAY_START = timestamp("2026-11-10T00:00:00Z")
SCREEN = timestamp("2026-11-10T14:52:12Z")


def load_indicator(path, minutes):
    entries = list(records(path))
    bar_records = [Bar.parse(t, p) for kind, t, p in entries if kind == "BAR"]
    snapshots = [(t, p) for kind, t, p in entries if kind == "SNAPSHOT"]
    if len(snapshots) != 1 or not bar_records:
        raise ValueError(f"Complete indicator BAR and SNAPSHOT exports required in {path.name}")
    last, summary = snapshots[0]
    symbol, frame = summary[:2]
    first, last_open, count, hash1, hash2 = map(int, summary[2:7])
    tick = Decimal(summary[11])
    if (symbol, frame, first, last_open, count, last) != (
            "OANDA:XAUUSD", str(minutes), bar_records[0].open_time, bar_records[-1].open_time,
            len(bar_records), bar_records[-1].known):
        raise ValueError(f"Native indicator snapshot coverage differs in {path.name}")
    if fingerprint(bar_records, tick) != (hash1, hash2) or summary[12:16] != ["1", "1", "1", "0"]:
        raise ValueError(f"OHLC fingerprint or default model settings differ in {path.name}")
    bars = [{"time": b.open_time, "time_close": b.known, "open": b.open, "high": b.high, "low": b.low,
             "close": b.close} for b in bar_records]
    if validate_bars(bars) != minutes * 60_000:
        raise ValueError("Unexpected chart period")
    return entries, bar_records, bars, tick, (hash1, hash2)


def read_strategy(path, minutes, cutoff):
    data, _ = read_jsonl(path)
    observed = audit_journal(data)
    run = observed["run"]
    if (observed["status"], run["run_id"], run["symbol"], run["spread_budget_price"],
        run["reject_htf_conflict"], run["require_displacement"], run["orders_enabled"]) != (
            "PASS_OBSERVED_JOURNAL", f"v22|ABC|A|{minutes}", "OANDA:XAUUSD", Decimal(".35"),
            True, False, True) or run["cutoff"] < cutoff:
        raise ValueError(f"Comparable fixed ABC journal required in {path.name}")
    return observed


def observed_confirmation(t, payload, positions):
    model, side, setup_id, watched, level, ref, stop, barrier, atr, ext, spike, conflict = payload
    return Candidate(model, int(side), positions[t], t, int(watched), float(level), float(stop), float(ref),
                     float(atr), float(ext), number(barrier), spike == "1", conflict == "1"), setup_id


def format_lifecycle(log):
    return {k: log[k] for k in ("state", "model", "side", "id", "reason", "known_at", "effective_at", "price")}


def scenario(c, plan, bars, minutes, execution, tick, pointvalue, extreme_distance, next_open):
    if plan.risk_price < float(tick):
        return {"status": "INVALID_STOP_GEOMETRY"}
    if c.index + 1 >= len(bars):
        return {"status": "NO_COMPLETE_POST_ENTRY_BAR_BY_SCREENSHOT",
                "visible_next_open": next_open}
    trade = simulate(plan, bars, [None] * len(bars), execution, float(tick), pointvalue, minutes, "FIXED")
    movement = excursion(trade, {"last_bar": len(bars) - 1, "directional_extreme_distance": extreme_distance}, bars)
    closed = trade.complete
    return {
        "status": "CLOSED_HYPOTHETICAL" if closed else "CENSORED_AT_SCREENSHOT",
        "known_confirmation": c.time, "entry_bar_open": trade.entry_time, "entry_price_after_slippage": trade.entry_price,
        "confirmation_to_entry_open_minutes": (trade.entry_time - c.time) / 60_000,
        "actual_initial_risk_usd": trade.initial_risk, "gap_invalid_at_fill": trade.gap_invalid,
        "exit_parts": [asdict(part) for part in trade.exits],
        "fee_usd_on_realized_parts": sum((part.commission for part in trade.exits), Decimal(0)),
        "net_realized_parts_usd": trade.cash,
        "full_campaign_net_usd": trade.cash if closed else None,
        "full_campaign_stressed_net_usd": trade.cash - stress(trade, execution, pointvalue) if closed else None,
        "favorable_excursion_price": movement["favorable"], "adverse_excursion_price": movement["adverse"],
        "favorable_fraction_of_observed_peak_to_trough": movement["capture_fraction"],
    }


def case_frame(folder, minutes, execution, indicator_prefix, strategy_prefix, lifecycle_name):
    indicator_path = folder / f"{indicator_prefix}M{minutes}.csv"
    strategy_path = folder / f"{strategy_prefix}M{minutes}_ABC_A.csv"
    entries, all_rows, all_bars, tick, checksums = load_indicator(indicator_path, minutes)
    source = all_rows[0].open_time
    if source > START:
        raise ValueError("Indicator export lacks November 9 lead-in")
    bars = [b for b in all_bars if b["time_close"] <= SCREEN]
    rows = all_rows[:len(bars)]
    cutoff = bars[-1]["time_close"]
    pointvalue = Decimal(1)
    strategy = read_strategy(strategy_path, minutes, cutoff)
    if strategy["run"]["mintick"] != tick or strategy["run"]["pointvalue"] != pointvalue:
        raise ValueError("Native strategy and indicator instrument properties differ")
    admitted = {c["entry_id"]: c for c in strategy["campaigns"]}
    pending = {p["entry_id"] for p in strategy["submitted_without_recorded_fill"] if p["submitted_time"] <= cutoff}
    eligible = [r for r in rows if r.open_time >= START]
    peak = max(eligible, key=lambda b: b.high)
    low = min((r for r in eligible if r.open_time >= peak.open_time), key=lambda b: b.low)
    bearish = next((b for b in eligible if b.known > peak.known and b.structure in ("BOS DOWN", "MSS DOWN")), None)
    span = peak.high - low.low
    positions = {b["time_close"]: i for i, b in enumerate(bars)}
    candidate_rows, timeline = [], []
    lifecycle = []
    lifecycle_path = folder / lifecycle_name.format(minutes=minutes)
    if lifecycle_path.is_file():
        with lifecycle_path.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream):
                message = row["Message"].lstrip()
                if not message.startswith("{"):
                    continue
                value = json.loads(message)
                if (value.get("schema") == "qirvanta.v22.lifecycle.v1"
                        and value.get("run_id") == f"v22|ABC|A|{minutes}"
                        and peak.known <= value["known_at"] <= cutoff):
                    lifecycle.append(format_lifecycle(value))
    for kind, time, payload in entries:
        if not peak.known <= time <= cutoff:
            continue
        if kind == "OBS":
            model, side, setup_id, state, reason, level = payload
            timeline.append({"time": time, "model": model, "side": int(side), "id": setup_id,
                             "state": state, "reason": reason, "level": number(level)})
        elif kind == "CANDIDATE":
            c, setup_id = observed_confirmation(time, payload, positions)
            plan, failures = make_plan(c, execution, float(tick), pointvalue)
            if plan.id != f"{setup_id}|{time}":
                raise ValueError("Indicator setup cannot be reconciled to strategy ID")
            matched = admitted.get(plan.id)
            native_state = ("MATCHED_NATIVE_SUBMISSION" if matched else "NATIVE_SUBMISSION_WITHOUT_RECORDED_FILL"
                            if plan.id in pending else "NO_MATCHING_NATIVE_SUBMISSION")
            next_bar = all_bars[c.index + 1] if c.index + 1 < len(all_bars) else None
            next_open = next_bar["open"] if next_bar and next_bar["time"] <= SCREEN else None
            followup = scenario(c, plan, bars, minutes, execution, tick, pointvalue, span, next_open)
            native_case_state = [l for l in lifecycle if l["id"] in (setup_id, plan.id)]
            seen_parts = ([p for p in matched["exit_rows"] if p["exit_time"] + minutes * 60_000 <= cutoff]
                          if matched else [])
            context = rows[c.index]
            actual_reasons = [l["reason"] for l in native_case_state
                              if l["state"] == "REJECTED" and l["known_at"] == time]
            native_valid = any(l["state"] == "ENTRY_VALID" and l["known_at"] == time for l in native_case_state)
            candidate_rows.append({"confirmation": asdict(c), "plan": asdict(plan),
                                   "indicator_htf_context": {
                                       "h1_known": context.h1_at, "h1_bias": context.h1_bias,
                                       "h4_known": context.h4_at, "h4_bias": context.h4_bias,
                                       "h1_confirmed_values": context.h1_values, "h4_confirmed_values": context.h4_values,
                                   }, "primary_geometry_gate":
                                   failures[0] if failures else None, "all_geometry_gate_failures": failures,
                                   "native_strategy_match": native_state, "native_lifecycle": native_case_state,
                                   "native_risk_gate_pass_observed": native_valid if native_case_state else None,
                                   "native_rejection_reasons": actual_reasons,
                                   "native_fill_known_by_screen": (matched["actual_entry"] if matched and
                                       matched["entry_time"] + minutes * 60_000 <= cutoff else None),
                                   "native_exit_parts_known_by_screen": seen_parts, "hypothetical_fixed_path": followup})
    sell = [r for r in candidate_rows if r["confirmation"]["side"] == -1]
    highlights = {}
    for model in MODELS:
        items = [r for r in sell if r["confirmation"]["model"] == model]
        watches = [r for r in timeline if r["model"] == model and r["side"] == -1 and r["state"] == "WATCH"]
        highlights[model] = {
            "first_watch": watches[0] if watches else None,
            "first_confirmation": items[0] if items else None,
            "first_risk_valid_indicator_candidate": next((r for r in items if not r["all_geometry_gate_failures"]), None),
            "first_risk_rejected_indicator_candidate": next((r for r in items if r["all_geometry_gate_failures"]), None),
            "first_native_entry_valid": next((r for r in items if r["native_risk_gate_pass_observed"]), None),
            "first_native_rejected_confirmation": next((r for r in items if r["native_rejection_reasons"]), None),
            "first_exact_native_admission_of_an_indicator_candidate": next(
                (r for r in items if r["native_strategy_match"] != "NO_MATCHING_NATIVE_SUBMISSION"), None),
            "confirmations": len(items),
            "primary_geometry_gate_counts": dict(Counter(r["primary_geometry_gate"] for r in items
                                                         if r["primary_geometry_gate"])),
            "native_rejection_counts": dict(Counter(reason for r in items for reason in r["native_rejection_reasons"])),
        }
    native_sales = [
        {"model": {"1": "A", "2": "B", "3": "C"}[str(c["family"])],
         "id": c["entry_id"], "submission": c["submitted_metadata"],
         "entry_time": c["entry_time"] if c["entry_time"] + minutes * 60_000 <= cutoff else None,
         "actual_entry": c["actual_entry"] if c["entry_time"] + minutes * 60_000 <= cutoff else None,
         "native_parts_known_by_screen": [p for p in c["exit_rows"] if p["exit_time"] + minutes * 60_000 <= cutoff]}
        for c in strategy["campaigns"] if c["side"] == -1 and
        peak.known <= c["submitted_metadata"]["submitted_time"] <= cutoff
    ]
    return ({
        "timeframe": minutes, "screen_last_confirmed_close": cutoff, "observed_from": source,
        "observed_last_close_in_export": all_rows[-1].known, "indicator_ohlc_fingerprints": checksums,
        "peak": {"price": peak.high, "open": peak.open_time, "known": peak.known},
        "trough": {"price": low.low, "open": low.open_time, "known": low.known},
        "peak_to_trough_price": round(span, 9),
        "first_native_bearish_structure_after_peak": asdict(bearish) if bearish else None,
        "model_firsts": highlights, "native_sell_submissions_in_case": native_sales,
        "strategy_first_open": strategy["run"]["first_bar_time"],
        "strategy_bar_count": strategy["run"]["bar_count"],
        "source_hashes": {path.name: sha(path.read_bytes()) for path in (indicator_path, strategy_path, lifecycle_path)
                          if path.is_file()},
    }, {"native_indicator_bar_trace": [asdict(r) for r in eligible], "native_indicator_observations": timeline,
        "candidate_geometry_and_path": candidate_rows, "native_strategy_lifecycle": lifecycle})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs-dir", type=Path, required=True)
    parser.add_argument("--indicator-prefix", default="indicator_final_")
    parser.add_argument("--strategy-prefix", default="strategy_")
    parser.add_argument("--lifecycle-name", default="strategy_M{minutes}_lifecycle.csv")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tapes", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.tapes.exists():
        parser.error("Use new output filenames")
    reg = json.loads(REGISTRATION.read_bytes())
    report, tape = {}, {}
    for minutes in reg["frames_minutes"]:
        report[str(minutes)], tape[str(minutes)] = case_frame(
            args.logs_dir, minutes, reg["execution"], args.indicator_prefix, args.strategy_prefix, args.lifecycle_name)
    raw = json.dumps(tape, default=json_default, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    with args.tapes.open("xb") as stream:
        stream.write(gzip.compress(raw, compresslevel=9, mtime=0))
    write_json(args.output, {"schema": "qirvanta.v22.screenshot-case.v1", "screenshot_clock_utc": utc(SCREEN).isoformat(),
                             "status": "NATIVE_INDICATOR_AND_STRATEGY_SEPARATE_HYPOTHETICAL_PATHS",
                             "scenario_basis": "Risk gates use native indicator values and fixed strategy defaults. Paths are "
                                 "diagnostic counterfactuals on complete chart candles known by the screenshot, with fees "
                                 "and two ticks of market and stop slippage. Rejected candidates were never enabled. "
                                 "Native strategy and indicator may load different warmup ranges.",
                             "tape_sha256": sha(args.tapes.read_bytes()), "frames": report})
    print(f"Wrote {len(report)} native screenshot-case frame audits.")


if __name__ == "__main__":
    main()
