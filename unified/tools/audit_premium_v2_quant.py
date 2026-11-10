"""Fixed-fill Premium V2 research on the original observed S5 campaigns."""
import argparse
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal
import json
from pathlib import Path
from statistics import mean
from zoneinfo import ZoneInfo

from diagnostics.accounting.audit import closed_drawdown, json_default, stats
from research.data.loader import load_bars
from research.features.causal import build_features
from research.validation.native_report import utc
from research.validation.statistics import block_bootstrap, holm, quantile, sign_flip_p
from unified.tools.audit_premium_s5 import atr_average, signal_features, values, verify_bars
from unified.tools.audit_tv_observations import inspect
from unified.tools.build_tradingview_final import ROOT, sha


REGISTRATION = ROOT / "docs/premium_v2_evidence/quant_registration.json"
ZERO = Decimal(0)


def read_pinned(path, expected):
    raw = path.read_bytes()
    if sha(raw) != expected:
        raise ValueError(f"Pinned input SHA differs: {path.name}")
    return raw


def describe(values):
    return {"n": len(values), "median": quantile(values, .5), "p95": quantile(values, .95),
            "max": max(values, default=None)}


def structural_context(bars):
    highs, lows, ema = [], [], []
    for i, bar in enumerate(bars):
        ema.append(bar["close"] if i == 0 else ema[-1] + 2 / 21 * (bar["close"] - ema[-1]))
        if i < 4:
            continue
        center = bars[i-2]
        neighbors = (bars[i-4], bars[i-3], bars[i-1], bar)
        if all(center["high"] > neighbor["high"] for neighbor in neighbors):
            highs.append((i, center["high"]))
        if all(center["low"] < neighbor["low"] for neighbor in neighbors):
            lows.append((i, center["low"]))
    return highs, lows, ema


def signal_screen(row, bars, indices, context, features):
    side, meta = row["side"], row["submitted_metadata"]
    i = meta["submitted_bar"]
    seed_index = indices[int(row["plan_id"].split("|")[-2])]
    seed, bar = bars[seed_index], bars[i]
    atr, stop = float(meta["atr"]), float(meta["initial_sl"])
    highs, lows, ema = context
    ahead, behind = (highs, lows) if side == 1 else (lows, highs)
    prior_count = bisect_left(ahead, (i,))
    level = ahead[prior_count-1][1] if prior_count else None
    swing_break = None if level is None else (
        (bar["close"] - level) * side > 0 and (bars[i-1]["close"] - level) * side <= 0)
    previous = bisect_left(behind, (seed_index,))
    pair = [price for _, price in behind[max(0, previous-2):previous]]
    equal_sweep = None
    if len(pair) == 2:
        seed_extreme = seed["low"] if side == 1 else seed["high"]
        equal_sweep = (abs(pair[0] - pair[1]) <= .10 * atr
                       and all((seed_extreme - level) * side < 0 < (bar["close"] - level) * side
                               for level in pair))
    adx = features[i]["adx"]
    return {
        "VQ01": swing_break, "VQ02": equal_sweep,
        "VQ03": None if adx is None else adx <= 25,
        "VQ04": (ema[i] - ema[i-3]) * side >= .10 * atr,
        "VQ05": (float(meta["reference"]) - stop) * side <= 2.0 * atr,
    }


def financing_events(first, last):
    ny = ZoneInfo("America/New_York")
    day = utc(first).astimezone(ny).date()
    final_day = utc(last).astimezone(ny).date()
    events = []
    while day <= final_day:
        if day.weekday() < 5:
            cutoff = datetime.combine(day, time(17), tzinfo=ny)
            events.append((int(cutoff.timestamp() * 1000), 3 if day.weekday() == 2 else 1))
        day += timedelta(days=1)
    return events


def cost_inputs(row, bar, events, pointvalue):
    exits = row["exit_rows"]
    exited_qty = sum((abs(part["size"]) for part in exits), ZERO)
    carry = sum((abs(part["size"]) * sum(weight for ts, weight in events
                 if row["entry_time"] < ts < part["exit_time"]) for part in exits), ZERO)
    return {
        "entry_units_value": row["submitted_quantity"] * pointvalue,
        "turnover_units_value": (row["submitted_quantity"] + exited_qty) * pointvalue,
        "original_commission": row["realized_commission_cash"],
        "weighted_open_unit_rollovers": carry,
        "signal_range_atr": (bar["high"] - bar["low"]) / float(row["submitted_metadata"]["atr"]),
    }


def cost_components(inputs, scenario):
    if inputs["signal_range_atr"] < scenario.get("apply_only_if_signal_range_atr_at_least", 0):
        return dict.fromkeys(("spread", "slip", "fee", "carry"), ZERO)
    return {
        "spread": inputs["entry_units_value"] * Decimal(scenario.get("extra_round_trip_spread_price", "0")),
        "slip": inputs["turnover_units_value"] * Decimal(scenario.get("extra_slippage_price_per_fill", "0")),
        "fee": inputs["original_commission"] * Decimal(scenario.get("incremental_commission_fraction", "0")),
        "carry": inputs["weighted_open_unit_rollovers"] * Decimal(scenario.get("carry_cash_per_unit_event", "0")),
    }


def scenario_metrics(campaigns, inputs, scenario):
    components = [cost_components(item, scenario) for item in inputs]
    totals = {key: sum((x[key] for x in components), ZERO) for key in ("spread", "slip", "fee", "carry")}
    penalties = [sum(row.values(), ZERO) for row in components]
    cash = [row["realized_net_cash"] - penalty for row, penalty in zip(campaigns, penalties)]
    risk_r = [float(net / row["initial_risk_cash"]) for row, net in zip(campaigns, cash)]
    return {
        **stats(cash),
        "affected_campaigns": sum(value > 0 for value in penalties),
        "incremental_cost_usd": totals,
        "average_net_r": block_bootstrap(risk_r),
        "cash_drawdown": closed_drawdown((row["last_exit_time"], i, net)
                                         for i, (row, net) in enumerate(zip(campaigns, cash)))["max_drawdown_cash"],
        "native_equity_drawdown": None,
    }


def delay_diagnostic(campaigns, bars):
    drifts = []
    same_or_earlier_exit = gaps = unavailable = 0
    for row in campaigns:
        i = row["entry_bar_index"]
        if i + 1 >= len(bars):
            unavailable += 1
        elif i + 1 >= min(part["exit_bar_index"] for part in row["exit_rows"]):
            same_or_earlier_exit += 1
        elif bars[i+1]["time"] != bars[i]["time_close"]:
            gaps += 1
        else:
            drifts.append((bars[i+1]["open"] - bars[i]["open"]) * row["side"])
    return {"signed_price_drift": describe(drifts), "abs_price_drift": describe([abs(x) for x in drifts]),
            "adverse_price_drift": describe([x for x in drifts if x > 0]),
            "favorable": sum(x < 0 for x in drifts), "flat": sum(x == 0 for x in drifts),
            "prior_or_same_exit_bar": same_or_earlier_exit, "gaps": gaps, "end_unavailable": unavailable,
            "pnl_counterfactual": None}


def risk_record(campaigns, run):
    changes, daily = defaultdict(int), defaultdict(lambda: ZERO)
    risks, actual_risks, notionals = [], [], []
    losses = longest_loss_run = 0
    for row in campaigns:
        meta = row["submitted_metadata"]
        qty = row["submitted_quantity"]
        changes[row["entry_bar_index"]] += 1
        changes[max(part["exit_bar_index"] for part in row["exit_rows"]) + 1] -= 1
        risks.append(float(abs(meta["reference"] - meta["initial_sl"]) * qty * run["pointvalue"]))
        actual_risks.append(float(row["initial_risk_cash"]))
        notionals.append(float(abs(row["actual_entry"]) * qty * run["pointvalue"]))
        losses = losses + 1 if row["realized_net_cash"] < 0 else 0
        longest_loss_run = max(longest_loss_run, losses)
        for part in row["exit_rows"]:
            daily[utc(part["exit_time"]).date().isoformat()] += part["net_cash"]
    current = active_bars = max_same_bar = 0
    previous = 0
    for index, delta in sorted(changes.items()):
        if current:
            active_bars += index - previous
        current += delta
        max_same_bar = max(max_same_bar, current)
        previous = index
    worst_day = min(daily, key=daily.get)
    return {"planned_risk_cash": describe(risks), "initial_fill_risk_cash": describe(actual_risks),
            "fill_notional_native_price_times_pointvalue": describe(notionals),
            "max_concurrent_campaign_bar_spans": max_same_bar, "observed_position_bar_spans": active_bars,
            "bar_span_fraction": active_bars / run["bar_count"], "longest_losing_campaign_run": longest_loss_run,
            "worst_realized_utc_day": worst_day, "worst_realized_utc_day_net_cash": daily[worst_day]}


def cohort_metrics(campaigns, start, end):
    earlier = [c for c in campaigns if c["last_exit_time"] < start]
    later = [c for c in campaigns if start <= c["entry_time"] and c["last_exit_time"] < end]
    buckets = defaultdict(list)
    for c in campaigns:
        buckets[utc(c["entry_time"]).strftime("%Y-%m")].append(c)
    return {"development": values(earlier), "validation": values(later),
            "censored_at_cohort_boundaries": len(campaigns) - len(earlier) - len(later),
            "entry_month_utc": {month: values(rows) for month, rows in sorted(buckets.items())}}


def run_frame(tf, source, published, logs_dir, registration, limits):
    log = logs_dir / source["log_name"]
    raw_bars = ROOT / source["local_bars"]
    read_pinned(log, source["log_sha256"])
    read_pinned(raw_bars, source["bars_sha256"])
    observed = inspect(log)
    run = observed["run"]
    campaigns = sorted(observed["campaigns"], key=lambda row: (row["last_exit_time"], row["entry_id"]))
    if observed["mode"] != "S5" or run["timeframe"] != tf or observed["unfilled"] or any(
            row["state"] != "COMPLETE" or row["initial_risk_cash"] is None for row in campaigns):
        raise ValueError("Unexpected S5 exposure or incomplete baseline")
    bars = load_bars(raw_bars)
    verify_bars(bars, run)
    indices = {bar["time_close"]: i for i, bar in enumerate(bars)}
    averages, context, features = atr_average(bars), structural_context(bars), build_features(bars)
    for row in campaigns:
        # Confirm actual archived S5 D20 seed and decision mapping using the pinned verifier.
        signal_features(row, bars, indices, averages, tf)
    screens = [signal_screen(row, bars, indices, context, features) for row in campaigns]
    calendar = financing_events(bars[0]["time"], bars[-1]["time_close"])
    inputs = [cost_inputs(row, bars[row["submitted_metadata"]["submitted_bar"]], calendar, run["pointvalue"])
              for row in campaigns]
    cost_cases = registration["cost_scenarios"]
    scenarios = {name: scenario_metrics(campaigns, inputs, scenario) for name, scenario in cost_cases.items()}
    baseline, base_prior = scenarios["C0"], published["baseline"]
    for key in ("count", "wins", "losses", "net_cash"):
        if baseline[key] != Decimal(str(base_prior[key])):
            raise ValueError(f"Raw S5 does not reproduce original metric {key}")
    baseline.update({
        "native_equity_drawdown": observed["native_equity_drawdown"]["value"],
        "parts": observed["native_parts"],
        "fees_already_in_net_cash": observed["cash"]["commission_cash"],
    })
    gates = registration["primary_screen_gates"]
    trials = {}
    for code in registration["signal_screens"]:
        choices = [s[code] is True for s in screens]
        selected = [row for row, keep in zip(campaigns, choices) if keep]
        rejected = [row for row, keep in zip(campaigns, choices) if not keep]
        selected_inputs = [item for item, keep in zip(inputs, choices) if keep]
        base = scenario_metrics(selected, selected_inputs, cost_cases["C0"])
        stressed = scenario_metrics(selected, selected_inputs, cost_cases["C7"])
        delta_r = [0. if keep else -float(row["net_r"]) for row, keep in zip(campaigns, choices)]
        by_side = {name: values([row for row in selected if row["side"] == side])
                   for name, side in (("BUY", 1), ("SELL", -1))}
        result = {"retained": base, "rejected": values(rejected), "missing": sum(s[code] is None for s in screens),
                  "combined_cost": stressed, "side": by_side, "chronology": cohort_metrics(selected, *limits),
                  "sign_flip_incremental_r_p": sign_flip_p(delta_r)}
        if tf == "5":
            n, pf, wr = base["count"], base["profit_factor"], base["win_rate"]
            result["failed_screen_gates"] = [key for key, ok in {
                "frequency": n >= baseline["count"] * gates["min_retained_fraction"],
                "both_sides": all(group["count"] >= gates["min_retained_each_side"] for group in by_side.values()),
                "mean_r_gain": n and base["average_net_r"]["mean"] >= (
                    baseline["average_net_r"]["mean"] + gates["min_mean_net_r_gain"]),
                "profit_factor": pf is not None and pf >= Decimal(str(gates["min_profit_factor"])),
                "win_rate": not gates["win_rate_must_not_decrease"] or (wr is not None and wr >= baseline["win_rate"]),
                "cash_drawdown": base["cash_drawdown"] <= (
                    baseline["cash_drawdown"] * gates["max_campaign_cash_drawdown_multiple"]),
                "combined_cost": stressed["net_cash"] > gates["min_combined_stress_net_usd"],
            }.items() if not ok]
        trials[code] = result
    if tf == "5":
        adjusted = holm([row["sign_flip_incremental_r_p"] for row in trials.values()])
        for result, p in zip(trials.values(), adjusted):
            result["holm_incremental_r_p"] = p
    return {"run": run, "verified_original_inputs": source, "costs": scenarios, "trials": trials,
            "baseline_chronology": cohort_metrics(campaigns, *limits),
            "risk": risk_record(campaigns, run), "additional_bar_open_drift": delay_diagnostic(campaigns, bars),
            "available_extra_round_trip_spread_price_at_cash_breakeven": (
                max(ZERO, baseline["net_cash"]) / sum((x["entry_units_value"] for x in inputs), ZERO)),
            "campaigns": [{"id": row["plan_id"], "side": row["side"], "net_cash": row["realized_net_cash"],
                           "net_r": row["net_r"], "initial_risk_cash": row["initial_risk_cash"],
                           "screen_flags": s} for row, s in zip(campaigns, screens)]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs-dir", type=Path, default=ROOT.parent / "tv-final")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raw = REGISTRATION.read_bytes()
    r = json.loads(raw)
    original = json.loads(read_pinned(ROOT / r["original_input_registration"],
                                     r["original_input_registration_sha256"]))
    published = json.loads(read_pinned(ROOT / r["original_baseline_and_scope_check"],
                                      r["original_baseline_and_scope_check_sha256"]))["frames"]
    limits = tuple(int(datetime.fromisoformat(original["chronology"][key]).timestamp() * 1000)
                   for key in ("validation_entry_start_utc", "validation_last_exit_before_utc"))
    output = {"schema": "qirvanta.premium-v2.quant.results.v1", "status": "NO_VALIDATED_IMPROVEMENT",
              "registration_sha256": sha(raw), "tool_sha256": sha(Path(__file__).read_bytes()),
              "classification": r["evidence_class"],
              "frames": {tf: run_frame(tf, source, published[tf], args.logs_dir, r, limits)
                         for tf, source in original["frames"].items()}}
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(output, stream, indent=2, ensure_ascii=False, default=json_default, allow_nan=False)
        stream.write("\n")


if __name__ == "__main__":
    main()
