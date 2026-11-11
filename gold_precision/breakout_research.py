"""Investigate the pinned FAST BREAKOUT archive with independent entry hypotheses."""

import argparse
from collections import Counter
import gzip
import json
from pathlib import Path
from statistics import median

from gold_pro.archive import REGISTRATION, ROOT, _hash, _read, _read_json, _write
from gold_pro.opportunity import event_audit
from gold_pro.replay import simulate
from research.validation.statistics import holm

from .diagnose import PROTOCOL as DIAGNOSTIC_PROTOCOL
from .diagnose import _anchors, utc
from .execution import book, friction_price
from .experiments import _event_counts, _inference, _periods, _plan_details, _score, _verify_pin, archived_inputs

PROTOCOL = Path(__file__).with_name("breakout_protocol.json")


def recent_reclaim(signal_bar, side, anchor, atr, attempts, bars, settings):
    """Use only old confirmed attempts and bars closed before this signal."""
    first = max(0, signal_bar - settings["lookback_bars"])
    for j in range(first, signal_bar - 1):
        previous = attempts.get((j, side))
        if previous is None:
            continue
        old_anchor, old_atr = previous
        if abs(anchor - old_anchor) > settings["anchor_proximity_atr"] * atr:
            continue
        for k in range(j + 1, signal_bar):
            if side * (bars[k]["close"] - old_anchor) <= -settings["reclaim_atr"] * old_atr:
                return j, k
    return None


def breakout_quality(plans, bars, features, decisions, costs, settings, arms, warmup):
    details = _plan_details(decisions)
    anchors = _anchors(bars, features, warmup)
    attempts = {
        (row["bar"], row["side"]): (anchors[row["bar"], row["side"]]["BREAKOUT"], features[row["bar"]].atr)
        for row in decisions if row["state"] == "TREND_CONFIRMED" and row["model"] == "BREAKOUT"
    }
    reclaim = next(arm for arm in arms if arm["id"] == "NO_NEARBY_FAILED_BREAK")
    quality = {}
    for plan in plans:
        if plan.model != "BREAKOUT":
            continue
        i, side = plan.bar, plan.side
        row, info = bars[i], details[plan.id]
        anchor = anchors[i, side]["BREAKOUT"]
        prior_close = bars[i - 1]["close"]
        true_range = max(row["high"] - row["low"], abs(row["high"] - prior_close), abs(row["low"] - prior_close))
        prior_fail = recent_reclaim(i, side, anchor, plan.atr, attempts, bars, reclaim)
        first = max(0, i + 1 - settings["opposite_bos_lookback_including_signal"])
        opposite_bos = next((j for j in range(i, first - 1, -1) if features[j].bos == -side), None)
        risk = side * (plan.reference - plan.stop)
        if abs(risk - info["stop_distance"]) > 1e-8:
            raise ValueError(f"Price geometry differs for {plan.id}")
        quality[plan.id] = {
            "anchor_kind": "CONFIRMED_SWING_BOS" if features[i].bos == side else "PREVIOUS_THREE_BAR_EDGE",
            "directional_displacement": features[i].displacement == side,
            "signal_clearance_atr": side * (plan.reference - anchor) / plan.atr,
            "extension_atr": info["extension_atr"],
            "true_range_atr": true_range / plan.atr,
            "signal_age_bars": info["signal_age_bars"],
            "stop_atr": risk / plan.atr,
            "friction_fraction": friction_price(costs) / risk,
            "net_room_r": info["net_room_r"],
            "explicit_barrier": info["known_barrier"] is not None,
            "opposite_bos_close_utc": utc(bars[opposite_bos]["time_close"]) if opposite_bos is not None else None,
            "nearby_prior_failed_break": {
                "prior_break_close_utc": utc(bars[prior_fail[0]]["time_close"]),
                "reclaim_close_utc": utc(bars[prior_fail[1]]["time_close"]),
            } if prior_fail is not None else None,
        }
    return quality


def keeps(arm_id, row, limits):
    if arm_id == "ANCHOR_ACCEPTANCE_020":
        return row["signal_clearance_atr"] >= limits["weak_anchor_clearance_atr"]
    if arm_id == "NO_NEARBY_FAILED_BREAK":
        return row["nearby_prior_failed_break"] is None
    if arm_id == "VOLATILITY_175":
        return row["true_range_atr"] <= limits["true_range_atr"]
    if arm_id == "NO_OPPOSITE_BOS_4":
        return row["opposite_bos_close_utc"] is None
    raise ValueError(f"Unknown preregistered arm {arm_id}")


def risk_sum(campaigns, *, stressed=False):
    return sum((c.stressed_net_cash if stressed else c.net_cash) / c.risk_cash for c in campaigns)


def fixed_gate_effect(reference, removed_ids):
    removed = [c for c in reference if c.id in removed_ids]
    winners = [c for c in removed if c.net_cash > 0]
    losses = [c for c in removed if c.net_cash < 0]
    return {
        "winners_excluded": len(winners),
        "losers_excluded": len(losses),
        "flat_excluded": len(removed) - len(winners) - len(losses),
        "winning_r_forgone": risk_sum(winners),
        "losing_r_avoided": -risk_sum(losses),
        "net_r_delta": -risk_sum(removed),
        "stress_r_delta": -risk_sum(removed, stressed=True),
        "excluded_campaign_ids": [c.id for c in removed],
    }


def occupancy_delta(control, trial):
    base, revised = ({c.id: c for c in values} for values in (control, trial))
    removed = [c for cid, c in base.items() if cid not in revised]
    added = [c for cid, c in revised.items() if cid not in base]
    if any(c != revised[cid] for cid, c in base.items() if cid in revised):
        raise ValueError("A fixed-outcome arm changed a retained campaign")
    return {
        "absent_closed_ids": [c.id for c in removed],
        "new_closed_ids": [c.id for c in added],
        "absent_winners": sum(c.net_cash > 0 for c in removed),
        "absent_losses": sum(c.net_cash < 0 for c in removed),
        "new_winners": sum(c.net_cash > 0 for c in added),
        "new_losses": sum(c.net_cash < 0 for c in added),
        "absent_net_r": risk_sum(removed),
        "new_net_r": risk_sum(added),
        "net_r_delta": risk_sum(added) - risk_sum(removed),
        "stress_r_delta": risk_sum(added, stressed=True) - risk_sum(removed, stressed=True),
        "absent_s5_campaigns": sum(c.model == "S5" for c in removed),
        "new_s5_campaigns": sum(c.model == "S5" for c in added),
    }


def descriptive(rows, limits):
    def summarize(sample):
        fields = ("signal_clearance_atr", "extension_atr", "true_range_atr", "signal_age_bars",
                  "stop_atr", "friction_fraction", "net_room_r", "anchor_distance_at_fill_atr",
                  "fill_deviation_atr")
        flags = {
            "weak_anchor_clearance": lambda r: r["signal_clearance_atr"] < limits["weak_anchor_clearance_atr"],
            "no_directional_displacement": lambda r: not r["directional_displacement"],
            "post_fill_reclaim_before_stop_or_final_exit": lambda r: r["reclaim_after_fill_utc"] is not None,
            "previous_nearby_reclaim": lambda r: r["nearby_prior_failed_break"] is not None,
            "extended_signal": lambda r: r["extension_atr"] >= limits["extension_atr"],
            "signal_volatility_expansion": lambda r: r["true_range_atr"] > limits["true_range_atr"],
            "stale_watch": lambda r: r["signal_age_bars"] >= limits["signal_age_bars"],
            "adverse_fill_deviation": lambda r: r["fill_deviation_atr"] >= limits["fill_deviation_atr"],
            "wide_stop": lambda r: r["stop_atr"] >= limits["wide_stop_atr"],
            "high_friction": lambda r: r["friction_fraction"] >= limits["friction_to_price_risk"],
            "thin_net_room": lambda r: r["net_room_r"] < limits["net_room_r"],
            "explicit_barrier": lambda r: r["explicit_barrier"],
            "opposing_bos_known": lambda r: r["opposite_bos_close_utc"] is not None,
            "opposing_confirm_known": lambda r: r["opposing_signal_before_entry"] is not None,
        }
        return {
            "n": len(sample),
            "counts": {key: sum(test(row) for row in sample) for key, test in flags.items()},
            "medians": {key: median([r[key] for r in sample]) if sample else None for key in fields},
            "net_usd": sum(r["net_usd"] for r in sample),
            "net_r": sum(r["net_r"] for r in sample),
        }

    return {
        "winners": summarize([r for r in rows if r["net_usd"] > 0]),
        "losses": summarize([r for r in rows if r["net_usd"] < 0]),
        "slices": {f"{side}_{kind}": summarize([r for r in rows if (r["side"], r["anchor_kind"]) == (side, kind)])
                   for side in ("BUY", "SELL") for kind in ("CONFIRMED_SWING_BOS", "PREVIOUS_THREE_BAR_EDGE")},
    }


def run(protocol, inputs, archive, diagnostic, feature_settings, study):
    bars, features, costs, equity, decisions, plans, native, market_hash, event = inputs
    minutes = protocol["minutes"]
    limits = protocol["diagnostic_thresholds"]
    start, end = bars[0]["time"], bars[-1]["time_close"] + 1
    split = bars[int(len(bars) * study["split"]["bar_fraction"])]["time"]
    embargo = study["split"]["embargo_minutes"] * 60_000
    later = split + embargo
    frozen_fast = {p.id: simulate(p, bars, costs, minutes) for p in plans}
    frozen_shared = {**frozen_fast, **{o.plan.id: o for o in native}}
    s5_plans = [o.plan for o in native]
    original, blocked = book(plans, costs, equity, outcomes=frozen_fast)
    shared_original, shared_blocked = book(s5_plans + plans, costs, equity, outcomes=frozen_shared)
    _verify_pin(archive, native, [], "S5")
    _verify_pin(archive, original, blocked, "FAST")
    _verify_pin(archive, shared_original, shared_blocked, "S5_PLUS_FAST")
    baseline, baseline_shared = _score(original, bars, costs), _score(shared_original, bars, costs)
    baseline_breakout = [c for c in baseline if c.model == "BREAKOUT"]
    actual = {"campaigns": len(baseline_breakout), "winners": sum(c.net_cash > 0 for c in baseline_breakout),
              "losers": sum(c.net_cash < 0 for c in baseline_breakout)}
    if actual != protocol["expected_baseline_breakout"]:
        raise ValueError("Baseline does not match preregistered BREAKOUT population")
    quality = breakout_quality(plans, bars, features, decisions, costs, limits, protocol["arms"], feature_settings["warmup_bars"])
    verified_rows = {r["campaign_id"]: r for r in diagnostic["campaigns"] if r["model"] == "BREAKOUT" and r["state"] == "CLOSED"}
    if set(verified_rows) != {c.id for c in baseline_breakout}:
        raise ValueError("Pinned diagnostic and replayed cohort differ")
    rows = []
    for campaign in baseline_breakout:
        row = verified_rows[campaign.id]
        if abs(row["net_r"] - campaign.net_cash / campaign.risk_cash) > 1e-8:
            raise ValueError("Pinned diagnostic campaign economics differ")
        selected = ("campaign_id", "side", "confirmed_signal_utc", "fill_open_utc", "net_usd", "net_r",
                    "stress_net_usd", "exit_reasons", "mfe_lower_bound_r", "mae_lower_bound_r",
                    "opposing_signal_before_entry", "anchor_distance_at_fill_atr")
        rows.append({key: row[key] for key in selected} | quality[campaign.id] | {
            "fill_deviation_atr": row["anchor_distance_at_fill_atr"] - row["anchor_distance_at_signal_atr"],
            "reclaim_after_fill_utc": row["invalidation"]["anchor_failure_close_utc"],
        })

    def event_counts(selection, outcomes, rejects, a, b, shared=False):
        source = decisions
        if shared:
            source = decisions + [{"time_ms": bars[p.bar]["time_close"], "side": p.side, "state": "TREND_CONFIRMED",
                                    "reason": "ARCHIVED_S5_ADMISSION", "model": "S5"} for p in s5_plans]
        events, _, _ = event_audit(bars, minutes, event, source, selection, outcomes, rejects)
        return _event_counts(events, a, b)

    baseline_full = _periods(baseline, start, split, end, embargo)
    baseline_events_later = event_counts(plans, original, blocked, later, end)
    studies = {}
    for arm in protocol["arms"]:
        arm_id = arm["id"]
        removed = {cid for cid, row in quality.items() if not keeps(arm_id, row, limits)}
        retained = [p for p in plans if p.id not in removed]
        trial, rejection = book(retained, costs, equity, outcomes=frozen_fast)
        combined, combined_rejection = book(s5_plans + retained, costs, equity, outcomes=frozen_shared)
        score, combined_score = _score(trial, bars, costs), _score(combined, bars, costs)
        periods = _periods(score, start, split, end, embargo)
        later_events = event_counts(retained, trial, rejection, later, end)
        inference = _inference(baseline, score, later, end, protocol["chronology"])
        studies[arm_id] = {
            "status": "EXPERIMENTAL",
            "removed_risk_eligible_breakout_offers": len(removed),
            "retained_risk_eligible_breakout_offers": len(quality) - len(removed),
            "fixed_breakout_campaigns": fixed_gate_effect(baseline_breakout, removed),
            "all_fast_periods": periods,
            "all_fast_occupancy_delta": occupancy_delta(baseline, score),
            "all_fast_blocks": dict(Counter(reason for _, reason in rejection)),
            "all_fast_event_full": event_counts(retained, trial, rejection, start, end),
            "all_fast_event_later": later_events,
            "s5_plus_fast_periods": _periods(combined_score, start, split, end, embargo),
            "s5_plus_fast_occupancy_delta": occupancy_delta(baseline_shared, combined_score),
            "s5_plus_fast_blocks": dict(Counter(reason for _, reason in combined_rejection)),
            "s5_plus_fast_event_full": event_counts(s5_plans + retained, combined, combined_rejection, start, end, True),
            "inference": inference,
        }
        metrics = periods["later_archive"]
        failures = []
        if inference["stress_r_delta"] <= 0:
            failures.append("LATER_STRESS_R")
        if any(metrics[key] > baseline_full["later_archive"][key] + 1e-8
               for key in ("campaign_close_drawdown", "stressed_drawdown_cash")):
            failures.append("LATER_DRAWDOWN")
        if later_events["filled"] < baseline_events_later["filled"]:
            failures.append("LATER_EVENT_FILLS")
        studies[arm_id]["continued_review_failures"] = failures

    adjusted = holm([result["inference"]["raw_one_sided_p"] for result in studies.values()])
    for result, p in zip(studies.values(), adjusted):
        result["inference"]["holm_all_arms_p"] = p
        if p >= .05:
            result["continued_review_failures"].append("HOLM_ADJUSTED_P")

    return {
        "schema": protocol["schema"],
        "status": "EXPERIMENTAL",
        "promotion": "NO_UNSEEN_RESULTS",
        "verified_original_tapes": ["S5", "FAST", "S5_PLUS_FAST"],
        "market_sha256": market_hash,
        "periods_utc": {"start": utc(start), "split": utc(split), "later_start": utc(later), "last_close": utc(end - 1)},
        "baseline": {
            "fixed_breakout_counts": actual,
            "risk_eligible_breakout_offers": len(quality),
            "breakout_periods": _periods(baseline_breakout, start, split, end, embargo),
            "all_fast_periods": baseline_full,
            "all_fast_event_full": event_counts(plans, original, blocked, start, end),
            "all_fast_event_later": baseline_events_later,
            "s5_plus_fast_periods": _periods(baseline_shared, start, split, end, embargo),
            "s5_plus_fast_event_full": event_counts(s5_plans + plans, shared_original, shared_blocked, start, end, True),
        },
        "diagnostic": descriptive(rows, limits),
        "baseline_campaigns": rows,
        "candidates": studies,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--logs-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--tape", type=Path, required=True)
    parser.add_argument("--diagnostic", type=Path, default=ROOT / "docs/precision_evidence/fast_diagnostic.json")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists():
        parser.error("Choose a new output path")
    protocol = _read_json(PROTOCOL)
    report = _read_json(_read(args.report, protocol["report_sha256"]))
    if report["private_tape_sha256"] != protocol["tape_sha256"]:
        raise ValueError("Baseline report and tape pins differ")
    for path, digest in report["research_sources"].items():
        _read(ROOT / path, digest)
    config = _read_json(_read(REGISTRATION, report["registration_sha256"]))
    diag_protocol = _read_json(DIAGNOSTIC_PROTOCOL)
    feature_code = ROOT / "unified/tools/evaluate_v22_signals.py"
    event_code = ROOT / "unified/tools/evaluate_v21_impulse.py"
    feature_config = ROOT / "docs/v22_evidence/historical_registration.json"
    _read(feature_code, diag_protocol["feature_code_sha256"])
    _read(event_code, diag_protocol["event_code_sha256"])
    _read(feature_config, diag_protocol["feature_config_sha256"])
    _read(ROOT / config["opportunity"]["event_reference"], diag_protocol["event_config_sha256"])
    archive = json.loads(gzip.decompress(_read(args.tape, protocol["tape_sha256"]).read_bytes()))["5"]
    diagnostic = _read_json(_read(args.diagnostic, protocol["diagnostic_sha256"]))
    study_path = PROTOCOL.with_name("experiment_protocol.json")
    inputs = archived_inputs(args.data_root, args.logs_dir)
    result = run(protocol, inputs, archive, diagnostic, config["opportunity"], _read_json(study_path))
    result["source"] = {
        "protocol_sha256": _hash(PROTOCOL), "report_sha256": protocol["report_sha256"],
        "tape_sha256": protocol["tape_sha256"], "diagnostic_sha256": protocol["diagnostic_sha256"],
        "chronology_protocol_sha256": _hash(study_path),
        "local_code_sha256": {name: _hash(PROTOCOL.with_name(name))
                              for name in ("breakout_research.py", "diagnose.py", "execution.py", "experiments.py")},
    }
    _write(args.out, result)
    print(f"BREAKOUT: {result['baseline']['fixed_breakout_counts']['campaigns']} campaigns and {len(result['candidates'])} arms")


if __name__ == "__main__":
    main()
