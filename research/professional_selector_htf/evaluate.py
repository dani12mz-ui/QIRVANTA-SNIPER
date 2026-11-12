"""Reevaluate frozen P1/P2 on causal, provider-aligned H1/H4 archives."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile
from io import BytesIO

from gold_pro.direct_breakout_research import (
    EVENT_CONFIG, FORWARD_SEAL, RC2_ARCHIVE, WARMUP, event_audit, independent_hours,
    journal, pinned, portfolio, simulate,
)
from gold_pro.direct_trend_breakout import RETEST, WINDOW_BARS, scan
from gold_pro.rc1_qa import read_export
from research.data.loader import load_bars
from research.direct_trend_breakout.forensic_timeline import ms, utc
from research.professional_signal_selector.evaluate import (
    COSTS, HOUR, MODELS, PROTOCOL_SHA, SIDES, admission, checked, cohort_statistics, expectancy,
    extract, observed_native_slots, offers_status, read_panels, reclaim, reclaim_counts, require,
    source_prefix_audit, transition_difference,
)
from unified.tools.audit_gold_context_archive import compare_sources
from unified.tools.gold_market_data import HTF_WINDOW, MAX_AGE_MS, context_rows


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
FROZEN = "26dfcd7d7e287cb22cc4baa6607d0cff887c494c55cd9b4f0656086e3201da1b"
PRIOR_SHA = "1b31dc5dad532b9362c5340511211fbc13a33c64983d4a49485c7442a269c5d5"
V22_PINS = {
    5: "fc88437df919a0b4fabebdc1d03d048e7c1c814fd4315a4ca7d559c1290b8385",
    15: "35e911e9ed53f57d4550bea601ec6653217254d83422dcf2d3eb63cf824b2252",
}
OHLC = ("open", "high", "low", "close")
FRAMES = {"h1": 60, "h4": 240}


def check_aggregation(lower, higher):
    by_open = {row["time"]: row for row in lower}
    step = lower[0]["time_close"] - lower[0]["time"]
    compared = 0
    for row in higher:
        parts = [by_open.get(stamp) for stamp in range(row["time"], row["time_close"], step)]
        if not parts or any(part is None for part in parts):
            continue
        ohlc = (parts[0]["open"], max(part["high"] for part in parts),
                min(part["low"] for part in parts), parts[-1]["close"])
        require(ohlc == tuple(row[key] for key in OHLC), f"Provider OHLC disagreement at {utc(row['time'])}")
        compared += 1
    require(compared > 0, "No exact overlapping provider candle was available")
    return compared


def canonical_context(bars, prior):
    earlier = [row for row in prior if row["time_close"] <= bars[0]["time"]]
    result = list(context_rows(bars, earlier=earlier))
    cuts = sorted({WARMUP, len(bars) // 2, 3 * len(bars) // 4, len(bars) - 1})
    for size in cuts:
        require(list(context_rows(bars[:size], earlier=earlier)) == result[:size],
                f"Canonical HTF changes with later chart data at cut {size}")
    require(list(context_rows(bars, earlier=earlier[-2000:])) == result, "Canonical window changed when old history was removed")
    for row in result:
        for label, minutes in FRAMES.items():
            context = getattr(row, label)
            if context.known_at is not None:
                require(context.known_at <= row.bar.open_at, "Future canonical HTF candle")
            if context.side is not None:
                require(0 <= row.bar.open_at - context.known_at <= MAX_AGE_MS[minutes], "Stale canonical HTF side")
    gap_seconds = Counter((after["time"] - before["time_close"]) // 1000
                          for before, after in zip(bars, bars[1:]) if after["time"] > before["time_close"])
    known = sum(row.h1.side is not None and row.h4.side is not None for row in result[WARMUP:])
    return result, {
        "loaded_bars": len(bars), "prior_m30_bars": len(earlier), "scored_bars": len(result) - WARMUP,
        "scored_both_frames_known": known, "scored_unknown": len(result) - WARMUP - known,
        "known_side_by_frame": {label: dict(Counter(str(getattr(row, label).side) for row in result[WARMUP:]))
                                for label in FRAMES},
        "chart_gap_seconds": dict(gap_seconds), "prefix_lengths_exact": cuts,
        "all_contexts_equal_with_last_2000_prefix_bars": True,
    }


def verify_recent_clocks(name, rows):
    if not name.endswith("_matched"):
        return None
    frame = int(name.split("_")[0][1:])
    with TemporaryDirectory() as folder, ZipFile(BytesIO(pinned(RC2_ARCHIVE))) as archive:
        native = read_export(extract(archive, f"fixed_M{frame}.csv", Path(folder)), frame, "audit").context
    require(len(rows) == len(native), f"{name}: invalid native coverage")
    summary = {}
    for index, label in enumerate(FRAMES):
        pair = [(getattr(row, label).known_at, observed.htf_closes[index]) for row, observed in zip(rows, native)]
        both_known = [(left, right) for left, right in pair if left is not None and right is not None]
        require(all(left == right for left, right in both_known), f"{name} {label}: non-null native clock differs")
        summary[label] = {
            "equal_when_both_known": len(both_known),
            "native_unknown_canonical_present": sum(a is not None and b is None for a, b in pair),
            "canonical_unknown_native_present": sum(a is None and b is not None for a, b in pair),
            "canonical_native_bias_parity": "NOT_COMPARABLE_DIFFERENT_HTF_FORMULAS",
        }
    return summary


def htf_snapshot(row):
    return {label: {
        "source_open_utc": utc(value.open_at) if value.open_at is not None else None,
        "source_known_at_utc": utc(value.known_at) if value.known_at is not None else None,
        "source_close": value.close, "ema21": value.ema21, "ema55": value.ema55, "side": value.side,
    } for label in FRAMES for value in (getattr(row, label),)}


def alignment(row, side):
    biases = (row.h1.side, row.h4.side)
    if None in biases:
        return "DATA MISSING"
    if biases == (side, side):
        return "ALIGNED"
    if biases == (-side, -side):
        return "COUNTERTREND"
    return "MIXED"


def active_confirmations(source, bars, approved):
    """A Shadow break persists only while its original linked retest stays open."""
    by_bar = defaultdict(list)
    for row in source:
        by_bar[row["bar"]].append(row)
    by_close = defaultdict(set)
    for plans, decisions, _, _ in approved.values():
        for event in decisions:
            if event["state"] == "CONFIRMED":
                by_close[event["known_time"]].add(event["side"])
        # The long archived S5 tape records actual admitted signals only.
        for plan in plans:
            if plan.bar < len(bars):
                by_close[bars[plan.bar]["time_close"]].add(plan.side)
    linked = {}
    result = {}
    for i, bar in enumerate(bars):
        now = bar["time_close"]
        current = set(by_close[now])
        for event in by_bar[i]:
            side, model, state = (event[key] for key in ("side", "model", "state"))
            if model == RETEST and state in {"BREAKOUT CONFIRMED", "INVALIDATED", "EXPIRED"}:
                linked.pop(side, None)
            if state == "RETEST WATCH":
                expires = now + (WINDOW_BARS + 1) * (now - bar["time"])
                linked[side] = expires
            if state == "BREAKOUT CONFIRMED":
                current.add(side)
        result[i] = current | {side for side, expires in linked.items() if now < expires}
    return result


def cost_replays(plans, bars, bounds, cost, source, events, regimes, parent, excluded):
    outcomes = portfolio(plans, bars, cost)
    return {
        "closed_campaigns": offers_status(outcomes),
        "metrics": cohort_statistics(outcomes, bars, bounds),
        "opportunities": event_audit(events, regimes, source, outcomes, bars),
        "parent_fills_removed_or_recovered": transition_difference(parent, outcomes),
        "excluded_offers_isolated_outcomes": offers_status([simulate(plan, bars, cost) for plan in excluded]),
        "research_reservations": [(outcome.plan.bar, outcome.release, outcome.plan.id) for outcome in outcomes
                                  if outcome.entry_bar is not None],
        "ledger": [journal(outcome, bars) for outcome in outcomes],
    }


def evaluate_window(name, frame, bars, contexts, lanes, window, protocol, rules):
    begin, split, end = (ms(window[key]) for key in ("score_first", "descriptive_split", "last"))
    bounds = {"full": (begin, end), "early_60": (begin, split), "later_40": (split, end)}
    require((bars[0]["time"], bars[WARMUP]["time"], bars[-1]["time_close"])
            == (ms(window["load_first"]), begin, end), f"{name}: frozen window changed")
    require(end < json.loads(pinned(FORWARD_SEAL))["forward_start_ms"], "Sealed data cannot be evaluated")
    source, original = scan(bars)
    source_prefixes = source_prefix_audit(bars, source, original)
    plans = [plan for plan in original if plan.bar >= WARMUP]
    available = {row["plan_id"]: row for row in source if row["state"] == "ENTRY CANDIDATE"}
    labels = {plan.id: reclaim(plan, available[plan.id], bars) for plan in plans}
    verdicts = {plan.id: admission(plan, available[plan.id], bars, protocol["policies"]["shared"]) for plan in plans}
    competing = active_confirmations(source, bars, lanes)
    conflict_prefixes = (WARMUP, len(bars) // 2, 3 * len(bars) // 4)
    for size in conflict_prefixes:
        prefix = active_confirmations([event for event in source if event["bar"] < size], bars[:size], lanes)
        require(prefix == {index: sides for index, sides in competing.items() if index < size},
                f"{name}: opportunity conflict changed with future bars")
    events, regimes, _ = independent_hours(bars, frame, rules)
    events = [event for event in events if begin <= event["start"] <= end - HOUR]
    native_occupancy = {lane: observed_native_slots(plans, value[2]) for lane, value in lanes.items()}
    parent = {cost: portfolio(plans, bars, pricing) for cost, pricing in COSTS.items()}
    controls = {}
    for lane, (approved, _, _, provenance) in lanes.items():
        controls[lane] = {
            "provenance": provenance,
            "metrics": {cost: cohort_statistics(
                portfolio([plan for plan in approved if plan.bar >= WARMUP], bars, pricing, use_s5_lock=lane == "S5_RAW"),
                bars, bounds) for cost, pricing in COSTS.items()},
        }
    policies = {}
    for key, models in MODELS.items():
        subjects = [plan for plan in plans if plan.model in models]
        by_side = [row for row in source if row["model"] in models]
        offer_rows = []
        selected = []
        for plan in subjects:
            item = available[plan.id]
            reasons, signed_body = verdicts[plan.id]
            at = item["time_ms"]
            expires = item["watch_ms"] + (WINDOW_BARS + 1) * (bars[plan.bar]["time_close"] - bars[plan.bar]["time"])
            require(item["watch_ms"] <= at < expires, f"Source lifecycle expired at admission: {plan.id}")
            context = contexts[plan.bar]
            bias = alignment(context, plan.side)
            conflict = -plan.side in competing[plan.bar]
            if not reasons and not conflict and bias == "ALIGNED":
                selected.append(plan)
            offer_rows.append({
                "id": plan.id, "model": plan.model, "side": SIDES[plan.side], "known_at_utc": utc(at),
                "original_setup_expires_at_utc": utc(expires), "alignment": bias, "htf": htf_snapshot(context),
                "frozen_level": item["level"], "frozen_level_known_at_utc": utc(item["level_known_ms"]),
                "net_room_r": item["net_room_r"], "known_barrier": item["obstacle"], "signed_body_prior_atr": signed_body,
                "original_research_risk": item["risk"], "modeled_friction_r": item["friction_r"],
                "original_geometry_blockers": reasons, "opposing_confirmed_known": conflict,
                "native_occupied_by": {lane: found.get(plan.id, []) for lane, found in native_occupancy.items()},
                "trade_approved": False, "offline_reclaim_label": labels[plan.id],
            })
        qualified = {plan.id for plan in selected}
        analyses = {}
        for cost, pricing in COSTS.items():
            parent_same_model = [row for row in parent[cost] if row.plan.model in models]
            excluded = [plan for plan in subjects if plan.id not in qualified]
            row = cost_replays(selected, bars, bounds, pricing, by_side, events, regimes, parent_same_model, excluded)
            reservations = row.pop("research_reservations")
            row["hypothetical_approved_submission_overlap"] = {
                lane: observed_native_slots([plan for plan in lane_plans if plan.bar >= WARMUP], reservations)
                for lane, (lane_plans, _, _, _) in lanes.items()}
            # A second, independent sensitivity masks observed approved occupancy. Control results stay immutable.
            row["native_occupancy_masks"] = {}
            for lane, occupied in native_occupancy.items():
                available_under_control = [plan for plan in selected if plan.id not in occupied]
                outcomes = portfolio(available_under_control, bars, pricing)
                row["native_occupancy_masks"][lane] = {
                    "offers_blocked": [plan.id for plan in selected if plan.id in occupied],
                    "metrics": expectancy(outcomes, bars, begin, end, bootstrap=True),
                    "closed_campaigns": offers_status(outcomes),
                    "ledger": [journal(outcome, bars) for outcome in outcomes],
                }
            analyses[cost] = row
        policies[key] = {
            "offers": len(subjects), "aligned_before_geometry_and_conflict": sum(row["alignment"] == "ALIGNED" for row in offer_rows),
            "alignment": dict(Counter(row["alignment"] for row in offer_rows)),
            "known_opposing_confirmed": sum(row["opposing_confirmed_known"] for row in offer_rows),
            "known_geometry_and_conflict_and_aligned": len(selected),
            "geometry_reasons": dict(Counter(reason for row in offer_rows for reason in row["original_geometry_blockers"])),
            "original_reclaim": reclaim_counts(subjects, labels),
            "selected_reclaim": reclaim_counts(selected, labels),
            "same_side_regime_context": "Exact finite canonical provider reconstruction, not protected Pine legacy bias",
            "research_only": True,
            "offers_with_evidence": [{**row, "passes_offline_canonical_gate_before_occupancy": row["id"] in qualified}
                                     for row in offer_rows],
            "economics": analyses,
        }
    return {
        "provider_window": window, "original_source_prefixes_exact": source_prefixes,
        "active_confirmation_prefix_lengths_exact": list(conflict_prefixes),
        "directional_hours": dict(Counter(SIDES[row["side"]] for row in events)),
        "parent": {cost: {
            "metrics": cohort_statistics(outcomes, bars, bounds),
            "opportunities": event_audit(events, regimes, source, outcomes, bars),
        } for cost, outcomes in parent.items()},
        "controls": controls, "policies": policies,
        "conflict_scope": "Direct Trend fresh confirmations and original active retest links, plus recorded S5/RC2 confirmations. "
                          "The long archive only has already admitted S5; no complete non-Shadow observation or RC2 ledger there.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prior-m30", type=Path, required=True, help="Exact registered prior M30 provider archive bars.csv")
    parser.add_argument("--v22-logs", type=Path, help="Optional existing exact native indicator_final_M5/M15 witness logs")
    parser.add_argument("--output", type=Path, default=HERE / "evaluation.json")
    args = parser.parse_args()
    rules = json.loads(checked(HERE / "PROTOCOL.json", FROZEN))
    original = json.loads(checked(ROOT / "research/professional_signal_selector/PROTOCOL.json", PROTOCOL_SHA))
    checked(ROOT / rules["existing_context_source"], rules["existing_context_source_sha256"])
    require(HTF_WINDOW == 110 and {str(f): MAX_AGE_MS[f] for f in (60, 240)} == rules["context"]["max_age_ms"],
            "Provider context constants differ from frozen definition")
    checked(args.prior_m30, PRIOR_SHA)
    prior = load_bars(args.prior_m30)
    seal_start = json.loads(pinned(FORWARD_SEAL))["forward_start_ms"]
    require(prior[-1]["time_close"] < seal_start, "Prior context contains sealed data")
    event_rules = json.loads(pinned(EVENT_CONFIG))["event"]
    panels = {name: (frame, bars, lanes, window) for name, frame, bars, lanes, window in read_panels(original)}
    output = {
        "schema": "qirvanta.professional-selector-canonical-htf-reevaluation.v1",
        "status": "INCONCLUSIVE_RESEARCH_ONLY", "protocol_sha256": FROZEN, "original_protocol_sha256": PROTOCOL_SHA,
        "prior_m30_sha256": PRIOR_SHA, "prior_m30_bars": len(prior),
        "execution": original["execution"], "untouched_forward_status": "NOT_AVAILABLE_NOT_ACCESSED",
        "scope": "Archived historical causal finite H1/H4, unchanged Shadow brackets and approved S5/RC2 ledgers. "
                 "Results are hypothetical quote-side next-open campaign scenarios, not live/native broker fills or an unseen test.",
        "provider_candle_checks": {}, "windows": {},
    }
    all_contexts = {}
    for name, (frame, bars, lanes, window) in panels.items():
        output["provider_candle_checks"][f"{name}_vs_M30"] = check_aggregation(bars, prior)
        contexts, qa = canonical_context(bars, prior)
        all_contexts[name] = {row.bar.open_at: row for row in contexts}
        qa["rc2_source_clocks"] = verify_recent_clocks(name, contexts)
        if args.v22_logs:
            witness = args.v22_logs / f"indicator_final_M{frame}.csv"
            checked(witness, V22_PINS[frame])
            overlap, evidence = compare_sources(contexts, witness)
            qa["native_v22_source_witness"] = {"sha256": V22_PINS[frame], "chart_overlap": overlap, **evidence}
        output["windows"][name] = {"context_quality": qa,
                                   **evaluate_window(name, frame, bars, contexts, lanes, window, original, event_rules)}
        print(f"{name}: {qa['scored_both_frames_known']}/{qa['scored_bars']} scored H1/H4 contexts", flush=True)
    for a, b in (("M5_long", "M5_matched"), ("M5_long", "M15_matched"), ("M5_matched", "M15_matched")):
        left, right = all_contexts[a], all_contexts[b]
        shared = left.keys() & right.keys()
        require(shared and all((left[stamp].h1, left[stamp].h4) == (right[stamp].h1, right[stamp].h4)
                               for stamp in shared), f"Canonical snapshots differ across {a} and {b}")
        proof = {"identical_contexts_on_shared_opens": len(shared)}
        lower, higher = panels[a][1], panels[b][1]
        if panels[a][0] == panels[b][0]:
            require(all(left[stamp].bar == right[stamp].bar for stamp in shared), "Overlapping same-frame OHLC differs")
            proof["exact_provider_ohlc"] = len(shared)
        else:
            proof["exact_provider_ohlc"] = check_aggregation(lower, higher)
        output["provider_candle_checks"][f"{a}_vs_{b}"] = proof
    args.output.write_text(json.dumps(output, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n",
                           encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()

