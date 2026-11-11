"""Reconstruct the November 11 selloff from pinned native OHLC and decision logs."""

import argparse
from collections import Counter, defaultdict
import csv
from dataclasses import asdict, replace
from hashlib import sha256
import json
from pathlib import Path
from statistics import median

from gold_pro.direct_breakout_research import (
    BASE, STRESS, event_audit, failures, independent_hours, journal, pinned, portfolio, simulate, stats,
)
from gold_pro.direct_trend_breakout import closed_context, scan
from gold_pro.rc1_qa import Context, require
from gold_pro.rc2_research import early_observations
from gold_wick.engine import scan as wick_scan
from research.direct_trend_breakout.forensic_timeline import ms, utc
from research.direct_trend_breakout.shadow_native import event_differences, read_native


FRAMES = (5, 15, 30)
ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
FORENSIC = ROOT / "research/direct_trend_breakout/evidence/forensic-timeline-M15.csv"
FORENSIC_SHA = "b5773bb4c69e265cf06043eb352abe2158ecf48a4353f4804574061ec68d264f"
EVENT_CONFIG = "docs/v21_evidence/event_evaluation_registration.json"
HOUR = 3_600_000
DAY = ms("2026-11-11T00:00:00Z")
START = ms("2026-11-11T11:30:00Z")
END = ms("2026-11-11T16:30:00Z")
DTV_END = ms("2026-11-11T17:30:00Z")
TAGS = ("RC1_CTX", "RC1_CORE", "RC1_READY", "RC2_EARLY", "DTV_STATE", "DTV_PAPER", "DTV_ALERT")
PRICES = ("open", "high", "low", "close")
NAMES = {-1: "SELL", 1: "BUY"}
PINS = {
    5: (
        "e22f352b224e756f71dd692be8ff64c16acf2ffa627aa89afa68317318eb8ebe",
        "76891e77662e48ad032daf40f06a1ee78d621e4ac27c915f9babec436597d6c9",
        "0803ceb364859bf2640472b13479429582372fc80b99dbd32bfd4a12f7e3ad8f",
    ),
    15: (
        "b51d251280824d5b57d739f501b408935a28a8aa0c76111bc470e8df857c8342",
        "a02f8867de9a475b05bd83377505e0c396c9fdb74774afb153f40ae8149632ad",
        "d2b694559f08c93106dbc460c1dc64bb7bb7f7ec8c30f15fdd0df3e008c15860",
    ),
    30: (
        "065fd74c4b3285d32ea75be4a7109efea5ed9e2e6f6348ac4c8bf02e48e303e6",
        "5bdea06ce08b958f50b64cb2c8cd08d1ebeb133b19eaa60d11dcea6bcb323073",
        "6070b4ad0912f5269fe639e17a50f24c8d93cbd18ee464d8ae465e59b6d1562b",
    ),
}


def native_rows(path, frame):
    streams = {tag: [] for tag in TAGS}
    with path.open(encoding="utf-8-sig", newline="") as data:
        for row in csv.DictReader(data):
            tag, _, payload = row["Message"].partition("|")
            if tag == "RC1_CTX":
                streams[tag].append(Context.parse(payload, frame))
            elif tag == "RC2_EARLY":
                schema, symbol, interval, known, side, price = payload.split("|")
                require((schema, symbol, interval) == ("1", "OANDA:XAUUSD", str(frame)), tag)
                streams[tag].append(dict(known_ms=int(known), side={"BUY": 1, "SELL": -1}[side], price=float(price)))
            elif tag in streams:
                streams[tag].append(json.loads(payload))
    return streams


def load_frame(frame, default_dir, probe_dir):
    paths = (default_dir / f"unified-M{frame}.csv", probe_dir / f"baseline-M{frame}.csv",
             probe_dir / f"early-M{frame}.csv")
    default, baseline, enabled = tapes = tuple(read_native(path) for path in paths)
    require(tuple(t.sha256 for t in tapes) == PINS[frame], f"M{frame} native input hash mismatch")
    require(all(t.timeframe == frame for t in tapes), f"M{frame} source timeframe mismatch")
    raw, baseline_rows, early = (native_rows(path, frame) for path in paths)
    require(not raw["RC2_EARLY"] and not baseline_rows["RC2_EARLY"], f"M{frame} default EARLY enabled")
    known = {row["known_ms"]: row for row in raw["DTV_STATE"]}
    require(known.keys() == {row["known_ms"] for row in early["DTV_STATE"]},
            f"M{frame} EARLY comparison requires the same panel scope")
    market = {bar["time"]: bar for bar in default.bars}
    overlaps = {}
    for name, tape in (("fresh_default", baseline), ("early_enabled", enabled)):
        shared = [bar for bar in tape.bars if bar["time"] in market]
        require(shared and all(bar == market[bar["time"]] for bar in shared), f"M{frame} native OHLC mismatch")
        overlaps[name] = len(shared)
    geometry = early_observations(default.bars)
    in_scope = lambda rows: [row for row in rows if row["known_ms"] in known]
    require(in_scope(geometry) == early["RC2_EARLY"], f"M{frame} EARLY geometry differs from native probe")
    decisions, plans = scan(default.bars)
    require(event_differences(default.events, decisions, default.first_index) is None,
            f"M{frame} Direct Trend event-level mismatch")
    return default, raw, early, decisions, plans, overlaps


def archived_forensic():
    require(sha256(FORENSIC.read_bytes()).hexdigest() == FORENSIC_SHA, "M15 forensic input changed")
    with FORENSIC.open(encoding="utf-8", newline="") as data:
        return {ms(row["known_utc"]): row for row in csv.DictReader(data)}


def market_row(bar):
    return {**{key: bar[key] for key in PRICES}, "open_utc": utc(bar["time"]), "known_utc": utc(bar["time_close"])}


def within(rows, field, begin=START, end=END):
    return [row for row in rows if begin <= row[field] <= end]


def keyed(rows, field):
    result = defaultdict(list)
    for row in rows:
        result[row[field]].append(row)
    return result


def compact_event(row, fields):
    result = {}
    for key in fields.split():
        if key in row:
            out_key = key.removesuffix("_ms") + "_utc" if key.endswith("_ms") else key
            result[out_key] = utc(row[key]) if key.endswith("_ms") and row[key] else row[key]
    return result


def structure_edges(bars, index, atr):
    """Recompute the existing six-bar Pine overlay on confirmed OHLC."""
    bar = bars[index]
    previous_close = bars[index - 1]["close"]
    prior = bars[index - 6:index]
    width = max(bar["high"] - bar["low"], BASE.tick)
    low, high = min(b["low"] for b in prior), max(b["high"] for b in prior)
    close, opening = bar["close"], bar["open"]
    bear = close < low <= previous_close and (bar["high"] - close) / width >= .65 and opening - close >= .30 * atr
    bull = close > high >= previous_close and (close - bar["low"]) / width >= .65 and close - opening >= .30 * atr
    return {"prior6_low": low, "prior6_high": high, "confirmed_side": -1 if bear else 1 if bull else 0}


def current_atr(bars, index, prior_atr):
    bar, previous_close = bars[index], bars[index - 1]["close"]
    tr = max(bar["high"] - bar["low"], abs(bar["high"] - previous_close), abs(bar["low"] - previous_close))
    return (13 * prior_atr + tr) / 14


def emit_case_rows(frame, bars, raw, early, dt, wick, wsb):
    groups = [
        keyed(dt, "time_ms"), keyed(wick, "time_ms"), keyed(early["RC2_EARLY"], "known_ms"),
        keyed(raw["RC1_CORE"], "known_time"), keyed(raw["RC1_READY"], "confirmed_ms"),
        keyed(raw["DTV_PAPER"], "known_time"),
    ]
    panel = {row["known_ms"]: row for row in raw["DTV_STATE"]}
    htf = {row.closing: row.htf_closes for row in raw["RC1_CTX"]}
    results = []
    for index, (bar, ctx) in enumerate(zip(bars, closed_context(bars))):
        stamp = bar["time_close"]
        if not START <= stamp <= END:
            continue
        previous = bars[max(0, index - 20):index]
        prior_low = min(row["low"] for row in previous)
        prior_high = max(row["high"] for row in previous)
        shadow, wk, warning, core, ready, paper = (group[stamp] for group in groups)
        display = panel.get(stamp, {})
        forensic = wsb.get(stamp, {}) if frame == 15 else {}
        pivots = {}
        for name in ("confirmed_low", "confirmed_high"):
            if (pivot := getattr(ctx, name)) is not None:
                pivots[name] = {"level": pivot.price, "source_utc": utc(pivot.source_ms), "known_utc": utc(pivot.known_ms)}
        details = {key: display[key] for key in ("direction", "permission", "permission_reason", "paper_phase",
                                                "paper_capacity", "s5_stage", "signals") if key in display}
        if "signals" in details:
            signals = details["signals"]
            require(all(signal["known_ms"] <= stamp for signal in signals), "Panel emitted a future source")
            details["signals"] = [compact_event(signal, "source model stage side birth_ms known_ms expiry_ms research")
                                  for signal in signals]
        close_atr = current_atr(bars, index, ctx.prior_atr)
        row = {
            "frame": frame, **market_row(bar), "loaded_index": index, "prior_atr14": ctx.prior_atr,
            "prior_ema20": ctx.prior_ema, "prior_ema20_slope3": ctx.prior_ema_slope,
            "prior_20_low": prior_low, "prior_20_high": prior_high, "pivots_2_plus_2_at_close": pivots,
            "six_bar_structure_reconstructed": structure_edges(bars, index, close_atr),
            "close_below_prior_20_low": bar["close"] < prior_low,
            "close_above_prior_20_high": bar["close"] > prior_high,
            "wick_below_prior_20_low_close_inside": bar["low"] < prior_low <= bar["close"],
            "bear_body_over_prior_atr": (bar["open"] - bar["close"]) / ctx.prior_atr,
            "range_over_prior_atr": (bar["high"] - bar["low"]) / ctx.prior_atr,
            "closed_htf_utc": [utc(x) if x else None for x in htf.get(stamp, ())],
            "archived_m15_native_bias": [forensic.get("h1_native_bias"), forensic.get("h4_native_bias")],
            "early_enabled": [compact_event(x, "known_ms side price") for x in warning],
            "shadow_native": [compact_event(x, "model state side reason reasons level level_known_ms source_ms watch_ms "
                                             "breakout_ms risk risk_cash extension_atr friction_r net_room_r "
                                             "reference stop targets obstacle") for x in shadow],
            "wick_local": [compact_event(x, "state side model reason top bottom risk_reasons reference stop targets "
                                           "known_barrier net_room_r")
                           for x in wk if x["side"]],
            "core_native": [compact_event(x, "event side plan_id price reason quality quality_note evidence") for x in core],
            "ready_native": [compact_event(x, "direction entry sl tp1 tp2 tp3 reason") for x in ready],
            "paper_native": [compact_event(x, "event side plan_id price effective_time reason cash_pnl net_r") for x in paper],
            "release_panel": details,
            "prior_native_m15_s5_sell_reason": forensic.get("s5_sell_reason"),
            "prior_native_m15_wsb_plans": json.loads(forensic.get("wsb_original_plans", "[]")),
        }
        results.append(row)
    return results


def coverage(rows, events, regimes, begin, end):
    by_key = defaultdict(list)
    stage_names = ("watch", "confirmation", "candidate", "fill")
    for row in rows:
        stamp, side, stage = row
        if begin <= stamp < end:
            by_key[stamp // HOUR * HOUR, side, stage].append(stamp)
    coverage_by_side = {}
    for side, label in NAMES.items():
        directional = [event["start"] for event in events
                       if event["side"] == side and begin <= event["start"] <= end - HOUR]
        other_label = "DIRECTIONAL_" + NAMES[-side]
        stages = {}
        for stage in stage_names:
            latency = [(min(by_key[start, side, stage]) - start) / 60_000
                       for start in directional if (start, side, stage) in by_key]
            off = Counter()
            for (start, polarity, kind), stamps in by_key.items():
                if polarity == side and kind == stage and start not in directional:
                    off[regimes.get(start, "INELIGIBLE_OR_PARTIAL")] += len(stamps)
            stages[stage] = {"events_hit": len(latency), "median_delay_min": median(latency) if latency else None,
                             "outside_same_side_event_decisions": dict(sorted(off.items())),
                             "opposite_directional_decisions": off[other_label]}
        coverage_by_side[label] = {"directional_events": len(directional), **stages}
    return coverage_by_side


def stamps(rows, time, side, mapping, state):
    return [(row[time], row[side], mapping[row[state]]) for row in rows if row[state] in mapping]


def scope_outcomes(outcomes, bars, begin, end):
    """Preserve open campaigns and avoid using post-cohort closes in cohort P&L."""
    selected = []
    for outcome in outcomes:
        known = bars[outcome.plan.bar]["time_close"]
        fill = bars[outcome.entry_bar]["time"] if outcome.entry_bar is not None else None
        if not begin <= known < end and (fill is None or not begin <= fill < end):
            continue
        if fill is not None and fill >= end:
            outcome = replace(outcome, state="CENSORED_PENDING", entry_bar=None, entry=None, exits=())
        elif fill is not None and (outcome.state == "CENSORED_OPEN" or bars[outcome.release]["time_close"] > end):
            boundary = next((i for i in range(outcome.entry_bar, len(bars)) if bars[i]["time_close"] > end), len(bars))
            outcome = replace(outcome, state="CENSORED_OPEN", release=boundary,
                              exits=tuple(part for part in outcome.exits if part["bar"] < boundary))
        selected.append(outcome)
    return selected


def comparison_observations(base_rows, early_rows, structure_rows, events, regimes, begin, end):
    """Deduplicate by side, stage and close. Keep EARLY in the watch stage."""
    stages = {"watch", "confirmation"}
    original = {(t, s, k) for t, s, k in base_rows if k in stages and begin <= t < end}
    early = {(t, s, k) for t, s, k in early_rows if begin <= t < end}
    structure = {(t, s, k) for t, s, k in structure_rows if begin <= t < end}
    expanded = original | early | structure
    incremental = expanded - original
    def first_by_side_stage(rows):
        first = {}
        for t, side, stage in sorted(rows):
            first.setdefault(f"{NAMES[side]} {stage}", utc(t))
        return first

    return {
        "watch_confirmation_original": coverage(original, events, regimes, begin, end),
        "watch_confirmation_expanded": coverage(expanded, events, regimes, begin, end),
        "unique_transition_counts": {
            name: {stage: sum(k == stage for _, _, k in rows) for stage in sorted(stages)}
            for name, rows in (("shadow_plus_wick", original), ("legacy_early", early), ("six_bar_structure", structure),
                               ("expanded_deduplicated", expanded), ("new_after_deduplication", incremental))
        },
        "unique_side_close_observations_before": len({(t, s) for t, s, _ in original}),
        "unique_side_close_observations_after": len({(t, s) for t, s, _ in expanded}),
        "first_original_transition": first_by_side_stage(original),
        "first_expanded_transition": first_by_side_stage(expanded),
        "simultaneous_early_watch_and_six_bar_confirmation": len({(t, s) for t, s, _ in early}
                                                                & {(t, s) for t, s, _ in structure}),
    }


def example_events(events, regimes, bars, shadow_rows, begin, end):
    by_start = {row["start"]: row for row in events}
    examples = {}
    for kind in ("DIRECTIONAL_BUY", "DIRECTIONAL_SELL", "SIDEWAYS", "REVERSAL"):
        choices = [start for start, category in regimes.items() if category == kind and begin <= start < end]
        if not choices:
            continue
        # Pick the first eligible example with a confirmation, otherwise the first eligible hour.
        active = {time // HOUR * HOUR for time, _, stage in shadow_rows if stage == "confirmation"}
        start = next((s for s in choices if s in active), choices[0])
        bucket = [bar for bar in bars if start <= bar["time"] < start + HOUR]
        examples[kind] = {
            "start_utc": utc(start), "first_open": bucket[0]["open"], "last_close": bucket[-1]["close"],
            "range": max(bar["high"] for bar in bucket) - min(bar["low"] for bar in bucket),
            "directional_price_distance": by_start[start]["directional_extreme_distance"] if start in by_start else None,
            "shadow": [{"known_utc": utc(t), "side": NAMES[s], "stage": k}
                       for t, s, k in shadow_rows if start <= t < start + HOUR],
        }
    return examples


def cohorts(frame, bars, raw, early, dt, plans, wick, rules, common_start):
    events, regimes, audit = independent_hours(bars, frame, rules)
    dt_rows = stamps(dt, "time_ms", "side",
                     {"WATCH": "watch", "RETEST WATCH": "watch", "BREAKOUT CONFIRMED": "confirmation",
                      "ENTRY CANDIDATE": "candidate"}, "state")
    wick_rows = stamps(wick.events, "time_ms", "side",
                       {"BUY_WATCH": "watch", "SELL_WATCH": "watch", "RETEST_WATCH": "watch",
                        "BUY_BREAKOUT_CONFIRMED": "confirmation", "SELL_BREAKOUT_CONFIRMED": "confirmation",
                        "ENTRY_VALID": "candidate"}, "state")
    early_rows = [(row["known_ms"], row["side"], "watch") for row in early_observations(bars)]
    native_early_rows = [(row["known_ms"], row["side"], "watch") for row in early["RC2_EARLY"]]
    structure_rows = []
    for index, ctx in enumerate(closed_context(bars)):
        if index < 250:
            continue
        outcome = structure_edges(bars, index, current_atr(bars, index, ctx.prior_atr))
        if outcome["confirmed_side"]:
            structure_rows.append((bars[index]["time_close"], outcome["confirmed_side"], "confirmation"))
    core_rows = stamps(raw["RC1_CORE"], "known_time", "side", {"WATCH": "watch", "CONFIRMED": "confirmation"}, "event")
    core_rows.extend((row["confirmed_ms"], {"BUY": 1, "SELL": -1}[row["direction"]], "candidate")
                     for row in raw["RC1_READY"])
    # Native RAW PAPER records the fill when the containing bar is observed, with a separate effective time.
    core_rows.extend((row["effective_time"], row["side"], "fill")
                     for row in raw["DTV_PAPER"] if row["event"] == "SIM_ENTRY")
    histories = {}
    history_end = DAY
    case_day_end = DTV_END // HOUR * HOUR
    for lane, candidates, observations in (("shadow", plans, dt_rows), ("wick", wick.plans, wick_rows)):
        outcomes = portfolio([plan for plan in candidates if plan.bar >= 250], bars, BASE)
        stress = portfolio([plan for plan in candidates if plan.bar >= 250], bars, STRESS)
        observed = [*observations, *((bars[outcome.entry_bar]["time"], outcome.plan.side, "fill")
                                     for outcome in outcomes if outcome.entry_bar is not None)]
        history_events = [event for event in events if common_start <= event["start"] < history_end]
        report = {
            "history": coverage(observed, events, regimes, common_start, history_end),
            "case_day": coverage(observed, events, regimes, DAY, case_day_end),
            "history_performance": {
                cost: {name: stats(result, bars, common_start, history_end, side)
                       for side, name in ((0, "all"), *NAMES.items())}
                for cost, result in (("base", outcomes), ("stress", stress))
            },
        }
        if lane == "shadow":
            cohort_outcomes = scope_outcomes(outcomes, bars, common_start, history_end)
            report["history_order_disposition"] = dict(Counter(row.state for row in cohort_outcomes))
            report["history_opportunity_capture"] = event_audit(
                history_events, regimes,
                [row for row in dt if common_start <= row["time_ms"] < history_end], cohort_outcomes, bars)
            report["historical_examples"] = example_events(events, regimes, bars, observed, common_start, history_end)
            failed = failures(dt, bars, history_end)
            observed_failures = [row for row in failed["rows"] if common_start <= ms(row["confirm_utc"]) < history_end]
            report["historical_failed_breaks"] = {
                "tested_three_contiguous_closes": len(observed_failures),
                "reclaimed_all_edges": sum(bool(row["all_supporting_edges_reclaimed_by_utc"]) for row in observed_failures),
                "first_four_reclaims": [row for row in observed_failures if row["all_supporting_edges_reclaimed_by_utc"]][:4],
            }
        histories[lane] = report
    histories["early_geometry"] = {
        "history_watch_only": coverage(early_rows, events, regimes, common_start, history_end),
        "case_day_watch_only": coverage(early_rows, events, regimes, DAY, case_day_end),
    }
    histories["s5_raw_native_case_day_only"] = coverage(core_rows, events, regimes, DAY, case_day_end)
    histories["directional_visibility_increment"] = {
        "history": comparison_observations([*dt_rows, *wick_rows], early_rows, structure_rows, events, regimes,
                                           common_start, history_end),
        "case_day": comparison_observations([*dt_rows, *wick_rows], native_early_rows, structure_rows, events, regimes,
                                            DAY, case_day_end),
        "case_window_through_16_30_inclusive": comparison_observations(
            [*dt_rows, *wick_rows], native_early_rows, structure_rows, events, regimes, START, END + 1),
        "native_legacy_shadow_intents_at_observed_closes": {
            "full_panel": dict(Counter(row["side"] for row in raw["DTV_ALERT"])),
            "case": dict(Counter(row["side"] for row in within(raw["DTV_ALERT"], "known_ms"))),
        },
        "legacy_early_native_observations_that_could_trigger_if_opted_in": {
            "full_panel": dict(Counter(NAMES[row["side"]] for row in early["RC2_EARLY"])),
            "case": dict(Counter(NAMES[row["side"]] for row in within(early["RC2_EARLY"], "known_ms"))),
        },
    }
    return {
        "history_window_utc": [utc(common_start), utc(history_end)],
        "case_day_complete_hour_window_utc": [utc(DAY), utc(case_day_end)],
        "eligible_history_regimes": dict(Counter(regime for hour, regime in regimes.items() if common_start <= hour < DAY)),
        "event_label_audit_on_loaded_tape": audit, **histories,
    }


def isolated(plans, bars):
    outcomes = [simulate(plan, bars, BASE) for plan in plans
                if plan.side == -1 and START <= bars[plan.bar]["time_close"] <= END]
    rows = []
    for outcome in outcomes:
        record = journal(outcome, bars)
        if outcome.entry_bar is not None:
            # Exit-bar extrema have unknown ordering; use booked fills on that bar.
            last = min(outcome.release, len(bars))
            highs = [bar["high"] + BASE.spread / 2 for bar in bars[outcome.entry_bar:last]]
            lows = [bar["low"] + BASE.spread / 2 for bar in bars[outcome.entry_bar:last]]
            prices = [part["price"] for part in outcome.exits]
            record["observed_adverse_excursion_price"] = max([0., *(x - outcome.entry for x in highs + prices)])
            record["observed_favorable_excursion_price"] = max([0., *(outcome.entry - x for x in lows + prices)])
            record["closed_net_r"] = record["net_r"] if outcome.state == "CLOSED" else None
            record["closed_net_usd"] = record["realized_usd"] if outcome.state == "CLOSED" else None
        rows.append(record)
    return rows


def frame_summary(frame, tape, raw, early, dt, plans, wick, overlaps, rows, wsb):
    bars = tape.bars
    paper_counts = Counter((row["event"], NAMES[row["side"]]) for row in within(raw["DTV_PAPER"], "known_time"))
    first_after = [bar for bar in bars if bar["time"] >= START]
    local_day = [bar for bar in bars if bar["time"] >= DAY]
    core = within(raw["RC1_CORE"], "known_time")
    shadow = within(dt, "time_ms")
    case = {
        "bar_count_including_start_snapshot": len(rows),
        "start_open": market_row(first_after[0]),
        "bar_containing_4001": market_row(next(bar for bar in first_after if bar["low"] == 4001))
        if any(bar["low"] == 4001 for bar in first_after) else None,
        "bar_containing_4000_755": market_row(next(bar for bar in first_after if bar["low"] == 4000.755)),
        "first_sub_4000_bar": market_row(next(bar for bar in first_after if bar["low"] < 4000)),
        "day_low_in_loaded_tape": market_row(min(local_day, key=lambda bar: bar["low"])),
        "s5_core_events": dict(Counter(f'{NAMES[row["side"]]} {row["event"]}' for row in core)),
        "s5_ready": [{"known_utc": utc(row["confirmed_ms"]), **compact_event(row, "direction entry sl tp1 tp2 tp3")}
                     for row in within(raw["RC1_READY"], "confirmed_ms")],
        "s5_raw_paper_events": {f"{side} {state}": count for (state, side), count in paper_counts.items()},
        "shadow_events": dict(Counter(f'{NAMES[row["side"]]} {row["state"]}' for row in shadow)),
        "native_early_case": [compact_event(row, "known_ms side price") for row in within(early["RC2_EARLY"], "known_ms")],
        "isolated_next_open_shadow_sells": isolated(plans, bars),
        "isolated_next_open_wick_sells": isolated(wick.plans, bars),
        "six_bar_sell_breaks": [{"known_utc": row["known_utc"], "close": row["close"],
                                  "level": row["six_bar_structure_reconstructed"]["prior6_low"]}
                                for row in rows if row["six_bar_structure_reconstructed"]["confirmed_side"] == -1],
        "case_early_coincident_with_same_side_six_bar_break": sum(
            warning["side"] == row["six_bar_structure_reconstructed"]["confirmed_side"]
            for row in rows for warning in row["early_enabled"]),
        "decision_snapshots": [row for row in rows if row["early_enabled"] or row["ready_native"] or row["paper_native"]
                               or any(event["side"] == -1 and event["state"] in {
                                   "WATCH", "BREAKOUT CONFIRMED", "ENTRY CANDIDATE", "REJECTED"
                               } for event in row["shadow_native"])
                               or any(event["side"] == -1 for event in row["wick_local"])],
    }
    if frame == 15:
        case["prior_public_wsb_sell_plans_09_to_16_30"] = [
            {"known_utc": utc(stamp), **plan} for stamp, row in wsb.items()
            for plan in json.loads(row["wsb_original_plans"]) if plan["direction"] == "SELL"
        ]
    return {
        "sha256": dict(zip(("default", "fresh_default", "early_enabled"), PINS[frame])),
        "native_ohlc_bars": len(bars), "first_open_utc": utc(bars[0]["time"]),
        "last_close_utc": utc(bars[-1]["time_close"]), "same_ohlc_overlap_rows": overlaps,
        "native_shadow_events_matched_event_by_event": len(dt),
        "native_panel_rows": len(raw["DTV_STATE"]),
        "native_panel_first_and_last_close_utc": [utc(raw["DTV_STATE"][i]["known_ms"]) for i in (0, -1)],
        "case": case,
    }


def write_timeline(path, rows):
    with path.open("w", encoding="utf-8", newline="") as out:
        writer = csv.DictWriter(out, fieldnames=list(rows[0]))
        writer.writeheader()
        for row in rows:
            writer.writerow({key: json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                             if isinstance(value, (dict, list)) else value for key, value in row.items()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--default-native", type=Path, required=True)
    parser.add_argument("--probe-native", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=OUT / "case-summary.json")
    parser.add_argument("--timeline", type=Path, default=OUT / "case-timeline.csv")
    args = parser.parse_args()
    rules = json.loads(pinned(EVENT_CONFIG))["event"]
    wsb = archived_forensic()
    sources = {frame: load_frame(frame, args.default_native, args.probe_native) for frame in FRAMES}
    for m in FRAMES:
        bars = sources[m][0].bars
        _, regime, _ = independent_hours(bars, m, rules)
        require(regime, f"M{m}: no registered hourly events")
    common_start = max(min(independent_hours(sources[m][0].bars, m, rules)[1]) for m in FRAMES)
    report = {
        "schema": "qirvanta.unified.nov11-missed-move.v1", "symbol": "OANDA:XAUUSD",
        "source_commit": "48b2539", "time_basis": "UTC; decisions on completed closes, fills at distinct effective opens",
        "case_clock_utc": [utc(START), utc(END)],
        "window_note": "First and last close included; first row is the pre-decline snapshot of the preceding bar.",
        "scope_note": "Historical cohort is in-sample research. Raw native S5 states only cover the case day. "
                      "Wick and all isolated next-open results are offline research. Native EARLY probe is not alert delivery.",
        "costs": {"base": asdict(BASE), "stress": asdict(STRESS)},
        "frames": {},
    }
    timeline = []
    for frame, (tape, raw, early, dt, plans, overlaps) in sources.items():
        bars = tape.bars
        wick = wick_scan(bars, minutes=frame)
        rows = emit_case_rows(frame, bars, raw, early, dt, wick.events, wsb)
        timeline.extend(rows)
        evidence = frame_summary(frame, tape, raw, early, dt, plans, wick, overlaps, rows, wsb)
        evidence["event_research"] = cohorts(frame, bars, raw, early, dt, plans, wick, rules, common_start)
        report["frames"][f"M{frame}"] = evidence
    m5 = {bar["time"]: bar for bar in sources[5][0].bars}
    for frame in (15, 30):
        checked = 0
        for bar in sources[frame][0].bars:
            if not START <= bar["time"] < END:
                continue
            parts = [m5[stamp] for stamp in range(bar["time"], bar["time_close"], 300_000)]
            combined = (parts[0]["open"], max(x["high"] for x in parts), min(x["low"] for x in parts), parts[-1]["close"])
            require(combined == tuple(bar[key] for key in PRICES), f"M{frame} aggregation at {utc(bar['time'])}")
            checked += 1
        report["frames"][f"M{frame}"]["case_ohlc_bars_matched_to_native_m5"] = checked
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    write_timeline(args.timeline, timeline)
    print(f"Wrote {len(timeline)} case timeline rows and {len(sources)} frame summaries.")


if __name__ == "__main__":
    main()

