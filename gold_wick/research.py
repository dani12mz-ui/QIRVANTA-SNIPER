"""Compare fixed wick hypotheses with the registered OANDA research books."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import asdict, replace
import gzip
import json
from math import isfinite
from pathlib import Path
from statistics import mean, median

from gold_precision.experiments import _inference, _verify_pin
from gold_precision.retest_research import blocked_ledger, stressed_cash
from gold_pro.archive import REGISTRATION, ROOT, _hash, _ms, _read, _read_json, _write
from gold_pro.archive import cash_campaign, load_archive, native_outcomes
from gold_pro.evaluate import _window
from gold_pro.opportunity import HOUR, event_audit, scan as scan_gold
from gold_pro.replay import Costs, Part, portfolio, simulate
from research.validation.statistics import cash_metrics, holm
from unified.tools.evaluate_v22_signals import feature_rows
from unified.tools.gold_market_data import context_rows

from .engine import scan


PROTOCOL = Path(__file__).with_name("research_protocol.json")
FORWARD_SEAL = ROOT / "docs/gold_pro_evidence/forward_seal_m5.json"
EVENT_REG = ROOT / "docs/v21_evidence/event_evaluation_registration.json"
FEATURE_REG = ROOT / "docs/v22_evidence/historical_registration.json"
WATCH = {"BUY_WATCH", "SELL_WATCH"}
BREAKOUT = {"BUY_BREAKOUT_CONFIRMED", "SELL_BREAKOUT_CONFIRMED"}


def scoring_windows(registration):
    config = {key: _ms(value) for key, value in registration["windows_utc"].items()}
    start, end = config["full_start"], config["full_end_inclusive"] + 1
    return {
        "full": (start, end),
        "development": (start, config["development_end_exclusive"]),
        "later_archive": (config["later_start"], end),
    }


def registered_cost(config):
    fields = ("tick", "pointvalue", "commission_per_unit_per_fill",
              "adverse_ticks_market_and_stop", "round_trip_spread",
              "stress_extra_round_trip_spread", "stress_extra_slip_each_side",
              "stress_commission_fraction")
    return Costs(*(config["cost"][key] for key in fields))


def wick_decisions(events, *, include_retest=False):
    states = {**dict.fromkeys(WATCH, "WATCH"), **dict.fromkeys(BREAKOUT, "TREND_CONFIRMED"),
              "RISK_REJECTED": "RISK_REJECTED"}
    decisions = [{**row, "state": states[row["state"]]} for row in events if row["state"] in states]
    if include_retest:
        decisions.extend({**row, "state": "SECOND_CHANCE"} for row in events
                         if row["state"] == "ENTRY_VALID" and row["model"] == "WICK_RETEST")
    return decisions


def causal_wick_filter(plans, features):
    kept, excluded = [], []
    for plan in plans:
        key = "buy_sequence_count" if plan.side == 1 else "sell_sequence_count"
        candle = features[plan.bar]
        wick_confirmation = candle["sequence_ready"] and candle[key] >= 2
        destination = excluded if plan.model == "BREAKOUT" and not wick_confirmation else kept
        destination.append(plan)
    return kept, excluded


def fail_exit(outcome, failed_bar, bars, costs):
    """Keep all settled parts. A confirmed reclaim submits a next-open market exit."""
    i, plan = outcome.entry_bar, outcome.plan
    if i is None or failed_bar is None or not i <= failed_bar < outcome.release:
        return outcome
    execution = failed_bar + 1
    if execution >= len(bars):
        return outcome
    opening_exit = {"STOP_GAP", "TIME_EXIT", "GAP_OR_FILL_INVALID"}
    if (outcome.release == execution and
            all(p.reason in opening_exit for p in outcome.exits if p.bar == execution)):
        return outcome
    prior = tuple(p for p in outcome.exits if p.bar <= failed_bar)
    still_open = plan.units - sum(part.units for part in prior)
    if still_open <= 1e-10:
        return outcome
    opening = bars[execution]["open"]
    price = opening - plan.side * costs.slip
    legs = round(still_open * 3 / plan.units)
    commission = 2 * costs.commission * plan.units / 3
    spread = costs.spread * costs.pointvalue * plan.units / 3
    net = plan.side * (price - outcome.fill) * (plan.units / 3) * costs.pointvalue - commission - spread
    close = Part(execution, price, plan.units / 3, "RECLAIM_NEXT_OPEN", commission, net)
    return replace(outcome, state="CLOSED", release=execution, exits=prior + (close,) * legs)


def cash_by_cost(campaigns, costs):
    ordered = sorted(campaigns, key=lambda c: (c.exit_ms, c.entry_ms, c.id))
    results = {}
    for label, stress in costs.items():
        cash = [stressed_cash(campaign, stress) for campaign in ordered]
        r = [value / campaign.risk_cash for campaign, value in zip(ordered, cash)]
        wins, losses = [v for v in cash if v > 0], [v for v in cash if v < 0]
        results[label] = {
            **cash_metrics(cash), "net_r": sum(r), "expectancy_r": mean(r) if r else None,
            "mean_win_usd": mean(wins) if wins else None,
            "mean_loss_usd": mean(losses) if losses else None,
        }
    return results


def closed_score(outcomes, bars, costs):
    return [cash_campaign(x, bars, costs, native=x.plan.model == "S5") for x in outcomes if x.state == "CLOSED"]


def outcomes_changed(before, after, directly_excluded=()):
    old, new = ({c.id: c for c in group} for group in (before, after))
    removed, added = old.keys() - new.keys(), new.keys() - old.keys()
    excluded = old.keys() & set(directly_excluded)
    changed = sorted(pid for pid in old.keys() & new.keys()
                     if abs(old[pid].net_cash - new[pid].net_cash) > 1e-8)
    return {
        "directly_excluded_closed": sorted(excluded),
        "directly_excluded_winners": sum(old[pid].net_cash > 0 for pid in excluded),
        "directly_excluded_losers": sum(old[pid].net_cash < 0 for pid in excluded),
        "removed_closed_winners": sum(old[pid].net_cash > 0 for pid in removed),
        "removed_closed_losers": sum(old[pid].net_cash < 0 for pid in removed),
        "removed_ids": sorted(removed), "added_ids": sorted(added), "changed_outcome_ids": changed,
        "net_usd_change": sum(c.net_cash for c in after) - sum(c.net_cash for c in before),
        "net_r_change": sum(c.net_cash / c.risk_cash for c in after) - sum(c.net_cash / c.risk_cash for c in before),
    }


def summarize_events(rows, observations, start, end, off_event):
    window = [r for r in rows if start <= r["time_ms"] and r["time_ms"] + HOUR < end]
    summaries = {}
    for label, side in (("all", None), ("BUY", 1), ("SELL", -1)):
        events = [r for r in window if side is None or r["side"] == side]
        counts = {"independent_events": len(events)}
        counts.update({key: sum(e[key] for e in events) for key in ("detected", "actionable", "filled")})
        counts["undetected"] = counts["independent_events"] - counts["detected"]
        counts["missed_fill"] = counts["independent_events"] - counts["filled"]
        counts["actionable_without_fill"] = sum(r["actionable"] and not r["filled"] for r in events)
        for key in ("watch_at", "confirmed_at", "executable_at", "fill_at"):
            delays = [(r[key] - r["time_ms"]) / 60_000 for r in events if r[key] is not None]
            counts[f"median_{key}_delay_minutes"] = median(delays) if delays else None
        captures = [r["capture_fraction"] for r in events if r["capture_fraction"] is not None]
        counts["mean_capture_fraction"] = mean(captures) if captures else None
        summaries[label] = counts
    matched = {(r["time_ms"] // HOUR, r["side"]) for r in rows}
    for state in ("WATCH", "TREND_CONFIRMED"):
        candidates = [d for d in observations if d["state"] == state and start <= d["time_ms"] < end]
        summaries[f"outside_event_{state.lower()}"] = sum(
            (d["time_ms"] // HOUR, d["side"]) not in matched for d in candidates)
    summaries["outside_event_closed_losses"] = sum(x["losses"] for x in off_event if start <= x["time_ms"] < end)
    return summaries


def paired_event_timing(reference, candidate):
    by_id = {row["id"]: row for row in reference}
    result = {}
    for key in ("watch_at", "confirmed_at", "executable_at", "fill_at"):
        deltas = [(row[key] - by_id[row["id"]][key]) / 60_000 for row in candidate
                  if row[key] is not None and row["id"] in by_id and by_id[row["id"]][key] is not None]
        result[key] = {
            "same_events": len(deltas), "candidate_earlier": sum(d < 0 for d in deltas),
            "candidate_later": sum(d > 0 for d in deltas),
            "median_candidate_minus_reference_minutes": median(deltas) if deltas else None,
        }
    return result


def event_diagnostics(bars, wick, windows):
    complete_volume = [b["volume"] for b in bars if b.get("volume") is not None
                       and isfinite(b["volume"]) and b["volume"] > 0]
    reports = {}
    for window, (start, end) in windows.items():
        events = [row for row in wick.events if start <= row["time_ms"] < end]
        states = Counter(row["state"] for row in events)
        def ids(kinds):
            return {row["formation_id"] for row in events if row["state"] in kinds}

        plans = [row for row in events if row["state"] == "ENTRY_VALID"]
        delays = defaultdict(list)
        for row in plans:
            delays[row["model"]].append((row["time_ms"] - row["first_known_ms"]) / 60_000)
        confirmed, reclaimed = ids(BREAKOUT), ids({"FALSE_BREAKOUT"})
        attempts = ids({"BREAKOUT_PENDING"})
        watches = {side: [row for row in events if row["state"] == f"{side}_WATCH"] for side in ("BUY", "SELL")}
        reports[window] = {
            "states": dict(states), "watch_formations": len(ids(WATCH)),
            "provisional_penetration_formations": len(attempts),
            "confirmed_breakout_formations": len(confirmed),
            "breakout_attempt_formations": len(attempts | confirmed),
            "failed_breakout_formations": len(reclaimed),
            "breakouts_confirmed_and_reclaimed_in_slice": len(confirmed & reclaimed),
            "provisional_formations_expired_without_confirmation": len((attempts - confirmed) & ids({"FORMATION_EXPIRED"})),
            "expired_formations": len(ids({"FORMATION_EXPIRED"})),
            "valid_plans": len(plans),
            "risk_rejection_reasons": dict(Counter(row["reason"] for row in events if row["state"] == "RISK_REJECTED")),
            "median_recognition_to_plan_minutes": {k: median(v) for k, v in delays.items()},
            "reversal_watches": {
                side: {"count": len(selected), "near_prior_structure": sum(row["at_structure"] for row in selected)}
                for side, selected in watches.items()
            },
        }
    return {"volume": {"bars": len(bars), "positive_finite_bars": len(complete_volume),
                       "min": min(complete_volume, default=None), "max": max(complete_volume, default=None)},
            "periods": reports}


def evaluate_frame(minutes, frame, earlier, tape, config, event_cfg, signals, windows, cost, equity):
    bars, original, market_hash = frame
    context = [(c.h1.side, c.h4.side) for c in context_rows(bars, tick=cost.tick, earlier=earlier)]
    features = feature_rows(bars, signals, earlier=earlier)
    baseline = native_outcomes(original, cost)
    s5_plans = [o.plan for o in baseline]
    frozen_s5 = {o.plan.id: o for o in baseline}
    s5_decisions = [{"time_ms": bars[p.bar]["time_close"], "side": p.side, "state": "TREND_CONFIRMED",
                     "reason": "ARCHIVED_S5_ADMISSION", "model": "S5"} for p in s5_plans]
    _verify_pin(tape, baseline, [], "S5")
    lanes = {"S5": (s5_plans, baseline, [], s5_decisions)}
    original_inputs = {}
    frozen = dict(frozen_s5)
    for mode in ("FAST", "CONFIRMED"):
        decisions, plans = scan_gold(bars, features, context, config["opportunity"], cost, equity, mode=mode)
        original_inputs[mode] = (decisions, plans)
        frozen.update({p.id: simulate(p, bars, cost, minutes) for p in plans})
        individual = portfolio(plans, bars, cost, minutes, equity, frozen=frozen)
        joint = portfolio(s5_plans + plans, bars, cost, minutes, equity, frozen=frozen)
        _verify_pin(tape, *individual, mode)
        _verify_pin(tape, *joint, "S5_PLUS_" + mode)
        lanes["GOLD_" + mode] = (plans, *individual, decisions)
        lanes["S5_PLUS_GOLD_" + mode] = (s5_plans + plans, *joint, s5_decisions + decisions)

    wick = scan(bars, cost, minutes=minutes, context=context, equity=equity)
    observations = wick_decisions(wick.events)
    metadata = {r["plan_id"]: r for r in wick.events if r["state"] == "ENTRY_VALID"}
    if set(metadata) != {p.id for p in wick.plans}:
        raise ValueError("Wick plan and decision tape disagree")
    failed = {r["formation_id"]: r["bar"] for r in wick.events if r["state"] == "FALSE_BREAKOUT"}
    breakouts = [p for p in wick.plans if p.model == "WICK_BREAKOUT"]
    retests = [p for p in wick.plans if p.model == "WICK_RETEST"]
    frozen.update({p.id: simulate(p, bars, cost, minutes) for p in wick.plans})

    def add(name, plans, decisions, outcomes=frozen):
        lanes[name] = (plans, *portfolio(plans, bars, cost, minutes, equity, frozen=outcomes), decisions)

    add("BREAKOUT_A", breakouts, observations)
    add("RETEST_A", retests, wick_decisions(wick.events, include_retest=True))
    directly_excluded = {}
    for name, key in (("BREAKOUT_B", "volume_dominance"), ("BREAKOUT_C", "volume_activity"),
                      ("BREAKOUT_HTF", "htf_aligned")):
        selected = [p for p in breakouts if metadata[p.id][key] is True]
        directly_excluded[name] = {p.id for p in breakouts if metadata[p.id][key] is not True}
        add(name, selected, observations)
    modified = {p.id: fail_exit(frozen[p.id], failed.get(metadata[p.id]["formation_id"]), bars, cost)
                for p in breakouts}
    add("BREAKOUT_A_RECLAIM_EXIT", breakouts, observations, modified)
    for mode, (decisions, plans) in original_inputs.items():
        selected, excluded = causal_wick_filter(plans, wick.features)
        name = f"GOLD_{mode}_WICK_FILTER"
        directly_excluded[name] = {p.id for p in excluded}
        add(name, selected, decisions)
    add("S5_PLUS_BREAKOUT_A", s5_plans + breakouts, s5_decisions + observations)
    gold_fast_decisions, gold_fast_plans = original_inputs["FAST"]
    add("GOLD_FAST_PLUS_BREAKOUT_A", gold_fast_plans + breakouts, gold_fast_decisions + observations)
    return score_frame(minutes, market_hash, bars, wick, lanes, frozen, modified, directly_excluded,
                       event_cfg, windows, cost, config["research"])


def score_frame(minutes, market_hash, bars, wick, lanes, frozen, modified, excluded, event_cfg, windows, cost, config):
    if set(lanes) != set(config["lanes"]) | set(config["controls"]):
        raise ValueError("Unregistered wick research lane")
    closed, reports, ledger = {}, {}, {}
    shadow_cash = {c.id: c for c in closed_score(frozen.values(), bars, cost)}
    reclaim_shadow_cash = {c.id: c for c in closed_score(modified.values(), bars, cost)}
    case = config["cost_cases"]
    for name, (plans, outcomes, rejected, observations) in lanes.items():
        event_rows, outside, _ = event_audit(bars, minutes, event_cfg, observations, plans, outcomes, rejected)
        score = closed[name] = closed_score(outcomes, bars, cost)
        common_start, common_end = windows["full"]
        allowed = [p for p in plans if common_start <= bars[p.bar]["time_close"] < common_end]
        window_fills = [o for o in outcomes if o.entry_bar is not None and
                        common_start <= bars[o.entry_bar]["time"] < common_end]
        matched = {(r["time_ms"] // HOUR, r["side"]) for r in event_rows}
        off_fills = [o for o in window_fills if (bars[o.entry_bar]["time"] // HOUR, o.plan.side) not in matched]
        blocked = [(p, reason) for p, reason in rejected if common_start <= bars[p.bar]["time_close"] < common_end]
        shadow = reclaim_shadow_cash if name == "BREAKOUT_A_RECLAIM_EXIT" else shadow_cash
        profitable = blocked_ledger(blocked, shadow, bars, case)
        realized_risk = [o.plan.side * (o.fill - o.plan.stop) * o.plan.units * cost.pointvalue
                         for o in window_fills]
        positive_risk = [risk for risk in realized_risk if risk > 0]
        reports[name] = {
            "valid_offers": len(allowed), "actual_fills": len(window_fills),
            "fills_outside_directional_event": len(off_fills),
            "censored_end": sum(o.state.startswith("CENSORED") for o in outcomes),
            "median_planned_price_risk_usd": median(
                abs(p.reference - p.stop) * p.units * cost.pointvalue for p in allowed) if allowed else None,
            "median_actual_positive_price_risk_usd": median(positive_risk) if positive_risk else None,
            "fills_beyond_or_at_original_stop": sum(risk <= 0 for risk in realized_risk),
            "blocked": {key: value for key, value in profitable.items() if key != "profitable_isolated_offers"},
            "metrics": {window: {
                label: cash_by_cost([c for c in _window(score, *bounds) if side is None or c.side == side], case)
                for label, side in (("all", None), ("BUY", 1), ("SELL", -1))
            } for window, bounds in windows.items()},
            "coverage": {key: summarize_events(event_rows, observations, *bounds, outside)
                         for key, bounds in windows.items()},
        }
        ledger[name] = {
            "event_hours": [r for r in event_rows
                            if common_start <= r["time_ms"] and r["time_ms"] + HOUR < common_end],
            "outcomes": [asdict(o) for o in outcomes if
                         common_start <= bars[o.plan.bar]["time_close"] < common_end or o in window_fills],
            "rejections": [{"id": p.id, "reason": reason, "known_ms": bars[p.bar]["time_close"]}
                           for p, reason in blocked],
            "profitable_isolated_offers": profitable["profitable_isolated_offers"],
        }
    spec = config["primary_inference"]
    comparisons, detail = {}, {}
    for trial, base in spec["contrasts"].items():
        before, after = (_window(closed[key], *windows["full"]) for key in (base, trial))
        changed = outcomes_changed(before, after, excluded.get(trial, ()))
        detail[trial] = {key: value for key, value in changed.items() if key.endswith("_ids") or
                         key == "directly_excluded_closed"}
        comparisons[trial] = {"control": base, **{key: value for key, value in changed.items()
                                                  if key not in detail[trial]}}
        if minutes == config["primary_minutes"]:
            comparisons[trial]["later_archive_stress"] = _inference(
                closed[base], closed[trial], *windows["later_archive"], spec)
    if minutes == config["primary_minutes"]:
        adjusted = holm([row["later_archive_stress"]["raw_one_sided_p"] for row in comparisons.values()])
        for row, p in zip(comparisons.values(), adjusted):
            row["later_archive_stress"]["holm_p"] = p
    lifecycle = event_diagnostics(bars, wick, windows)
    timing = {name: paired_event_timing(ledger[name]["event_hours"], ledger["BREAKOUT_A"]["event_hours"])
              for name in ("S5", "GOLD_FAST", "GOLD_CONFIRMED")}
    return ({"market_sha256": market_hash, "bar_count": len(bars), "first_open": bars[0]["time"],
             "last_close": bars[-1]["time_close"], "pinned_controls": "EXACT_OUTCOME_AND_REJECTION_TAPES",
             "wick_lifecycle": lifecycle, "lanes": reports, "contrasts": comparisons,
             "paired_wick_breakout_a_timing": timing},
            {"lanes": ledger, "contrast_ids": detail, "wick_events": wick.events})


def run(registration, gold, frames, tape, event_cfg, signals):
    windows = scoring_windows(registration)
    cost = registered_cost(gold)
    equity = gold["study"]["starting_equity_usd"]
    m30 = frames[30][0]
    closes = [b["time_close"] for b in m30]
    summaries, ledgers = {}, {}
    for minutes in registration["frames"]:
        frame = frames[minutes]
        earlier = m30[:bisect_right(closes, frame[0][0]["time"])]
        summary, ledger = evaluate_frame(minutes, frame, earlier, tape[str(minutes)],
                                         {**gold, "research": registration}, event_cfg, signals,
                                         windows, cost, equity)
        summaries[str(minutes)], ledgers[str(minutes)] = summary, ledger
    return {"schema": registration["schema"], "status": "EXPERIMENTAL",
            "scope": "ALREADY_INSPECTED_ARCHIVE", "promotion": "INCONCLUSIVE_NO_UNSEEN_MATCHED_DATA",
            "windows_utc_ms": windows, "cost_cases": registration["cost_cases"],
            "drawdown_basis": "Campaign-close cash only. Native intrabar equity drawdown was not recreated.",
            "frames": summaries}, ledgers


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("../mission"))
    parser.add_argument("--logs-dir", type=Path, default=Path("../tv-final"))
    parser.add_argument("--tape", type=Path, default=Path("../../outputs/gold_pro/archive_release_tape.json.gz"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ledger", type=Path)
    args = parser.parse_args()
    registration = _read_json(PROTOCOL)
    pins = registration["pins"]
    gold = _read_json(_read(REGISTRATION, pins["gold_registration"]))
    event = _read_json(_read(EVENT_REG, pins["event_registration"]))["event"]
    signals = _read_json(_read(FEATURE_REG, pins["feature_registration"]))["signals"]
    _read(FORWARD_SEAL, pins["forward_seal"])
    tape = json.loads(gzip.decompress(_read(args.tape, pins["archive_tape"]).read_bytes()))
    report, ledgers = run(registration, gold, load_archive(gold, args.data_root, args.logs_dir), tape, event, signals)
    report["source"] = {**pins, "protocol_sha256": _hash(PROTOCOL), "runner_sha256": _hash(Path(__file__)),
                        "engine_sha256": _hash(Path(__file__).with_name("engine.py"))}
    if args.ledger:
        _write(args.ledger, ledgers, compressed=True)
        report["ledger_sha256"] = _hash(args.ledger)
    _write(args.out, report, compressed=args.out.suffix == ".gz")
    print("EXPERIMENTAL archive | exact pinned S5 and GOLD controls | M5, M15, M30")


if __name__ == "__main__":
    main()
