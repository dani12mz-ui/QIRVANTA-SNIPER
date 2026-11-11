"""Run the registered matched OANDA precision-entry comparison without order routing."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
from dataclasses import asdict
from datetime import datetime
from gzip import GzipFile
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from statistics import mean, median
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from gold_pro.direct_breakout_research import (
    BASE, EVENT_CONFIG, FORWARD_SEAL, HOUR, RC2_ARCHIVE, ROOT, STRESS, WARMUP, Execution, archive_file,
    event_audit, failures, independent_hours, journal, native_plans, pinned, portfolio, simulate, stats,
)
from gold_pro.direct_trend_breakout import CONSOLIDATION, RETEST, STRUCTURAL, scan
from gold_pro.rc1_qa import compare_exports, read_export
from gold_pro.rc2_research import chart_bars, utc


REGISTRATION = ROOT / "research/precision_entry/evaluation_registration.json"
FAMILIES = (RETEST, STRUCTURAL, CONSOLIDATION)
PRIORITY = {name: rank for rank, name in enumerate(FAMILIES, 1)}


def epoch(iso):
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def cohort_windows(registration):
    spec = registration["historical"]
    start, split, end = (epoch(spec[key]) for key in ("development_start_utc", "readout_start_utc", "end_utc"))
    if not start < split < end:
        raise ValueError("Invalid chronological registration")
    return {"full": (start, end), "development": (start, split), "readout": (split, end)}


def open_tapes(registration, folder):
    """Verify provider archive before reading market rows and original plan events."""
    raw = pinned(RC2_ARCHIVE)
    expected = registration["historical"]
    if (expected["archive"] != RC2_ARCHIVE or expected["minimum_warmup_bars"] != WARMUP or
            sha256(raw).hexdigest() != expected["archive_sha256"]):
        raise ValueError("Registration no longer matches the pinned RC2 archive")
    frames = {}
    start, end = cohort_windows(registration)["full"]
    raw_seal = pinned(FORWARD_SEAL)
    seal = json.loads(raw_seal)
    prospective = registration["prospective"]
    if sha256(raw_seal).hexdigest() != prospective["sealed_original_sha256"]:
        raise ValueError("Unexpected protected forward seal hash")
    if end >= seal["forward_start_ms"]:
        raise ValueError("Historical replay intersects the protected forward seal")
    if not seal["forward_end_ms"] <= epoch(prospective["start_utc"]) < epoch(prospective["end_utc"]):
        raise ValueError("Invalid prospective range after the protected forward seal")
    with ZipFile(BytesIO(raw)) as archive:
        for frame in registration["timeframes_minutes"]:
            fixed_path = archive_file(archive, f"fixed_M{frame}.csv", folder)
            s5_path = archive_file(archive, f"parity_M{frame}.csv", folder)
            fixed = read_export(fixed_path, frame, "audit")
            s5 = read_export(s5_path, frame, "audit")
            parity = compare_exports(fixed, s5, ("core", "ready"))
            if parity["status"] != "MATCH":
                raise ValueError(f"Fixed/S5 inputs differ on M{frame}: {parity}")
            bars = chart_bars(fixed)
            if bars[-1]["time_close"] != end or start < bars[WARMUP]["time"]:
                raise ValueError(f"Registered coverage or warmup unavailable on M{frame}")
            if sum(row["time"] == start for row in bars) != 1:
                raise ValueError(f"Missing registered start on M{frame}")
            frames[frame] = (bars, native_plans(fixed, frame, "RC2"), native_plans(s5, frame, "S5_RAW"), parity)
    return frames


def check_cross_frame(frames, start, end):
    """Compare native intervals without rewriting provider bars or choosing the better tape."""
    m5 = {b["time"]: b for b in frames[5][0] if start <= b["time"] < end}
    count = incomplete = 0
    differences = []
    for bar in frames[15][0]:
        if not start <= bar["time"] < end:
            continue
        children = [m5.get(bar["time"] + i * 300_000) for i in range(3)]
        if any(b is None for b in children):
            incomplete += 1
            continue
        count += 1
        derived = (children[0]["open"], max(x["high"] for x in children),
                   min(x["low"] for x in children), children[-1]["close"])
        fields = ("open", "high", "low", "close")
        if any(abs(bar[key] - value) > 1e-8 for key, value in zip(fields, derived)):
            differences.append({"open_utc": utc(bar["time"]),
                                "native_m15": dict(zip(fields, (bar[k] for k in fields))),
                                "aggregated_m5": dict(zip(fields, derived))})
    return {"m15_bars_with_complete_m5_children": count, "incomplete_m5_windows": incomplete,
            "ohlc_differences": differences}


def eligible(plans, bars, start, end):
    return [plan for plan in plans if start <= bars[plan.bar]["time_close"] < end]


def precision_portfolio(plans, bars, costs):
    """Apply fixed source priority without altering legacy simulation or S5 management."""
    queued_on = occupied_until = -1
    occupant = None
    outcomes = []
    for plan in sorted(plans, key=lambda p: (p.bar, PRIORITY.get(p.model, 0), p.id)):
        if plan.bar < occupied_until or plan.bar == queued_on:
            outcome = Execution(plan, "POSITION_OCCUPIED", plan.bar, blocked_by=occupant)
        else:
            outcome = simulate(plan, bars, costs, lock_r=0.05 if plan.model == "S5_RAW" else None)
            queued_on, occupied_until, occupant = plan.bar, outcome.release, plan.id
        outcomes.append(outcome)
    return outcomes


def realized(outcome, bars, start, end, side=0):
    return (outcome.entry_bar is not None and outcome.state == "CLOSED" and
            start <= bars[outcome.entry_bar]["time"] < end and bars[outcome.release]["time_close"] <= end and
            (side == 0 or outcome.plan.side == side))


def metrics(results, bars, start, end, side=0):
    summary = stats(results, bars, start, end, side)
    closed = [item for item in results if realized(item, bars, start, end, side)]
    losses = [item for item in closed if item.net < 0]
    wins = [item for item in closed if item.net > 0]
    summary["mean_loss_usd"] = mean(item.net for item in losses) if losses else None
    summary["mean_loss_r"] = mean(item.net / item.risk_usd for item in losses) if losses else None
    summary["mean_win_usd"] = mean(item.net for item in wins) if wins else None
    summary["mean_win_r"] = mean(item.net / item.risk_usd for item in wins) if wins else None
    return summary


def volatile_hours(bars, minutes):
    groups = defaultdict(list)
    for bar in bars:
        groups[bar["time"] // HOUR * HOUR].append(bar)
    previous = deque(maxlen=24)
    result = set()
    for stamp, group in sorted(groups.items()):
        if [b["time"] for b in group] != list(range(stamp, stamp + HOUR, minutes * 60_000)):
            continue
        width = max(b["high"] for b in group) - min(b["low"] for b in group)
        if len(previous) == 24 and median(previous) > 0 and width >= 1.5 * median(previous):
            result.add(stamp)
        previous.append(width)
    return result


def fill_examples(outcomes, bars, categories, volatility, costs, start, end):
    results, regime_counts = {}, defaultdict(Counter)
    for outcome in outcomes:
        if not realized(outcome, bars, start, end):
            continue
        stamp = bars[outcome.entry_bar]["time"] // HOUR * HOUR
        category = categories.get(stamp, "INELIGIBLE_OR_PARTIAL")
        kinds = [category] + (["VOLATILE"] if stamp in volatility else [])
        for kind in kinds:
            stats_ = regime_counts[kind]
            stats_["closed"] += 1
            stats_["losses"] += outcome.net < 0
            stats_["net_usd"] += outcome.net
            # Keep the first realized loss and first realized win per category in chronological order.
            label = kind + ("_LOSS" if outcome.net < 0 else "_WIN_OR_FLAT")
            if label not in results:
                results[label] = journal(outcome, bars)
    ambiguous, gapped_exits = [], []
    for outcome in outcomes:
        if outcome.entry_bar is None:
            continue
        by_bar = defaultdict(list)
        for leg in outcome.exits:
            by_bar[leg["bar"]].append(leg)
        for index, exits in by_bar.items():
            if any(leg["reason"] == "SL_GAP" for leg in exits):
                gapped_exits.append({"plan_id": outcome.plan.id, "bar_open_utc": utc(bars[index]["time"]),
                                     "exit_prices": [leg["price"] for leg in exits if leg["reason"] == "SL_GAP"]})
            stopped = [leg["leg"] for leg in exits if leg["reason"] == "SL"]
            if not stopped:
                continue
            bar, side = bars[index], outcome.plan.side
            favorable = (bar["high"] if side > 0 else bar["low"]) - side * costs.spread / 2
            collided = [part for part in stopped if side * (favorable - outcome.plan.targets[part - 1]) >= 0]
            if collided:
                ambiguous.append({"plan_id": outcome.plan.id, "bar_open_utc": utc(bar["time"]),
                                  "stopped_legs_with_target_touched": collided})
    return {"first_examples": results, "regime_closed": dict(regime_counts),
            "same_bar_stop_target_conflicts": ambiguous, "stop_gap_exits": gapped_exits}


def opportunity_audit(events, categories, decisions, outcomes, bars, costs):
    audit = event_audit(events, categories, decisions, outcomes, bars)
    if costs != BASE:
        # The archived helper uses BASE spread for the OHLC excursion. Reprice only this statistic.
        first_fills = {}
        for outcome in sorted((item for item in outcomes if item.entry_bar is not None),
                              key=lambda item: (item.entry_bar, item.plan.id)):
            key = utc(bars[outcome.entry_bar]["time"] // HOUR * HOUR), outcome.plan.side
            first_fills.setdefault(key, outcome)
        for event, episode in zip(events, audit["episodes"]):
            side = event["side"]
            first = first_fills.get((episode["start_utc"], side))
            if first is None:
                continue
            prices = [first.entry, *(leg["price"] for leg in first.exits if leg["bar"] <= event["last_bar"])]
            prices.extend(bars[i]["high" if side > 0 else "low"] - side * costs.spread / 2
                          for i in range(first.entry_bar, min(first.release, event["last_bar"] + 1)))
            excursion = max(side * (price - first.entry) for price in prices)
            episode["first_fill_capture_fraction"] = excursion / event["directional_extreme_distance"]

    audit["conditional_medians"] = {}
    for label, side in (("all", 0), ("BUY", 1), ("SELL", -1)):
        group = [row for row in audit["episodes"] if not side or row["side"] == side]
        medians = {}
        for key in ("watch_delay_min", "confirmation_delay_min", "candidate_delay_min", "fill_delay_min",
                    "first_fill_capture_fraction"):
            available = [row[key] for row in group if row[key] is not None]
            medians[key] = {"n": len(available), "median": median(available) if available else None}
        audit["conditional_medians"][label] = medians
    return audit


def audit_lane(outcomes, decisions, bars, costs, windows, events, categories, volatility):
    start, end = windows["full"]
    output = {
        "states": dict(Counter(item.state for item in outcomes)),
        "windows": {
            label: {name: metrics(outcomes, bars, *window, side)
                    for name, side in (("all", 0), ("BUY", 1), ("SELL", -1))}
            for label, window in windows.items()
        },
        "execution_examples": fill_examples(outcomes, bars, categories, volatility, costs, start, end),
        "journal": [journal(item, bars) for item in outcomes],
        "opportunity_audit": opportunity_audit(events, categories, decisions, outcomes, bars, costs),
    }
    return output


def phase_key(item):
    return item.plan.model, item.plan.side, item.confirmation_ms


def rejection_audit(assessments, bars, costs, windows, rc2):
    """Keep isolation counterfactuals separate from one-slot leave-one-gate-out effects."""
    start, end = windows["full"]
    reasons = defaultdict(list)
    original = defaultdict(list)
    for item in assessments:
        if item.accepted:
            continue
        outcome = simulate(item.plan, bars, costs) if item.plan is not None else None
        if outcome is None:
            kind = "NO_PLAN_GEOMETRY"
        elif realized(outcome, bars, start, end):
            kind = "WIN" if outcome.net > 0 else "LOSS" if outcome.net < 0 else "FLAT"
        else:
            kind = outcome.state if outcome.state != "CLOSED" else "CENSORED"
        row = {"model": item.event["model"], "side": item.event["side"],
               "confirmed_utc": utc(item.confirmation_ms), "reasons": item.reasons,
               "isolated_outcome": kind, "replay": journal(outcome, bars) if outcome else None}
        for reason in item.precision_reasons:
            reasons[reason].append(row)
        for reason in item.original_reasons:
            original[reason].append(row)

    def counts(items):
        counts_ = Counter(row["isolated_outcome"] for row in items)
        closed = [row["replay"] for row in items if row["isolated_outcome"] in {"WIN", "LOSS", "FLAT"}]
        return {"rows": len(items), "outcomes": dict(counts_),
                "closed_net_usd": sum(row["realized_usd"] for row in closed),
                "closed_net_r": sum(row["net_r"] for row in closed)}

    accepted = [item.plan for item in assessments if item.accepted]
    ablations = {}
    for reason, items in sorted(reasons.items()):
        sole_gate = [item for item in assessments if item.reasons == (reason,) and item.plan is not None]
        restored = [item.plan for item in sole_gate]
        counter = precision_portfolio([*accepted, *restored], bars, costs)
        ablations[reason] = {
            "independent_all_rejections": counts(items),
            "independent_sole_gate": counts([row for row in items if tuple(row["reasons"]) == (reason,)]),
            "restored_only_if_sole_gate": len(restored),
            "leave_one_gate_out_metrics": metrics(counter, bars, start, end),
            "rc2_plus_leave_one_gate_out_metrics": metrics(
                precision_portfolio([*rc2, *accepted, *restored], bars, costs), bars, start, end
            ),
            "counterfactuals": items,
        }
    return {"precision_gates": ablations,
            "original_gates": {reason: {"isolated": counts(items), "counterfactuals": items}
                               for reason, items in sorted(original.items())}}


def displaced(original, combined, bars, start, end):
    by_id = {item.plan.id: item for item in combined}
    rows = []
    for item in original:
        if not realized(item, bars, start, end):
            continue
        after = by_id.get(item.plan.id)
        if after is None or after.entry_bar is not None:
            continue
        rows.append({"plan_id": item.plan.id, "decision_utc": utc(bars[item.plan.bar]["time_close"]),
                     "original_net_usd": item.net, "original_net_r": item.net / item.risk_usd,
                     "combined_state": after.state, "blocked_by": after.blocked_by})
    return {"closed_campaigns_displaced": len(rows), "wins_displaced": sum(r["original_net_usd"] > 0 for r in rows),
            "losses_displaced": sum(r["original_net_usd"] < 0 for r in rows),
            "original_net_usd": sum(r["original_net_usd"] for r in rows), "rows": rows}


def retest_pairs(assessments, bars, costs, windows):
    start, end = windows["full"]
    origins = {phase_key(a): a for a in assessments if a.plan and a.event["model"] != RETEST}
    rows = []
    for retest in assessments:
        if retest.event["model"] != RETEST or not retest.plan:
            continue
        root = origins.get((retest.event["origin_model"], retest.plan.side, retest.origin_confirmation_ms))
        if root is None:
            continue
        early, late = (simulate(a.plan, bars, costs) for a in (root, retest))
        if not (realized(early, bars, start, end) and realized(late, bars, start, end)):
            continue
        rows.append({"side": retest.plan.side, "origin_utc": utc(root.confirmation_ms),
                     "retest_utc": utc(retest.confirmation_ms), "immediate_precision_accepted": root.accepted,
                     "retest_precision_accepted": retest.accepted, "immediate_usd": early.net, "retest_usd": late.net,
                     "delta_usd": late.net - early.net,
                     "delta_r": late.net / late.risk_usd - early.net / early.risk_usd})
    return {"matched_isolated_closed_pairs": len(rows), "mean_delta_r": mean(r["delta_r"] for r in rows) if rows else None,
            "median_delta_r": median(r["delta_r"] for r in rows) if rows else None,
            "better_retests": sum(row["delta_r"] > 1e-9 for row in rows),
            "worse_retests": sum(row["delta_r"] < -1e-9 for row in rows),
            "tied_retests": sum(abs(row["delta_r"]) <= 1e-9 for row in rows), "rows": rows}


def assessment_record(item):
    return {
        "plan": asdict(item.plan) if item.plan else None, "source_event": item.event,
        "source": item.source, "direction": item.direction, "costs": asdict(item.costs),
        "detection_utc": utc(item.detection_ms), "confirmation_utc": utc(item.confirmation_ms),
        "origin_detection_utc": utc(item.origin_detection_ms) if item.origin_detection_ms else None,
        "origin_confirmation_utc": utc(item.origin_confirmation_ms) if item.origin_confirmation_ms else None,
        "next_executable_utc": utc(item.next_executable_ms), "expires_utc": utc(item.expires_ms),
        "stop_risk": item.stop_risk, "risk_cash": item.risk_cash, "friction_price": item.friction_price,
        "net_room_r": item.net_room_r, "extension_atr": item.extension_atr, "body_atr": item.body_atr,
        "retest_age_bars": item.retest_age_bars, "retest_separation_atr": item.retest_separation_atr,
        "accepted": item.accepted, "risk_rejection_reason": item.risk_rejection_reason,
        "reasons": item.reasons, "shadow_reasons": item.shadow_reasons,
    }


def analyze(frame, tape, registration):
    from gold_pro.precision_entry import assess

    bars, (rc2, rc2_decisions), (s5, s5_decisions), parity = tape
    windows = cohort_windows(registration)
    start, end = windows["full"]
    rc2, s5 = (eligible(plans, bars, start, end) for plans in (rc2, s5))
    shadow_events, legacy_shadow = scan(bars, BASE)
    legacy_shadow = eligible(legacy_shadow, bars, start, end)
    events, categories, opportunity_catalog = independent_hours(bars, frame, json.loads(pinned(EVENT_CONFIG))["event"])
    events = [event for event in events if start <= event["start"] < end]
    volatility = volatile_hours(bars, frame)
    window_categories = Counter(cat for stamp, cat in categories.items() if start <= stamp < end)
    report = {"native_context_parity": parity, "first_loaded_open_utc": utc(bars[0]["time"]),
              "bars": len(bars), "independent_hours": {**opportunity_catalog,
                  "registered_eligible_regimes": dict(window_categories),
                  "registered_volatile": sum(start <= stamp < end for stamp in volatility)}, "cost_scenarios": {}}
    base_assessments = tuple(item for item in assess(bars, events=shadow_events, costs=BASE)
                             if start <= item.confirmation_ms < end)
    for scenario, costs in (("baseline", BASE), ("stress", STRESS)):
        assessed = base_assessments if scenario == "baseline" else tuple(
            item for item in assess(bars, events=shadow_events, costs=costs) if start <= item.confirmation_ms < end)
        accepted = [item.plan for item in assessed if item.accepted]
        offers = {name: [plan for plan in accepted if plan.model == name] for name in FAMILIES}
        shadow_by_model = {name: [event for event in shadow_events if event["model"] == name] for name in FAMILIES}
        lanes = {
            "RC2": (portfolio(rc2, bars, costs), rc2_decisions),
            "S5_RAW": (portfolio(s5, bars, costs, use_s5_lock=True), s5_decisions),
            "RC2_PLUS_ORIGINAL_SHADOW": (portfolio([*rc2, *legacy_shadow], bars, costs), [*rc2_decisions, *shadow_events]),
            **{name: (precision_portfolio(offers[name], bars, costs), shadow_by_model[name]) for name in FAMILIES},
            **{f"RC2_PLUS_{name}": (precision_portfolio([*rc2, *offers[name]], bars, costs),
                                   [*rc2_decisions, *shadow_by_model[name]]) for name in FAMILIES},
            "PRECISION_POOL": (precision_portfolio(accepted, bars, costs), shadow_events),
            "RC2_PLUS_PRECISION": (precision_portfolio([*rc2, *accepted], bars, costs), [*rc2_decisions, *shadow_events]),
            "S5_RAW_PLUS_PRECISION": (precision_portfolio([*s5, *accepted], bars, costs), [*s5_decisions, *shadow_events]),
        }
        if costs == STRESS:
            # Fixed baseline-approved offers isolate repricing from cost-dependent reselection.
            fixed_offers = [item.plan for item in base_assessments if item.accepted]
            lanes["RC2_PLUS_PRECISION_FIXED_BASE_OFFERS"] = (
                precision_portfolio([*rc2, *fixed_offers], bars, costs), [*rc2_decisions, *shadow_events]
            )
        report["cost_scenarios"][scenario] = {
            "costs": asdict(costs),
            "lanes": {name: audit_lane(lane, decisions, bars, costs, windows, events, categories, volatility)
                      for name, (lane, decisions) in lanes.items()},
            "assessments": [assessment_record(item) for item in assessed],
            "rejections": rejection_audit(assessed, bars, costs, windows, rc2),
            "origin_retest_pairs": retest_pairs(assessed, bars, costs, windows),
            "displacement": {
                "RC2": displaced(lanes["RC2"][0], lanes["RC2_PLUS_PRECISION"][0], bars, start, end),
                "S5_RAW": displaced(lanes["S5_RAW"][0], lanes["S5_RAW_PLUS_PRECISION"][0], bars, start, end),
                "RC2_BY_ORIGINAL_SHADOW": displaced(lanes["RC2"][0], lanes["RC2_PLUS_ORIGINAL_SHADOW"][0],
                                                   bars, start, end),
            },
        }
    report["three_bar_breakout_failures"] = {
        name: failures([event for event in shadow_events
                        if event["model"] == name and start <= event["time_ms"] < end], bars, end)
        for name in FAMILIES
    }
    return report


def run():
    raw = REGISTRATION.read_bytes()
    registration = json.loads(raw)
    if (registration["schema"] != "qirvanta.precision-entry.evaluation.v1" or
            registration["symbol"] != "OANDA:XAUUSD" or registration["timeframes_minutes"] != [5, 15]):
        raise ValueError("Unexpected registration")
    windows = cohort_windows(registration)
    with TemporaryDirectory() as temp:
        frames = open_tapes(registration, Path(temp))
        return {"registration_sha256": sha256(raw).hexdigest(), "registration": registration,
                "evaluation_scope": {
                    "native_inputs": "Frozen native TradingView CORE/READY offers and OANDA ordinary OHLC.",
                    "execution_results": "Python next-open replay on native inputs.",
                    "original_shadow_stress": "Keep archived close gates at BASE and reprice fills.",
                    "precision_stress": "Re-evaluate frozen cost-dependent close gates at stressed cost and reprice fills.",
                    "fixed_base_offers_stress": "Diagnostic counterpart keeps BASE close admissions and reprices fills.",
                    "opportunity_labels": "Independent completed clock-hour grades are available only after each hour.",
                    "off_event_fills": "Outside an independently labeled same-side hour. Includes profitable fills.",
                    "drawdown": "Realized equity plus open exposure marked at each bar close."
                },
                "source_sha256": {
                    path: sha256((ROOT / path).read_bytes()).hexdigest() for path in (
                        "gold_pro/precision_entry.py", "gold_pro/precision_entry_research.py",
                        "gold_pro/direct_trend_breakout.py", "gold_pro/direct_breakout_research.py",
                        "research/precision_entry/REGISTRATION.md"
                    )
                },
                "cross_frame": check_cross_frame(frames, *windows["full"]),
                "frames": {f"M{frame}": analyze(frame, tape, registration) for frame, tape in frames.items()},
                "promotion_status": "RESEARCH_ONLY_NO_UNTOUCHED_MATCHED_DATA"}


def compact(report):
    result = {key: value for key, value in report.items() if key != "frames"}
    result["frames"] = {}
    for frame, panel in report["frames"].items():
        base = {key: value for key, value in panel.items() if key not in {"cost_scenarios", "three_bar_breakout_failures"}}
        base["three_bar_breakout_failures"] = {
            model: {key: value for key, value in failures_.items() if key != "rows"}
            for model, failures_ in panel["three_bar_breakout_failures"].items()
        }
        base["cost_scenarios"] = {}
        for name, scenario in panel["cost_scenarios"].items():
            summary = {key: value for key, value in scenario.items() if key not in
                       {"lanes", "assessments", "rejections", "origin_retest_pairs"}}
            summary["lanes"] = {
                model: {**{k: value for k, value in lane.items() if k not in
                           {"journal", "execution_examples", "opportunity_audit"}},
                        "recall": lane["opportunity_audit"]["recall"],
                        "timing_and_capture": lane["opportunity_audit"]["conditional_medians"],
                        "outside_same_side_event": lane["opportunity_audit"]["outside_same_side_event"],
                        "same_bar_stop_target_conflicts": len(lane["execution_examples"]["same_bar_stop_target_conflicts"]),
                        "stop_gap_exits": len(lane["execution_examples"]["stop_gap_exits"])}
                for model, lane in scenario["lanes"].items()
            }
            summary["rejections"] = {
                group: {gate: {key: value for key, value in data.items() if key != "counterfactuals"}
                        for gate, data in gates.items()} for group, gates in scenario["rejections"].items()
            }
            summary["origin_retest_pairs"] = {k: value for k, value in scenario["origin_retest_pairs"].items() if k != "rows"}
            base["cost_scenarios"][name] = summary
        result["frames"][frame] = base
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(report, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()
    if args.output.suffix == ".gz":
        with args.output.open("wb") as raw, GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            compressed.write(payload)
    else:
        args.output.write_bytes(payload)
    if args.summary:
        args.summary.parent.mkdir(parents=True, exist_ok=True)
        args.summary.write_text(json.dumps(compact(report), ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                                encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
