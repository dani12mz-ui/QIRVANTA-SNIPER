"""Compare immutable archived S5 M5/M15 controls over identical OANDA history."""

import argparse
from collections import Counter
from dataclasses import replace
from gzip import compress
import json
from pathlib import Path
from statistics import mean, median

from gold_pro.archive import native_outcomes
from gold_pro.direct_breakout_research import (
    BASE, EVENT_CONFIG, FORWARD_SEAL, STRESS, WARMUP, independent_hours, journal, pinned, portfolio,
)
from gold_pro.direct_trend_breakout import scan
from research.data.loader import load_bars
from research.direct_trend_breakout.forensic_timeline import utc
from research.m5_lead_entry_window.evaluate import exact_aggregation
from research.market_coverage_entry_quality.audit_baseline import native_campaign_summary, rc2_trace, s5_hour
from research.market_coverage_entry_quality.entry_quality import metrics as quote_metrics
from research.market_coverage_entry_quality.radar import clock, detection, lifecycle, warning_clocks
from research.market_coverage_entry_quality.shadow_trace import trace
from research.professional_signal_selector.evaluate import checked
from research.validation.statistics import cash_metrics, quantile
from unified.tools.audit_premium_s5 import verify_bars
from unified.tools.audit_tv_observations import inspect


ROOT = Path(__file__).resolve().parents[2]
REGISTRATION = ROOT / "docs/premium_evidence/s5_screen_registration.json"
REGISTRATION_SHA = "9ef45b538670332e797e5bcbb0660b912f910c54fdf94fab3eb899f5165e6d97"
FRAMES = (5, 15)
COSTS = {"normal": BASE, "stress": STRESS}
SIDES = {1: "BUY", -1: "SELL"}
OHLC = ("open", "high", "low", "close")
COUNTS = {"BUY": 21, "SELL": 12}
PRE_HOUR = 15 * 60_000


def load_inputs(data_root, native_dir):
    registry = json.loads(checked(REGISTRATION, REGISTRATION_SHA))
    result = {}
    for frame in FRAMES:
        item = registry["frames"][str(frame)]
        path = data_root / Path(item["local_bars"]).parent.name / "bars.csv"
        checked(path, item["bars_sha256"])
        bars = load_bars(path)
        source = native_dir / item["log_name"]
        checked(source, item["log_sha256"])
        native = inspect(source)
        run = native["run"]
        if native["mode"] != "S5" or run["timeframe"] != str(frame) or native["unfilled"]:
            raise ValueError(f"M{frame}: incomplete or incompatible original S5 tape")
        verify_bars(bars, run)
        result[frame] = (bars, native, item)
    return result


def price_ohlc(bars):
    return (bars[0]["open"], max(row["high"] for row in bars),
            min(row["low"] for row in bars), bars[-1]["close"])


def pairing(inputs):
    m5, m15 = (inputs[frame][0] for frame in FRAMES)
    matched = exact_aggregation(m5, m15)
    if matched != 1855:
        raise ValueError(f"Expected 1855 identical aggregated provider bars, got {matched}")
    start = max(m5[WARMUP]["time"], m15[WARMUP]["time"])
    end = min(m5[-1]["time_close"], m15[-1]["time_close"])
    pivot = m5[WARMUP + (len(m5) - WARMUP) * 3 // 5]["time"]
    if not start < pivot < end:
        raise ValueError("Original M5 descriptive split not in common window")
    bounds = {"full": (start, end), "early_60": (start, pivot), "later_40": (pivot, end)}
    rules = json.loads(pinned(EVENT_CONFIG))["event"]
    m5_events, categories, catalog = independent_hours(m5, 5, rules)
    events = [row for row in m5_events if start <= row["start"] and row["end"] <= end]
    if dict(Counter(SIDES[row["side"]] for row in events)) != COUNTS:
        raise ValueError("Independent common hours differ from the published archive")
    by_open = {bar["time"]: bar for bar in m15}
    for hour in events:
        minute15 = [by_open.get(stamp) for stamp in range(hour["start"], hour["end"], 900_000)]
        minute5 = m5[hour["first_bar"]:hour["last_bar"] + 1]
        if any(bar is None for bar in minute15) or price_ohlc(minute5) != price_ohlc(minute15):
            raise ValueError(f"Independent hour has inconsistent provider OHLC at {utc(hour['start'])}")
    return matched, bounds, events, categories, catalog


def s5_native_statistics(native, bars, bounds):
    result = {}
    campaigns = native["campaigns"]
    for window, (start, end) in bounds.items():
        result[window] = {}
        for label, side in (("all", 0), ("BUY", 1), ("SELL", -1)):
            filled = [row for row in campaigns if start <= row["entry_time"] < end and (not side or row["side"] == side)]
            closed = [(max(part["exit_bar_index"] for part in row["exit_rows"]), row)
                      for row in filled if row["state"] == "COMPLETE"]
            closed = [row for index, row in sorted(closed, key=lambda pair: (pair[0], pair[1]["plan_id"]))
                      if bars[index]["time_close"] <= end]
            cash = [float(row["realized_net_cash"]) for row in closed]
            rs = [float(row["net_r"]) for row in closed if row["net_r"] is not None]
            commissions = sum(float(row["realized_commission_cash"]) for row in closed)
            result[window][label] = {
                **cash_metrics(cash), "filled": len(filled), "closed": len(closed),
                "open_or_straddling": len(filled) - len(closed),
                "realized_gross_cash_usd": sum(float(row["realized_gross_cash"]) for row in closed),
                "realized_commission_usd": commissions, "native_net_r": sum(rs),
                "native_expectancy_r": mean(rs) if rs else None, "native_r_sample_size": len(rs),
                "native_worst_r": min(rs) if rs else None, "native_p05_r": quantile(rs, .05),
            }
    return result


def quote_replays(plans, bars, bounds):
    start, end = bounds["full"]
    replay = {}
    for name, costs in COSTS.items():
        # Preserve pre-window campaign state by replaying all originally admitted plans.
        outcomes = portfolio(plans, bars, costs, use_s5_lock=True)
        relevant = [x for x in outcomes if start <= bars[x.plan.bar]["time_close"] < end
                    or (x.entry_bar is not None and bars[x.entry_bar]["time"] < end
                        and (x.release >= len(bars) or bars[x.release]["time_close"] >= start))]
        counts = dict(Counter(row.state for row in relevant))
        replay[name] = {
            "metrics": quote_metrics(outcomes, bars, costs, bounds),
            "same_interval_outcome_states": counts,
            "journal": [
                {**journal(row, bars), "known_ms": bars[row.plan.bar]["time_close"],
                 "effective_entry_ms": bars[row.entry_bar]["time"] if row.entry_bar is not None else None,
                 "terminal_known_ms": bars[row.release]["time_close"] if row.release < len(bars) else None}
                for row in relevant
            ],
        }
    return replay


def shadow_record(event):
    keys = ("time_ms", "side", "model", "state", "reason", "reasons", "level", "prior_atr",
            "plan_id", "reference", "stop", "targets", "obstacle", "net_room_r", "risk_cash")
    return {
        **{key: event[key] for key in keys if key in event},
        "known_utc": utc(event["time_ms"]), "level_known_utc": utc(event["level_known_ms"]),
    }


def observation_audit(bars, frame, native, events, categories, bounds):
    begin, end = bounds["full"]
    identity = [{"start_ms": row["start"], "side": row["side"]} for row in events]
    rc2_rows, rc2_events = rc2_trace(bars)
    early_clocks = warning_clocks(rc2_events, (), begin, end)
    shadow, _ = scan(bars, BASE)
    _, continued, _ = lifecycle(bars, shadow)
    all_clocks = warning_clocks(rc2_events, shadow, begin, end)
    rc2, rc2_aggregate = detection(early_clocks, identity, categories)
    radar, radar_aggregate = detection(all_clocks, identity, categories)
    shadow_traces = trace(bars, shadow, events)
    hours = []
    for hour in events:
        start, stop, side = (hour[key] for key in ("start", "end", "side"))
        same = [row for row in shadow if row["side"] == side and start <= row["time_ms"] < stop]
        confirmed = [row for row in same if row["state"] == "BREAKOUT CONFIRMED"]
        proposed = [row for row in same if row["state"] == "ENTRY CANDIDATE"]
        rejected = [row for row in same if row["state"] == "REJECTED"]
        closed_trace = [
            {"known_ms": row["known_ms"], "known_utc": utc(row["known_ms"]), "prior_atr14": row["atr_previous"],
             "directional_body": side * row["body_signed_buy"], "close": row["close"], **row["decisions"][side]}
            for row in rc2_rows if start - PRE_HOUR <= row["known_ms"] < stop
        ]
        market = {key: hour[key] for key in OHLC}
        hours.append({
            "start_ms": start, "start_utc": utc(start), "end_ms": stop, "end_utc": utc(stop),
            "side": side, "direction": SIDES[side], "market": market,
            "existing_radar": radar[start], "rc2_early": rc2[start], "rc2_gate_trace": closed_trace,
            "shadow": {
                "first_same_side_confirmed": shadow_record(confirmed[0]) if confirmed else None,
                "first_same_side_diagnostic_geometry": shadow_record(proposed[0]) if proposed else None,
                "same_side_independent_continuations": [
                    shadow_record(row) | {"linked_origin": continued[clock(row)]}
                    for row in confirmed if clock(row) in continued
                ],
                "rejected_known": [shadow_record(row) for row in rejected],
                "causal_bar_trace": shadow_traces[start],
                "rejection_reasons": dict(Counter(reason for row in rejected
                                                  for reason in row.get("reasons", (row["reason"],)))),
                "stage_reached": "DIAGNOSTIC_GEOMETRY" if proposed else "CONFIRMED_WITHOUT_GEOMETRY" if confirmed
                                 else "NO_CONFIRMED_BREAK",
            },
            "native_s5": s5_hour(native["campaigns"], bars, hour),
        })
    return {"frame_minutes": frame, "by_side": summarize_hours(hours), "rc2": rc2_aggregate,
            "union": radar_aggregate, "hours": hours}


def summarize_hours(hours):
    aggregate = {}
    for label in ("all", "BUY", "SELL"):
        group = [row for row in hours if label == "all" or row["direction"] == label]
        times = [row["existing_radar"]["lead_minutes"] for row in group if row["existing_radar"]["first"]]
        aggregate[label] = {
            "significant_hours": len(group), "union_detection": len(times), "median_union_latency_minutes": median(times) if times else None,
            "rc2_detection": sum(row["rc2_early"]["first"] is not None for row in group),
            "shadow_confirm": sum(row["shadow"]["first_same_side_confirmed"] is not None for row in group),
            "shadow_diagnostic_geometry": sum(row["shadow"]["first_same_side_diagnostic_geometry"] is not None for row in group),
            "native_s5_submitted": sum(row["native_s5"]["same_side_submission_count"] > 0 for row in group),
            "native_s5_filled": sum(row["native_s5"]["same_side_fill_count"] > 0 for row in group),
            "native_s5_opposite_filled": sum(row["native_s5"]["opposite_side_fill_count"] > 0 for row in group),
            "native_s5_any_position_carried": sum(bool(row["native_s5"]["carried_into_hour"]) for row in group),
        }
    return aggregate


def execution_hour_lookup(hours, scenarios):
    for name, replay in scenarios.items():
        for row in hours:
            start, end, side = row["start_ms"], row["end_ms"], row["side"]
            row.setdefault("quote_side_s5_replay", {})[name] = {
                "same_side_proposals_in_hour": [item["id"] for item in replay["journal"] if item["side"] == side
                                                and start <= item["known_ms"] < end],
                "same_side_fills_in_hour": [item["id"] for item in replay["journal"] if item["side"] == side
                                           and item["effective_entry_ms"] is not None and start <= item["effective_entry_ms"] < end],
                "first_same_side_fill": min((item for item in replay["journal"] if item["side"] == side
                                             and item["effective_entry_ms"] is not None
                                             and start <= item["effective_entry_ms"] < end),
                                            key=lambda item: item["effective_entry_ms"], default=None),
                "opposite_side_fills_in_hour": [item["id"] for item in replay["journal"] if item["side"] != side
                                               and item["effective_entry_ms"] is not None and start <= item["effective_entry_ms"] < end],
                "same_side_proposal_dispositions": [
                    {"id": item["id"], "state": item["state"], "known_ms": item["known_ms"],
                     "known_utc": utc(item["known_ms"]), "blocked_by": item["blocked_by"]}
                    for item in replay["journal"] if item["side"] == side and start <= item["known_ms"] < end
                ],
            }


def paired_availability(frames):
    m5, m15 = (frames[f"M{frame}"]["observations"]["hours"] for frame in FRAMES)
    counts = {}
    tests = {
        "rc2_observation": lambda row: row["rc2_early"]["first"] is not None,
        "union_observation": lambda row: row["existing_radar"]["first"] is not None,
        "shadow_in_hour_confirmation": lambda row: row["shadow"]["first_same_side_confirmed"] is not None,
        "shadow_diagnostic_geometry": lambda row: row["shadow"]["first_same_side_diagnostic_geometry"] is not None,
        "native_s5_fill": lambda row: row["native_s5"]["same_side_fill_count"] > 0,
        "normal_replay_s5_fill": lambda row: bool(row["quote_side_s5_replay"]["normal"]["same_side_fills_in_hour"]),
        "stress_replay_s5_fill": lambda row: bool(row["quote_side_s5_replay"]["stress"]["same_side_fills_in_hour"]),
    }
    for label, test in tests.items():
        outcome = Counter()
        for left, right in zip(m5, m15):
            if (left["start_ms"], left["side"]) != (right["start_ms"], right["side"]):
                raise ValueError("M5/M15 evidence no longer has identical hour identities")
            both = (test(left), test(right))
            outcome["both" if both == (True, True) else "M5_only" if both[0]
                    else "M15_only" if both[1] else "neither"] += 1
        counts[label] = dict(outcome)
    return counts


def s5_frame_evidence(bars, native, bounds, observations):
    start, end = bounds["full"]
    plans = [replace(item.plan, model="S5_RAW") for item in native_outcomes(native, BASE)]
    replay = quote_replays(plans, bars, bounds)
    execution_hour_lookup(observations["hours"], replay)
    return {
        "observations": observations,
        "s5_native_observed": {
            "metrics": s5_native_statistics(native, bars, bounds),
            "native_campaigns_full_tape": len(native["campaigns"]),
            "journal": [native_campaign_summary(row, bars, start) for row in native["campaigns"]
                        if start <= row["entry_time"] < end],
        },
        "s5_quote_side_replay": replay,
    }


def frame_own_m15(inputs, paired_bounds, paired_events):
    bars, native, _ = inputs[15]
    start = bars[WARMUP]["time"]
    paired_start, end = paired_bounds["full"]
    split = bars[WARMUP + (len(bars) - WARMUP) * 3 // 5]["time"]
    bounds = {"full": (start, end), "early_60": (start, split), "later_40": (split, end),
              "before_paired_score_window": (start, paired_start), "within_paired_score_window": (paired_start, end)}
    rules = json.loads(pinned(EVENT_CONFIG))["event"]
    own_events, categories, catalog = independent_hours(bars, 15, rules)
    events = [row for row in own_events if start <= row["start"] and row["end"] <= end]
    observed = observation_audit(bars, 15, native, events, categories, bounds)
    result = s5_frame_evidence(bars, native, bounds, observed)
    paired_ids = {(row["start"], row["side"]) for row in paired_events}
    m15_ids = {(row["start"], row["side"]) for row in events if paired_start <= row["start"]}
    full_tape = {"full": (bars[0]["time"], end)}
    result["s5_native_observed"]["ungated_full_tape_metrics"] = s5_native_statistics(native, bars, full_tape)["full"]
    result["s5_native_observed"]["ungated_full_tape_journal"] = [
        native_campaign_summary(row, bars, bars[0]["time"]) for row in native["campaigns"]]
    for name, replay in quote_replays(
            [replace(item.plan, model="S5_RAW") for item in native_outcomes(native, BASE)], bars, full_tape).items():
        result["s5_quote_side_replay"][name]["ungated_full_tape_metrics"] = replay["metrics"]["full"]
        result["s5_quote_side_replay"][name]["ungated_full_tape_journal"] = replay["journal"]
    return {
        **result,
        "scored_windows_utc": {key: [utc(a), utc(b)] for key, (a, b) in bounds.items()},
        "frame_own_hour_catalog": catalog,
        "before_paired_window_observations": summarize_hours(
            [row for row in observed["hours"] if row["end_ms"] <= paired_start]),
        "within_paired_window_observations": summarize_hours(
            [row for row in observed["hours"] if row["start_ms"] >= paired_start]),
        "m15_vs_m5_independent_label_identities_within_common_time": {
            "both": len(m15_ids & paired_ids), "m15_only": len(m15_ids - paired_ids),
            "m5_only": len(paired_ids - m15_ids),
            "note": "A frame-own M15 label has its own 250-bar and preceding 24 complete M15-hour eligibility. "
                    "The main 33-hour paired comparison remains based on the registered M5 labeling.",
        },
    }


def evaluate(data_root, s5_dir):
    inputs = load_inputs(Path(data_root), Path(s5_dir))
    seal_start = json.loads(pinned(FORWARD_SEAL))["forward_start_ms"]
    if max(bars[-1]["time_close"] for bars, _, _ in inputs.values()) >= seal_start:
        raise ValueError("Protected forward market data outside the study")
    matched, bounds, events, categories, catalog = pairing(inputs)
    frames = {}
    for frame, (bars, native, item) in inputs.items():
        observed = observation_audit(bars, frame, native, events, categories, bounds)
        frames[f"M{frame}"] = {
            "raw_inputs": {"ohlc_sha256": item["bars_sha256"], "native_log_sha256": item["log_sha256"],
                           "native_ohlc_fingerprints": [native["run"]["checksum1"], native["run"]["checksum2"]],
                           "chart_bars": len(bars), "loaded_first_utc": utc(bars[0]["time"]),
                           "last_close_utc": utc(bars[-1]["time_close"])},
            **s5_frame_evidence(bars, native, bounds, observed),
        }
    return {
        "schema": "qirvanta.coverage-data-readiness.controls.v1",
        "inputs": {"registration_sha256": REGISTRATION_SHA, "symbol": "OANDA:XAUUSD",
                   "calendar": "UTC", "native_s5_reference_commit": "6221eb63be02c70db1d6acae6d03745db9a45c94"},
        "overlap": {"exact_complete_m5_to_m15_aggregates": matched,
                    "scored_windows_utc": {name: [utc(a), utc(b)] for name, (a, b) in bounds.items()}},
        "independent_common_hours": {**catalog, "significant": COUNTS,
                                     "identity": [{"start_utc": utc(row["start"]), "side": SIDES[row["side"]]} for row in events]},
        "paired_availability": paired_availability(frames),
        "frames": frames,
        "m15_frame_own_history": frame_own_m15(inputs, bounds, events),
        "scope": {
            "source": "Genuine TradingView archive for the old S5 admitted plan and execution tape. Not current Visual V2 or RC2 native output.",
            "observation": "Causal RC2 EARLY and Shadow reconstructions from each real OHLC archive. An observation or diagnostic geometry never counts as a fill.",
            "pairing": "Both timeframes score the exact original 33 M5 independent clock hours, reconstructed to the same provider OHLC, but use their own prior available chart history. The common one-hour labels are ex-post and do not influence entries.",
            "data_missing": "Original unsubmitted, rejected and watched S5 plans are absent; no reason or trade is inferred. No broker quote or live order history.",
            "pricing": "Native cash is the archived emulator's net with its own slippage and commission. Separately modeled quote-side replay uses original S5 levels, +0.05R after TP1, single slot, actual next contiguous open, stop-first, no second spread deduction, base and stress costs.",
            "comparison": "Same common date span, original strategy geometry and risk by timeframe, not identical admissions. Cohort stats use entry then final confirmed exit cutoff. The two chronological slices are descriptive, not independent validation.",
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--s5-native", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = (json.dumps(evaluate(args.data_root, args.s5_native), sort_keys=True, indent=2,
                          ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(compress(payload, compresslevel=9, mtime=0) if args.output.suffix == ".gz" else payload)


if __name__ == "__main__":
    main()

