"""Audit historical RC2 EARLY, immutable S5, and the prior M5 lead model."""

import argparse
from collections import Counter
from gzip import decompress
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from gold_pro.direct_breakout_research import (
    BASE, EVENT_CONFIG, FORWARD_SEAL, PINS, WARMUP, WSB_ARCHIVE, independent_hours, pinned,
)
from gold_pro.rc2_research import early_observations
from research.data.loader import load_bars
from research.direct_trend_breakout.forensic_timeline import utc
from research.features.causal import Smooth
from research.m5_lead_entry_window.evaluate import first_unique
from unified.tools.audit_premium_s5 import verify_bars
from unified.tools.audit_tv_observations import inspect


HERE = Path(__file__).resolve().parent
PRIOR_EVALUATION = HERE.parent / "m5_lead_entry_window/evidence/evaluation.json.gz"
PRIOR_SHA256 = "d7793d06cf3075af63dae704e0ddd5fa5ae5dd4927863d90a748622e05dacd40"
PRE_HOUR = 15 * 60_000
BAR_MS = 5 * 60_000
SIDES = {1: "BUY", -1: "SELL"}
DIAGNOSTIC_PORTFOLIOS = ("standalone", "approved_priority")
DIRECTIONS = {"BUY": 21, "SELL": 12}


def load_prior_evaluation(path=PRIOR_EVALUATION):
    raw = Path(path).read_bytes()
    if sha256(raw).hexdigest() != PRIOR_SHA256:
        raise ValueError("Prior M5 lead research artifact checksum differs")
    return json.loads(decompress(raw))


def timing(stamp, beginning):
    return {"known_ms": stamp, "known_utc": utc(stamp), "minutes_from_hour_start": (stamp - beginning) / 60_000}


def market_record(bar):
    return {**{name: bar[name] for name in ("open", "high", "low", "close")},
            "open_utc": utc(bar["time"]), "close_utc": utc(bar["time_close"])}


def rc2_trace(bars):
    """The existing EARLY edge, independent of any hindsight movement label."""
    previous_side = 0
    smooth = Smooth(14, wilder=True)
    rows, observed = [], []
    for index, bar in enumerate(bars):
        reference_atr = smooth.value
        previous = bars[index - 1] if index else bar
        high, low = bar["high"], bar["low"]
        width = max(high - low, BASE.tick)
        body = bar["close"] - bar["open"]
        smooth.add(max(high - low, abs(high - previous["close"]), abs(low - previous["close"])))
        preceding = bars[max(0, index - 12):index]
        enough_bars = len(preceding) == 12
        level = {1: max(row["high"] for row in preceding), -1: min(row["low"] for row in preceding)} if enough_bars else {}
        gap = bar["time"] != previous["time_close"]
        expanded = reference_atr is not None and width >= 1.15 * reference_atr
        geometry = 0
        decisions = {}
        for side in SIDES:
            blockers = []
            if not enough_bars:
                blockers.append("INSUFFICIENT_PRIOR_BARS")
            if reference_atr is None:
                blockers.append("PRIOR_ATR_UNAVAILABLE")
            elif not expanded:
                blockers.append("RANGE_BELOW_1_15_PRIOR_ATR")
            if enough_bars and side * (bar["close"] - level[side]) <= 0:
                blockers.append("CLOSE_NOT_BEYOND_PRIOR_12_BAR_EDGE")
            if reference_atr is not None and side * body < 0.50 * reference_atr:
                blockers.append("DIRECTIONAL_BODY_BELOW_0_50_PRIOR_ATR")
            if not blockers:
                geometry = side
                if previous_side == side:
                    blockers.append("SAME_SIDE_GEOMETRY_CONTINUED")
                if gap:
                    blockers.append("PREVIOUS_BAR_GAP")
            decisions[side] = {"prior_12_edge": level.get(side), "blockers": blockers}
        if geometry and not decisions[geometry]["blockers"]:
            observed.append({"known_ms": bar["time_close"], "side": geometry, "price": bar["close"]})
        rows.append({
            "known_ms": bar["time_close"], "close": bar["close"], "atr_previous": reference_atr,
            "width": width, "body_signed_buy": body, "gap": gap, "geometry_side": geometry,
            "previous_geometry_side": previous_side, "decisions": decisions,
        })
        previous_side = geometry
    if observed != early_observations(bars):
        raise ValueError("RC2 EARLY predicate reconstruction differs from the existing reference")
    return rows, observed


def require_campaign_clock(campaign, bars):
    metadata = campaign["submitted_metadata"]
    submission = metadata["submitted_bar"]
    entry = campaign["entry_bar_index"]
    if (bars[submission]["time_close"], bars[entry]["time"]) != (metadata["submitted_time"], campaign["entry_time"]):
        raise ValueError(f"Original S5 clocks differ from matched OHLC: {campaign['plan_id']}")
    if submission >= entry:
        raise ValueError(f"Original S5 entry does not follow submission: {campaign['plan_id']}")


def native_campaign_summary(campaign, bars, beginning):
    require_campaign_clock(campaign, bars)
    metadata = campaign["submitted_metadata"]
    completed = campaign["state"] == "COMPLETE"
    last = max((row["exit_bar_index"] for row in campaign["exit_rows"]), default=None)
    last_known = bars[last]["time_close"] if completed and last is not None else None
    return {
        "plan_id": campaign["plan_id"], "side": campaign["side"], "source": "IMMUTABLE_TRADINGVIEW_S5_ARCHIVE",
        "approval": timing(metadata["submitted_time"], beginning),
        "entry_effective_ms": campaign["entry_time"], "entry_effective_utc": utc(campaign["entry_time"]),
        "entry_minutes_from_hour_start": (campaign["entry_time"] - beginning) / 60_000,
        "entry_bar_close_utc": utc(bars[campaign["entry_bar_index"]]["time_close"]),
        "entry": float(campaign["actual_entry"]), "signal_reference": float(metadata["reference"]),
        "initial_sl": float(metadata["initial_sl"]), "targets": [float(metadata[f"tp{i}"]) for i in (1, 2, 3)],
        "campaign_complete": completed, "full_exit_known_ms": last_known,
        "full_exit_known_utc": utc(last_known) if last_known is not None else None,
        "closed_cash_usd_native": float(campaign["realized_net_cash"]) if completed else None,
        "closed_net_r_native": float(campaign["net_r"]) if completed and campaign["net_r"] is not None else None,
        "exit_reasons": dict(Counter(row["exit_comment"] for row in campaign["exit_rows"])),
    }


def s5_hour(campaigns, bars, episode):
    start, stop, side = episode["start"], episode["end"], episode["side"]
    submitted, filled, carried = [], [], []
    for campaign in campaigns:
        at = campaign["submitted_metadata"]["submitted_time"]
        entry = campaign["entry_time"]
        if start <= at < stop:
            submitted.append(native_campaign_summary(campaign, bars, start))
        if start <= entry < stop:
            filled.append(native_campaign_summary(campaign, bars, start))
        if entry < start:
            last = max((row["exit_bar_index"] for row in campaign["exit_rows"]), default=None)
            if last is None or campaign["state"] != "COMPLETE" or bars[last]["time_close"] > start:
                carried.append(native_campaign_summary(campaign, bars, start))
    same_submit = [item for item in submitted if item["side"] == side]
    same_fill = [item for item in filled if item["side"] == side]
    return {
        "first_same_side_approval": min((row["approval"] for row in same_submit), key=lambda row: row["known_ms"], default=None),
        "first_same_side_fill": min(same_fill, key=lambda row: row["entry_effective_ms"], default=None),
        "submitted": submitted, "filled": filled, "carried_into_hour": carried,
        "same_side_submission_count": len(same_submit), "same_side_fill_count": len(same_fill),
        "opposite_side_submission_count": len(submitted) - len(same_submit),
        "opposite_side_fill_count": len(filled) - len(same_fill),
        "no_same_side_admission_reason": None if same_submit else "UNKNOWN_NOT_LOGGED_IN_ARCHIVED_ADMITTED_TAPE",
    }


def prior_rows(lane, bars_by_close):
    events = lane["all_confirmed_events"]
    for event in events:
        known, effective = event["known_ms"], event["entry_ms"]
        if known not in bars_by_close or event["side"] not in SIDES:
            raise ValueError("Prior M5 observation is not a recorded chart close")
        if effective is not None:
            bar = bars_by_close[known]
            if effective != bar["time"] or not effective < known:
                raise ValueError("Prior modeled entry open must be separate from the subsequent event close")
    return events


def prior_event_record(event, start):
    return {**event, **timing(event["known_ms"], start),
            "entry_effective_utc": utc(event["entry_ms"]) if event["entry_ms"] is not None else None}


def prior_diagnosis(events, side, start, stop):
    same = [row for row in events if row["side"] == side and start <= row["known_ms"] < stop]
    before = [row for row in events if row["side"] == side and start - PRE_HOUR <= row["known_ms"] < start]
    breaks = [row for row in same if row["event"] in {"MICRO_BREAK", "SECOND_CHANCE"}]
    watches = [row for row in same if row["event"] == "EARLY_SETUP"]
    candidates = [row for row in same if row["event"] == "ENTRY_CANDIDATE"]
    rejected = [row for row in same if row["event"] == "REJECTED"]
    clock = lambda records: prior_event_record(records[0], start) if records else None
    return {
        "first_watch": clock(watches), "first_confirmed_break": clock(breaks),
        "first_entry_candidate_known": clock(candidates), "first_rejection_known": clock(rejected),
        "first_watch_within_preceding_15_minutes": clock([row for row in before if row["event"] == "EARLY_SETUP"]),
        "first_confirmed_break_within_preceding_15_minutes":
            clock([row for row in before if row["event"] in {"MICRO_BREAK", "SECOND_CHANCE"}]),
        "rejections_by_reason": dict(Counter(row["reason"] for row in rejected)),
        "all_events_including_opposing": [prior_event_record(row, start) for row in events if start <= row["known_ms"] < stop],
    }


def prior_portfolio(lane, start, stop, side):
    portfolios = {}
    for name in DIAGNOSTIC_PORTFOLIOS:
        source = lane["portfolio"][name]
        fills = [row for row in source["journal"] if start <= row["entry_ms"] < stop]
        carry = [row for row in source["journal"] if row["entry_ms"] < start < row["release_ms"]]
        blocks = [row for row in source["position_exclusions"] if start <= row["entry_ms"] < stop]
        books = source["source_risk_rejection_and_occupancy_counterfactuals"]["ledger"]
        later_assessments = [row for row in books
                             if row["side"] == side and utc(start) <= row["known_at_utc"] < utc(stop)]
        portfolios[name] = {
            "all_modeled_fills": fills, "carried_into_hour": carry, "all_capacity_exclusions": blocks,
            "same_side_fills": sum(row["side"] == side for row in fills),
            "same_side_capacity_exclusions": sum(row["side"] == side for row in blocks),
            "retrospective_same_side_rejected_or_blocked_scenarios": later_assessments,
        }
    return portfolios


def summary(hours):
    output = {}
    for label in ("all", "BUY", "SELL"):
        group = [row for row in hours if label == "all" or row["direction"] == label]
        first = [row["rc2_early"]["first_same_side_in_hour"]["minutes_from_hour_start"] for row in group
                 if row["rc2_early"]["first_same_side_in_hour"]]
        output[label] = {
            "significant_hours": len(group),
            "rc2_early_in_hour": len(first),
            "rc2_early_preceding_15_minutes": sum(bool(row["rc2_early"]["first_unique_same_side_pre_hour"]) for row in group),
            "rc2_early_missed_including_pre_hour": sum(
                not row["rc2_early"]["first_same_side_in_hour"] and not row["rc2_early"]["first_unique_same_side_pre_hour"]
                for row in group),
            "rc2_early_in_hour_median_minutes": median(first) if first else None,
            "s5_admitted_same_side": sum(row["s5"]["same_side_submission_count"] > 0 for row in group),
            "s5_filled_same_side": sum(row["s5"]["same_side_fill_count"] > 0 for row in group),
            "s5_any_position_carried": sum(bool(row["s5"]["carried_into_hour"]) for row in group),
            "s5_filled_opposite_side": sum(row["s5"]["opposite_side_fill_count"] > 0 for row in group),
            "previous_m5_lead": {
                cost: {
                    "watch_in_hour": sum(bool(row["prior_m5_lead"][cost]["first_watch"]) for row in group),
                    "break_in_hour": sum(bool(row["prior_m5_lead"][cost]["first_confirmed_break"]) for row in group),
                    "candidate_known_in_hour": sum(bool(row["prior_m5_lead"][cost]["first_entry_candidate_known"]) for row in group),
                    **{f"{lane}_same_side_fill_hours": sum(
                        row["prior_m5_lead"][cost]["portfolios"][lane]["same_side_fills"] > 0 for row in group)
                        for lane in DIAGNOSTIC_PORTFOLIOS},
                } for cost in ("normal", "stress")
            },
        }
    return output


def audit(bars, prior_evaluation, s5_native):
    """Return archived baseline decisions for exactly the registered 33 hours."""
    if (s5_native["log_sha256"] != PINS["archival_s5_native.csv"] or s5_native["mode"] != "S5"
            or s5_native["unfilled"] or s5_native["run"]["timeframe"] != "5"):
        raise ValueError("Unexpected immutable S5 control")
    verify_bars(bars, s5_native["run"])
    seal_start = json.loads(pinned(FORWARD_SEAL))["forward_start_ms"]
    if bars[-1]["time_close"] >= seal_start:
        raise ValueError("Sealed data is outside the archival audit")
    previous = prior_evaluation["archival_m5"]
    start, end = bars[WARMUP]["time"], bars[-1]["time_close"]
    if (previous["loaded_bars"], previous["scored_window_utc"], previous["scored_directional_hours"]) != (
            len(bars), [utc(start), utc(end)], DIRECTIONS):
        raise ValueError("Prior M5 evaluation and archive windows differ")
    events, _, _ = independent_hours(bars, 5, json.loads(pinned(EVENT_CONFIG))["event"])
    episodes = [row for row in events if start <= row["start"] and row["end"] <= end]
    if dict(Counter(SIDES[row["side"]] for row in episodes)) != DIRECTIONS:
        raise ValueError("Independent event universe changed from the frozen 33 hours")
    trace, observations = rc2_trace(bars)
    trace_by_close = {row["known_ms"]: row for row in trace}
    observations = [row for row in observations if start <= row["known_ms"] <= end]
    kinds = {(row["start"], row["side"]) for row in episodes}
    rc2_match = first_unique([(row["known_ms"], row["side"], f"{row['known_ms']}|{row['side']}")
                              for row in observations], episodes, kinds, PRE_HOUR)
    bars_by_close = {bar["time_close"]: bar for bar in bars}
    lanes = {name: prior_rows(previous["results"][name], bars_by_close) for name in ("normal", "stress")}
    for name in lanes:
        archived = previous["results"][name]["portfolio"]["standalone"]["diagnostic_coverage"]["episodes"]
        if [(row["start_utc"], row["side"]) for row in archived] != [
                (utc(row["start"]), SIDES[row["side"]]) for row in episodes]:
            raise ValueError("Prior M5 directional episode identities differ")
    output = []
    for episode in episodes:
        at, stop, side = episode["start"], episode["end"], episode["side"]
        active_match = rc2_match.get(at, {})
        gate_rows = []
        for stamp in range(at - PRE_HOUR, stop, BAR_MS):
            entry = trace_by_close.get(stamp)
            if entry is None:
                continue
            gate_rows.append({
                **timing(stamp, at), "bar": market_record(bars_by_close[stamp]),
                "prior_atr14": entry["atr_previous"], "range": entry["width"],
                "directional_body": entry["body_signed_buy"] * side,
                "candidate_geometry_side": entry["geometry_side"],
                "previous_geometry_side": entry["previous_geometry_side"],
                **entry["decisions"][side],
            })
        known = {row["known_ms"]: row for row in observations if row["side"] == side and at - PRE_HOUR <= row["known_ms"] < stop}
        selected = lambda part: {**known[active_match[part][0]], **timing(active_match[part][0], at)} if part in active_match else None
        rc2 = {
            "source": "CAUSAL_OHLC_RECONSTRUCTION_OF_EXISTING_RC2_EARLY",
            "permission": "OBSERVATION_ONLY",
            "first_same_side_in_hour": selected("in_hour"),
            "first_unique_same_side_pre_hour": selected("pre_hour"),
            "all_observations_in_hour": [{**row, **timing(row["known_ms"], at)}
                                         for row in observations if at <= row["known_ms"] < stop],
            "same_side_gate_trace": gate_rows,
            "in_hour_gate_blocker_counts": dict(Counter(reason for row in gate_rows if row["known_ms"] >= at
                                                        for reason in row["blockers"])),
        }
        prior = {}
        for cost, lane in lanes.items():
            prior[cost] = {
                **prior_diagnosis(lane, side, at, stop),
                "portfolios": prior_portfolio(previous["results"][cost], at, stop, side),
            }
        output.append({
            "start_ms": at, "start_utc": utc(at), "end_ms": stop, "end_utc": utc(stop), "side": side,
            "direction": SIDES[side], "rc2_early": rc2, "s5": s5_hour(s5_native["campaigns"], bars, episode),
            "prior_m5_lead": prior,
        })
    final = summary(output)
    if final["all"]["rc2_early_in_hour"] != 29 or final["all"]["s5_filled_same_side"] != 2:
        raise ValueError("Baseline coverage does not reproduce the known archive controls")
    return {
        "schema": "qirvanta.market-coverage.baseline-audit.v1",
        "event_protocol_sha256": PINS[EVENT_CONFIG], "s5_archive_sha256": s5_native["log_sha256"],
        "prior_m5_lead_evidence_sha256": PRIOR_SHA256,
        "window_utc": [utc(start), utc(end)], "hours": output, "summary": final,
        "scope": {
            "rc2_early": "Historical observation reconstructed from exact OHLC, not native delivery. Price and all gate evidence are recorded only at the known close.",
            "s5": "Native TradingView strategy-emulator archive of admitted campaigns. Watch, unsubmitted/rejected opportunity reasons and broker fills are unavailable. Carried occupancy is observed from original chart-bar clocks.",
            "prior_m5": "Frozen historical-only diagnostic research. Same-side C known time and modeled open are separate. Counterfactual rejections use later prices for evaluation, never for candidate decisions.",
            "matching": "In-hour [start,end) plus unique same-side 15-minute pre-hour observations for RC2. A close on the hour belongs to the next hour. Significant direction is an ex-post label, never fed into a signal engine.",
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with TemporaryDirectory() as scratch, ZipFile(BytesIO(pinned(WSB_ARCHIVE))) as archive:
        paths = [Path(scratch) / name for name in ("archival_m5_bars.csv", "archival_s5_native.csv")]
        for path in paths:
            path.write_bytes(pinned(path.name, archive.read(path.name)))
        result = audit(load_bars(paths[0]), load_prior_evaluation(), inspect(paths[1]))
    args.output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
                           encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()

