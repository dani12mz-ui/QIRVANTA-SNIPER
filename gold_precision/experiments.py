"""Reproduce the 123-campaign FAST archive and compare preregistered isolated policies."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import asdict
import gzip
import json
from pathlib import Path
from statistics import mean, median

from gold_pro.archive import ROOT, REGISTRATION, _hash, _read, _read_json, _write
from gold_pro.archive import cash_campaign, load_archive, native_outcomes
from gold_pro.evaluate import _summarize, _window
from gold_pro.opportunity import event_audit, scan
from gold_pro.replay import Costs, simulate
from research.validation.statistics import block_bootstrap, holm, sign_flip_p
from unified.tools.evaluate_v22_signals import feature_rows
from unified.tools.gold_market_data import context_rows

from .execution import book, capped_plan, friction_price, policy_simulate


PROTOCOL = Path(__file__).with_name("experiment_protocol.json")
DAY = 86_400_000
SOURCE_REG = ROOT / "docs/v22_evidence/historical_registration.json"
STAGE_ONE = ROOT / "docs/precision_evidence/fast_diagnostic.json"


def archived_inputs(data_root, logs_dir):
    config = _read_json(REGISTRATION)
    frame = load_archive(config, data_root, logs_dir)
    bars, original, market_hash = frame[5]
    m30 = frame[30][0]
    history = m30[:bisect_right([bar["time_close"] for bar in m30], bars[0]["time"])]
    v22 = _read_json(SOURCE_REG)
    features = feature_rows(bars, v22["signals"], earlier=history)
    cc = config["cost"]
    costs = Costs(cc["tick"], cc["pointvalue"], cc["commission_per_unit_per_fill"],
                  cc["adverse_ticks_market_and_stop"], cc["round_trip_spread"],
                  cc["stress_extra_round_trip_spread"], cc["stress_extra_slip_each_side"],
                  cc["stress_commission_fraction"])
    context = [(x.h1.side, x.h4.side) for x in context_rows(bars, tick=costs.tick, earlier=history)]
    equity = config["study"]["starting_equity_usd"]
    decisions, plans = scan(bars, features, context, config["opportunity"], costs, equity, mode="FAST")
    event = _read_json(ROOT / config["opportunity"]["event_reference"])["event"]
    return bars, features, costs, equity, decisions, plans, native_outcomes(original, costs), market_hash, event


def _plan_details(decisions):
    return {row["plan_id"]: row for row in decisions if row["state"] == "ENTRY_AVAILABLE"}


def filtered_plans(name, plans, details, bars, features, costs):
    kept, seen_breaks = [], set()
    for plan in sorted(plans, key=lambda p: (p.bar, p.id)):
        i, side = plan.bar, plan.side
        data, feature = details[plan.id], features[i]
        row = bars[i]
        close, prior = row["close"], bars[i - 1]["close"]
        risk = side * (plan.reference - plan.stop)
        barrier = data["known_barrier"]
        gross_room = risk if barrier is None else side * (barrier - plan.reference)
        true_range = max(row["high"] - row["low"], abs(row["high"] - prior), abs(row["low"] - prior))
        root = (side, data["root_watch_ms"])
        gates = {
            "FIRST_BREAK": plan.model == "BREAKOUT" and feature.bos == side and root not in seen_breaks,
            "DISPLACEMENT": feature.displacement == side,
            "PERSISTENCE": min(side * (close - prior), side * (prior - bars[i - 2]["close"])) > 0,
            "ROOM_085": gross_room >= .85 * risk,
            "RECLAIM": plan.model in ("PULLBACK", "RETEST"),
            "FRESH_4": data["signal_age_bars"] <= 4 and data["signal_age_minutes"] <= 20,
            "EXTENSION_110": data["extension_atr"] <= 1.10,
            "HTF_BOTH": data["h1"] == side and data["h4"] == side,
            "VOL_STABLE": true_range <= 1.50 * plan.atr,
            "COST_015": friction_price(costs) <= .15 * risk,
        }
        if gates[name]:
            kept.append(plan)
        if plan.model == "BREAKOUT" and feature.bos == side:
            seen_breaks.add(root)
    return kept


def recovery_plans(plans, decisions, reference, rejections, bars, tick):
    """Use only prior reference-book events, with a fresh valid geometry and plan identity."""
    details = _plan_details(decisions)
    anchors = defaultdict(list)
    for row in decisions:
        if row["state"] in ("RISK_REJECTED", "OPPORTUNITY_EXPIRED"):
            anchors[row["side"]].append((row["bar"], row["state"], row.get("plan_id"), row["root_watch_ms"], None))
    for plan, reason in rejections:
        if reason == "POSITION_OCCUPIED":
            anchors[plan.side].append((plan.bar, reason, plan.id, None, plan))
    for outcome in reference:
        if outcome.state == "CLOSED":
            anchors[outcome.plan.side].append((outcome.release, "CAMPAIGN_CLOSED", outcome.plan.id, None, outcome.plan))

    found = {}
    for plan in plans:
        if plan.model == "BREAKOUT":
            continue
        data = details[plan.id]
        close_ms = bars[plan.bar]["time_close"]
        nearby = [item for item in anchors[plan.side]
                  if 0 < plan.bar - item[0] <= 12
                  and close_ms - bars[item[0]]["time_close"] <= 60 * 60_000]
        prior_trade = max((a for a in nearby if a[1] == "CAMPAIGN_CLOSED"),
                          key=lambda x: (x[0], x[2]), default=None)
        if prior_trade is not None:
            old = prior_trade[-1]
            if abs(plan.reference - old.reference) < tick - 1e-8 or abs(plan.stop - old.stop) < tick - 1e-8:
                continue
        eligible = []
        for anchor in nearby:
            anchor_bar, kind, old_id, root, old = anchor
            if old_id == plan.id or (old and (abs(plan.reference - old.reference) < tick - 1e-8 or
                                             abs(plan.stop - old.stop) < tick - 1e-8)):
                continue
            if kind == "RISK_REJECTED" and (not data["second_chance"] or data["root_watch_ms"] != root):
                continue
            eligible.append(anchor)
        if eligible:
            anchor = max(eligible, key=lambda x: (x[0], x[1], x[2] or ""))
            found[plan.id] = {"prior_kind": anchor[1], "prior_id": anchor[2],
                              "prior_known_at_utc_ms": bars[anchor[0]]["time_close"],
                              "decision_utc_ms": close_ms, "model": plan.model}
    return found


def _score(outcomes, bars, costs):
    return [cash_campaign(outcome, bars, costs, native=outcome.plan.model == "S5")
            for outcome in outcomes if outcome.state == "CLOSED"]


def _periods(campaigns, start, split, end, embargo):
    bounds = ((start, end), (start, split), (split + embargo, end))
    labels = ("full", "development", "later_archive")
    return {name: _summarize(_window(campaigns, a, b), a, b) for name, (a, b) in zip(labels, bounds)}


def _event_counts(events, start, end):
    events = [e for e in events if start <= e["time_ms"] and e["time_ms"] + 3_600_000 <= end]
    return {"calendar": len(events), "detected": sum(e["detected"] for e in events),
            "actionable": sum(e["actionable"] for e in events), "filled": sum(e["filled"] for e in events),
            "capture_fraction_mean": mean(fractions) if (fractions := [
                e["capture_fraction"] for e in events if e["capture_fraction"] is not None]) else None}


def _lost_and_saved(control, trial):
    base, alt = ({c.id: c for c in values} for values in (control, trial))
    removed = [c for cid, c in base.items() if cid not in alt]
    added = [c for cid, c in alt.items() if cid not in base]
    deltas = [(c, alt[cid], alt[cid].net_cash - c.net_cash) for cid, c in base.items() if cid in alt]
    improved_losers = [delta for c, _, delta in deltas if c.net_cash < 0 and delta > 1e-8]
    hurt_winners = [-delta for c, _, delta in deltas if c.net_cash > 0 and delta < -1e-8]
    premature = sum(c.net_cash > 0 and delta < -1e-8 and other.exit_ms < c.exit_ms
                    for c, other, delta in deltas)
    losers = [c.net_cash / c.risk_cash for c in trial if c.net_cash < 0]
    return {"closed_reference_winners_excluded": sum(c.net_cash > 0 for c in removed),
            "closed_reference_losers_excluded": sum(c.net_cash < 0 for c in removed),
            "excluded_winner_usd": sum(c.net_cash for c in removed if c.net_cash > 0),
            "excluded_loss_magnitude_usd": -sum(c.net_cash for c in removed if c.net_cash < 0),
            "added_closed_campaigns": len(added), "added_net_usd": sum(c.net_cash for c in added),
            "matched_losing_trades_improved": len(improved_losers), "saved_on_matched_losers_usd": sum(improved_losers),
            "matched_winners_hurt": len(hurt_winners), "removed_from_matched_winners_usd": sum(hurt_winners),
            "earlier_exits_hurting_reference_winners": premature,
            "median_loss_r": median(losers) if losers else None}


def _inference(control, trial, first, end, config):
    a, b = _window(control, first, end), _window(trial, first, end)
    count = config["block_days"]
    days = [0.] * (((end - 1) // DAY) - first // DAY + 1)
    for sign, lane in ((1, b), (-1, a)):
        for campaign in lane:
            days[campaign.exit_ms // DAY - first // DAY] += sign * campaign.stressed_net_cash / campaign.risk_cash
    blocks = [sum(days[i:i + count]) for i in range(0, len(days), count)]
    repetitions, seed = config["repetitions"], config["seed"]
    return {"paired_blocks": len(blocks), "block_days": count, "calendar_days": len(days),
            "stress_r_delta": sum(blocks), "raw_one_sided_p": sign_flip_p(blocks, repetitions, seed),
            "mean_stress_r_delta_per_block": block_bootstrap(blocks, repetitions, block=1, seed=seed)}


def _decision(control, trial, baseline_events, candidate_events, config):
    failures = []
    if min(control["n"], trial["n"]) < config["minimum_campaigns"]:
        failures.append("SAMPLE_TOO_SMALL")
    if trial["n"] < config["min_frequency_fraction"] * control["n"]:
        failures.append("FREQUENCY_LOST")
    if (control["expectancy_r"] is None or trial["expectancy_r"] is None or
            trial["expectancy_r"] <= 0 or trial["expectancy_r"] - control["expectancy_r"] < config["minimum_expectancy_gain_r"]):
        failures.append("NET_R")
    if trial["stressed_net_cash"] < control["stressed_net_cash"]:
        failures.append("STRESS_USD")
    if any(trial[k] > config["max_drawdown_fraction"] * control[k] + 1e-8
           for k in ("campaign_close_drawdown", "stressed_drawdown_cash")):
        failures.append("DRAWDOWN")
    if candidate_events["filled"] < config["min_event_fill_fraction"] * baseline_events["filled"]:
        failures.append("OPPORTUNITY_COVERAGE")
    return failures


def _verify_pin(tape, results, rejected, label):
    record = tape[label]
    replayed = json.loads(json.dumps([asdict(outcome) for outcome in results]))
    blocks = [{"id": plan.id, "bar": plan.bar, "reason": reason} for plan, reason in rejected]
    if replayed != record["outcomes"] or blocks != record["portfolio_rejections"]:
        raise ValueError(f"{label} outcome or rejection tape differs from the pinned reference")


def run(config, inputs, archive):
    bars, features, costs, equity, decisions, plans, native, market_hash, event = inputs
    start, end = bars[0]["time"], bars[-1]["time_close"] + 1
    split = bars[int(len(bars) * config["split"]["bar_fraction"])]["time"]
    embargo = config["split"]["embargo_minutes"] * 60_000
    later = split + embargo
    s5_plans = [o.plan for o in native]
    frozen_s5 = {o.plan.id: o for o in native}
    frozen_fast = {p.id: simulate(p, bars, costs, 5) for p in plans}
    control, blocked = book(plans, costs, equity, outcomes=frozen_fast)
    shared_control, shared_blocked = book(s5_plans + plans, costs, equity,
                                         outcomes={**frozen_fast, **frozen_s5})
    _verify_pin(archive, native, [], "S5")
    _verify_pin(archive, control, blocked, "FAST")
    _verify_pin(archive, shared_control, shared_blocked, "S5_PLUS_FAST")
    if sum(o.state == "CLOSED" for o in control) != config["expected_fast_campaigns"]:
        raise ValueError("FAST control campaign count differs")
    recovery = recovery_plans(plans, decisions, control, blocked, bars, costs.tick)
    details = _plan_details(decisions)
    s5_signals = [{"time_ms": bars[p.bar]["time_close"], "side": p.side, "state": "TREND_CONFIRMED",
                   "reason": "ARCHIVED_S5_ADMISSION", "model": "S5"} for p in s5_plans]
    s5_scores, base_scores, shared_scores = (_score(values, bars, costs)
                                              for values in (native, control, shared_control))
    s5_closed = {c.id: c for c in s5_scores}

    def audit(selection, filled, rejected, *, shared=False):
        signal_source = decisions + s5_signals if shared else decisions
        return event_audit(bars, 5, event, signal_source, selection, filled, rejected)

    baseline_event, _, baseline_event_meta = audit(plans, control, blocked)
    baseline_metrics = _periods(base_scores, start, split, end, embargo)
    baseline_later_events = _event_counts(baseline_event, later, end)
    base_open_ids = {o.plan.id for o in control if o.state == "CLOSED"}
    base_recoveries = [o for o in control if o.plan.id in recovery and o.state == "CLOSED"]
    reports = {}
    for family in ("entry_axes", "early_exit_axes", "recovery_axes", "sizing_axes", "management_axes"):
        for name, hypothesis in config[family]:
            selected, frozen, priority = plans, frozen_fast, ()
            if family == "entry_axes":
                selected = filtered_plans(name, plans, details, bars, features, costs)
            elif family in ("early_exit_axes", "management_axes"):
                frozen = {p.id: policy_simulate(p, bars, features, costs, 5, name) for p in plans}
            elif name == "CAP_20":
                selected = [capped_plan(p, costs) for p in plans]
                frozen = {p.id: simulate(p, bars, costs, 5) for p in selected}
            elif name in ("RECOVERY_ONLY", "NO_RECOVERY"):
                selected = [p for p in plans if (p.id in recovery) == (name == "RECOVERY_ONLY")]
            elif name == "RECOVERY_PRIORITY":
                priority = recovery
            trial, rejected = book(selected, costs, equity, outcomes=frozen, recovery_first=priority)
            shared, sblocked = book(s5_plans + selected, costs, equity,
                                   outcomes={**frozen, **frozen_s5}, recovery_first=priority)
            trials, shared_trial = _score(trial, bars, costs), _score(shared, bars, costs)
            events, off_event, event_meta = audit(selected, trial, rejected)
            se, _, _ = audit(s5_plans + selected, shared, sblocked, shared=True)
            metrics = _periods(trials, start, split, end, embargo)
            later_events = _event_counts(events, later, end)
            spent = {c.id for c in shared_trial if c.model == "S5"}
            omitted = [c for cid, c in s5_closed.items() if cid not in spent]
            retained_ids = {p.id for p in selected}
            excluded = [c for c in base_scores if c.id not in retained_ids]
            excluded_by_gate = {"offers": len(plans) - len(retained_ids),
                                "reference_wins": sum(c.net_cash > 0 for c in excluded),
                                "reference_losses": sum(c.net_cash < 0 for c in excluded),
                                "reference_winner_cash_usd": sum(c.net_cash for c in excluded if c.net_cash > 0),
                                "reference_loss_magnitude_usd": -sum(c.net_cash for c in excluded if c.net_cash < 0)}
            reports[name] = {"family": family, "hypothesis": hypothesis, "status": "EXPERIMENTAL",
                             "periods": metrics, "events": _event_counts(events, start, end),
                             "later_events": later_events, "event_scope": event_meta,
                             "off_event_losing_fills": sum(o["losses"] for o in off_event),
                             "tradeoffs": _lost_and_saved(base_scores, trials), "excluded_by_gate": excluded_by_gate,
                             "blocked_reasons": dict(Counter(reason for _, reason in rejected)),
                             "open_or_pending_at_cutoff": sum(o.state.startswith("CENSORED") for o in trial),
                             "campaigns_using_recovery": sum(c.id in recovery for c in trials),
                             "new_closed_campaigns": sum(c.id not in base_open_ids for c in trials),
                             "historical_gate_failures": _decision(baseline_metrics["later_archive"],
                                                                  metrics["later_archive"], baseline_later_events,
                                                                  later_events, config["primary"]),
                             "paired_inference": _inference(base_scores, trials, later, end, config["primary"]),
                             "shared_s5": {"periods": _periods(shared_trial, start, split, end, embargo),
                                           "events": _event_counts(se, start, end), "closed_s5_excluded": len(omitted),
                                           "original_s5_net_usd_excluded": sum(c.net_cash for c in omitted),
                                           "blocked_s5_reasons": dict(Counter(reason for p, reason in sblocked
                                                                             if p.model == "S5"))}}
    adjusted = holm([v["paired_inference"]["raw_one_sided_p"] for v in reports.values()])
    for result, p in zip(reports.values(), adjusted):
        result["paired_inference"]["holm_all_registered_arms_p"] = p
        if p >= .05:
            result["historical_gate_failures"].append("HOLM_ADJUSTED_P")
    base_recovery_by_kind = {}
    for kind in sorted({anchor["prior_kind"] for anchor in recovery.values()}):
        outcomes = [o for o in base_recoveries if recovery[o.plan.id]["prior_kind"] == kind]
        cash = _score(outcomes, bars, costs)
        base_recovery_by_kind[kind] = {
            "candidate_offers": sum(a["prior_kind"] == kind for a in recovery.values()),
            "campaigns": _summarize(cash, start, end),
        }
    return {"schema": "qirvanta.gold-precision.archive-research.v1",
            "status": "EXPERIMENTAL", "decision": "RETAIN_IMMUTABLE_CONTROLS",
            "market_sha256": market_hash,
            "baseline_parity": {"S5": "EXACT_TAPE", "FAST": "EXACT_TAPE", "S5_PLUS_FAST": "EXACT_TAPE"},
            "windows_utc_ms": {"start": start, "split": split, "later_start": later, "end_exclusive": end},
            "holdout": "NO_UNSEEN_RESULTS",
            "controls": {"FAST": baseline_metrics, "S5": _periods(s5_scores, start, split, end, embargo),
                         "S5_PLUS_FAST": _periods(shared_scores, start, split, end, embargo)},
            "baseline_events": {"full": _event_counts(baseline_event, start, end),
                                "later_archive": baseline_later_events, **baseline_event_meta},
            "recovery": {"offers": len(recovery), "baseline_campaigns": len(base_recoveries),
                         "original_second_chance_offers": sum(d["second_chance"] for d in details.values()),
                         "original_second_chance_closed": sum(details[o.plan.id]["second_chance"] for o in control
                                                             if o.state == "CLOSED"),
                         "by_kind": base_recovery_by_kind,
                         "lineage": recovery},
            "candidates": reports}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("../mission"))
    parser.add_argument("--logs-dir", type=Path, default=Path("../tv-final"))
    parser.add_argument("--tape", type=Path, default=Path("../../outputs/gold_pro/archive_release_tape.json.gz"))
    parser.add_argument("--diagnostic", type=Path, default=STAGE_ONE)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    config = _read_json(PROTOCOL)
    diagnostic = _read_json(args.diagnostic)
    if diagnostic["protocol"]["source_tape_sha256"] != config["baseline_tape_sha256"]:
        raise ValueError("Stage 1 diagnostic and research tape must have the same pin")
    if (diagnostic["status"] != "VERIFIED_ARCHIVED_RECONSTRUCTION" or
            diagnostic["states"]["CLOSED"] != config["expected_fast_campaigns"]):
        raise ValueError("Complete Stage 1 before evaluating candidate outcomes")
    raw = json.loads(gzip.decompress(_read(args.tape, config["baseline_tape_sha256"]).read_bytes()))
    report = run(config, archived_inputs(args.data_root, args.logs_dir), raw["5"])
    report["source"] = {"stage_1_sha256": _hash(args.diagnostic), "archive_tape_sha256": _hash(args.tape),
                        "registry_sha256": _hash(PROTOCOL), "execution_sha256": _hash(Path(__file__).with_name("execution.py")),
                        "runner_sha256": _hash(Path(__file__))}
    _write(args.out, report)
    print(f"{report['decision']} | {len(report['candidates'])} isolated policies")


if __name__ == "__main__":
    main()
