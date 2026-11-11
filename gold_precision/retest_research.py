"""Rebook pinned M5 RETEST and second-chance offers on the archived S5/FAST books."""

import argparse
from collections import Counter
import gzip
import json
from pathlib import Path
from statistics import mean

from gold_pro.archive import ROOT, _hash, _ms, _read, _read_json, _write
from gold_pro.evaluate import _window
from gold_pro.opportunity import event_audit
from gold_pro.replay import simulate
from research.validation.statistics import cash_metrics, holm

from .execution import book
from .experiments import DAY, PROTOCOL as PRECISION_PROTOCOL
from .experiments import _inference, _plan_details, _score, _verify_pin, archived_inputs, recovery_plans

PROTOCOL = Path(__file__).with_name("retest_protocol.json")
PRECISION_EVIDENCE = ROOT / "docs/precision_evidence/experiments.json"
FORWARD_SEAL = ROOT / "docs/gold_pro_evidence/forward_seal_m5.json"
DIRECTIONS = {"all": None, "BUY": 1, "SELL": -1}
WEEKLY_LANES = {
    "S5", "FAST_FILLED_RETEST", "FAST_FILLED_RECOVERY", "FAST_FILLED_ORIGINAL_SECOND_CHANCE",
    "RETEST_ONLY", "RECOVERY_ONLY",
}


def stressed_cash(campaign, multiplier):
    return campaign.net_cash - multiplier * (campaign.net_cash - campaign.stressed_net_cash)


def outcomes_metrics(campaigns, costs):
    ordered = sorted(campaigns, key=lambda c: (c.exit_ms, c.entry_ms, c.id))
    result = {}
    for scenario, multiplier in costs.items():
        cash = [stressed_cash(campaign, multiplier) for campaign in ordered]
        returns = [value / c.risk_cash for value, c in zip(cash, ordered)]
        metrics = cash_metrics(cash)
        winners = sorted((value for value in cash if value > 0), reverse=True)
        gross_wins = sum(winners)
        result[scenario] = {
            **{key: metrics[key] for key in ("n", "wins", "losses", "breakeven", "net_cash",
                                             "mean_cash", "profit_factor", "win_rate",
                                             "campaign_close_drawdown", "small_n")},
            "net_r_sum": sum(returns),
            "expectancy_r": mean(returns) if returns else None,
            "gross_profit_usd": gross_wins,
            "gross_loss_magnitude_usd": -sum(value for value in cash if value < 0),
            "top_three_gross_profit_share": sum(winners[:3]) / gross_wins if gross_wins else None,
        }
    return result


def direction_periods(campaigns, windows, costs):
    return {
        label: {
            name: outcomes_metrics([c for c in _window(campaigns, *bounds) if side is None or c.side == side], costs)
            for name, side in DIRECTIONS.items()
        }
        for label, bounds in windows.items()
    }


def blocked_ledger(rejected, shadow_cash, bars, costs):
    rows = []
    counts = Counter()
    for plan, reason in rejected:
        campaign = shadow_cash.get(plan.id)
        if campaign is None:
            continue
        values = {key: stressed_cash(campaign, m) for key, m in costs.items()}
        counts.update(key for key, value in values.items() if value > 0)
        if any(value > 0 for value in values.values()):
            rows.append({
                "plan_id": plan.id, "model": plan.model, "direction": "BUY" if plan.side == 1 else "SELL",
                "decision_utc_ms": bars[plan.bar]["time_close"], "hypothetical_entry_utc_ms": campaign.entry_ms,
                "hypothetical_exit_utc_ms": campaign.exit_ms, "portfolio_rejection": reason,
                "isolated_net_usd": values,
                "isolated_net_r": {key: value / campaign.risk_cash for key, value in values.items()},
            })
    return {
        "all_rejected": len(rejected),
        "by_reason": dict(Counter(reason for _, reason in rejected)),
        "profitable_isolated_by_cost": {key: counts[key] for key in costs},
        "profitable_isolated_offers": rows,
    }


def contrast(candidate, baseline, metrics):
    """Show the new book's full result minus the comparison book on identical windows."""
    return {
        "candidate": candidate, "baseline": baseline,
        "periods": {
            window: {
                direction: {
                    scenario: {
                        "delta_usd": row["net_cash"] - metrics[baseline][window][direction][scenario]["net_cash"],
                        "delta_total_net_r": row["net_r_sum"] - metrics[baseline][window][direction][scenario]["net_r_sum"],
                        "delta_closed_campaigns": row["n"] - metrics[baseline][window][direction][scenario]["n"],
                    }
                    for scenario, row in cases.items()
                }
                for direction, cases in by_side.items()
            }
            for window, by_side in metrics[candidate].items()
        },
    }


def event_summary(bars, event, decisions, selected, outcomes, rejected):
    rows, _, _ = event_audit(bars, 5, event, decisions, selected, outcomes, rejected)
    return {
        name: {
            "opportunities": len(subset := [e for e in rows if side is None or e["side"] == side]),
            "covered": sum(e["filled"] for e in subset),
        }
        for name, side in DIRECTIONS.items()
    }


def run(config, inputs, archive, precision):
    bars, _, costs, equity, decisions, plans, native, market_hash, event = inputs
    start, end = bars[0]["time"], bars[-1]["time_close"] + 1
    split = bars[int(len(bars) * config["split"]["bar_fraction"])]["time"]
    later = split + config["split"]["embargo_minutes"] * 60_000
    windows = {"full": (start, end), "development": (start, split), "later_archive": (later, end)}
    weekly_start = _ms(config["split"]["weekly_start_utc"])
    week = 7 * DAY
    weeks = {f"week_{i+1}": (weekly_start + i * week, weekly_start + (i+1) * week)
             for i in range(config["split"]["weekly_buckets"])}
    if any(a < start or b > end for a, b in weeks.values()):
        raise ValueError("Registered weeks outside archive bounds")

    s5_plans = [o.plan for o in native]
    frozen_fast = {p.id: simulate(p, bars, costs, 5) for p in plans}
    frozen = {**frozen_fast, **{o.plan.id: o for o in native}}
    control, blocked = book(plans, costs, equity, outcomes=frozen)
    shared, shared_blocked = book(s5_plans + plans, costs, equity, outcomes=frozen)
    _verify_pin(archive, native, [], "S5")
    _verify_pin(archive, control, blocked, "FAST")
    _verify_pin(archive, shared, shared_blocked, "S5_PLUS_FAST")
    recovery = recovery_plans(plans, decisions, control, blocked, bars, costs.tick)
    retest = [p for p in plans if p.model == "RETEST"]
    without_retest = [p for p in plans if p.model != "RETEST"]
    recovered = [p for p in plans if p.id in recovery]
    without_recovery = [p for p in plans if p.id not in recovery]
    subsets = {
        "RETEST_ONLY": retest, "FAST_NO_RETEST": without_retest,
        "RECOVERY_ONLY": recovered, "FAST_NO_RECOVERY": without_recovery,
    }
    runs = {"S5": (s5_plans, native, []), "FAST": (plans, control, blocked),
            "S5_PLUS_FAST": (s5_plans + plans, shared, shared_blocked)}
    for name, selected in subsets.items():
        runs[name] = (selected, *book(selected, costs, equity, outcomes=frozen))
        combined = s5_plans + selected
        runs["S5_PLUS_" + name] = (combined, *book(combined, costs, equity, outcomes=frozen))
    if set(runs) != set(config["controls"] + config["replayed_lanes"]):
        raise ValueError("Unregistered occupancy replay")

    cash = {name: _score(outcomes, bars, costs) for name, (_, outcomes, _) in runs.items()}
    original_second = {row["plan_id"] for row in _plan_details(decisions).values() if row["second_chance"]}
    cohorts = {
        "FAST_FILLED_RETEST": [c for c in cash["FAST"] if c.model == "RETEST"],
        "FAST_FILLED_RECOVERY": [c for c in cash["FAST"] if c.id in recovery],
        "FAST_FILLED_ORIGINAL_SECOND_CHANCE": [c for c in cash["FAST"] if c.id in original_second],
    }
    if set(cohorts) != set(config["observed_attribution"]):
        raise ValueError("Unregistered observational attribution")
    if len(cash["FAST"]) != 123 or len(cohorts["FAST_FILLED_RETEST"]) != 26:
        raise ValueError("Archived FAST or RETEST campaign count differs")
    previous = precision["candidates"]["RECOVERY_ONLY"]["periods"]["full"]
    actual = cash["RECOVERY_ONLY"]
    if len(actual) != previous["n"] or abs(sum(c.net_cash for c in actual) - previous["net_cash"]) > 1e-8:
        raise ValueError("Original RECOVERY_ONLY closed aggregate differs")

    costs_cases = config["cost_cases"]
    all_cash = {**cash, **cohorts}
    metrics = {name: direction_periods(lane, windows, costs_cases) for name, lane in all_cash.items()}
    weekly = {name: direction_periods(all_cash[name], weeks, costs_cases) for name in sorted(WEEKLY_LANES)}
    shadow_cash = {c.id: c for c in _score(frozen.values(), bars, costs)}
    s5_decisions = [{"time_ms": bars[p.bar]["time_close"], "side": p.side, "state": "TREND_CONFIRMED",
                     "reason": "ARCHIVED_S5_ADMISSION", "model": "S5"} for p in s5_plans]
    occupancy = {}
    for name, (selected, outcomes, rejected) in runs.items():
        occupied_s5_ids = {c.id for c in cash[name] if c.model == "S5"}
        displaced_s5 = [c for c in cash["S5"] if c.id not in occupied_s5_ids] if name.startswith("S5_PLUS") else []
        occupancy[name] = {
            "submitted_offers": len(selected), "closed_campaigns": len(cash[name]),
            "censored_at_cutoff": sum(o.state.startswith("CENSORED") for o in outcomes),
            "event_coverage": event_summary(bars, event, decisions + s5_decisions, selected, outcomes, rejected),
            "rejected": blocked_ledger(rejected, shadow_cash, bars, costs_cases),
            "displaced_s5_closed": len(displaced_s5),
            "displaced_s5_original_net_usd": sum(c.net_cash for c in displaced_s5),
            "displaced_s5_profitable_ids": [c.id for c in displaced_s5 if c.net_cash > 0],
        }

    spec = config["primary_inference"]
    comparisons = {name: contrast(*pair, metrics) for name, pair in spec["contrasts"].items()}
    inference = [_inference(cash[base], cash[trial], later, end, spec)
                 for trial, base in spec["contrasts"].values()]
    p_adjusted = holm([row["raw_one_sided_p"] for row in inference])
    for comparison, evidence, adjusted in zip(comparisons.values(), inference, p_adjusted):
        comparison["later_paired_stress_r"] = {**evidence, "holm_p": adjusted}
    for label, subset in (("RETEST", "RETEST"), ("RECOVERY", "RECOVERY")):
        comparisons[f"{label}_WITHIN_S5_PLUS_FAST"] = contrast("S5_PLUS_FAST", f"S5_PLUS_FAST_NO_{subset}", metrics)

    recovered_fast = cohorts["FAST_FILLED_RECOVERY"]
    by_anchor = {kind: outcomes_metrics([c for c in recovered_fast if recovery[c.id]["prior_kind"] == kind], costs_cases)
                 for kind in sorted({anchor["prior_kind"] for anchor in recovery.values()})}
    return {
        "schema": config["schema"], "status": "EXPERIMENTAL_ARCHIVE", "decision": "NO_PROMOTION",
        "market_sha256": market_hash, "pinned_controls": {"FAST": "EXACT_TAPE", "S5": "EXACT_TAPE",
                                                          "S5_PLUS_FAST": "EXACT_TAPE"},
        "original_recovery_closed_aggregate": "EXACT",
        "cost_cases": costs_cases,
        "windows_utc_ms": {**windows, **weeks}, "cohort_overlap": {
            "fast_retest_and_recovery": sum(c.id in recovery for c in cohorts["FAST_FILLED_RETEST"]),
            "fast_retest_and_original_second_chance": sum(c.id in original_second for c in cohorts["FAST_FILLED_RETEST"]),
        },
        "chronological_directional_results": metrics, "weekly_directional_results": weekly,
        "occupancy_replays": occupancy, "marginal_contributions": comparisons,
        "fast_closed_recovery_by_anchor": by_anchor,
        "recovery_offer_lineage": recovery,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=Path("../mission"))
    parser.add_argument("--logs-dir", type=Path, default=Path("../tv-final"))
    parser.add_argument("--tape", type=Path, default=Path("../../outputs/gold_pro/archive_release_tape.json.gz"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    config = _read_json(PROTOCOL)
    _read(PRECISION_PROTOCOL, config["precision_protocol_sha256"])
    precision = _read_json(_read(PRECISION_EVIDENCE, config["precision_evidence_sha256"]))
    _read(FORWARD_SEAL, config["forward_seal_sha256"])
    raw = json.loads(gzip.decompress(_read(args.tape, config["archive_sha256"]).read_bytes()))
    report = run(config, archived_inputs(args.data_root, args.logs_dir), raw["5"], precision)
    report["source"] = {**{name: config[name] for name in ("source_commit", "archive_sha256", "forward_seal_sha256",
                                                          "precision_evidence_sha256", "precision_protocol_sha256")},
                        "retest_protocol_sha256": _hash(PROTOCOL), "runner_sha256": _hash(Path(__file__))}
    _write(args.out, report)
    print(f"{report['decision']} | {len(report['occupancy_replays'])} archive books")


if __name__ == "__main__":
    main()
