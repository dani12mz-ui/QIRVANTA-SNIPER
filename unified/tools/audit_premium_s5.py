"""Audit original S5 Pine Logs and screen frozen campaign outcomes on causal features."""
import argparse
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
import json
from math import floor, isclose
from operator import eq, ge, le
from pathlib import Path
from statistics import mean

from diagnostics.accounting.audit import closed_drawdown, json_default, stats
from diagnostics.protocol.extract_bars import COLUMNS
from research.data.loader import load_bars
from research.features.causal import Smooth
from research.validation.native_report import sessions, utc
from research.validation.statistics import block_bootstrap, holm, sign_flip_p
from unified.tools.audit_tv_observations import ALIGNMENT_FIELDS, inspect
from unified.tools.build_tradingview_final import ROOT, sha


REGISTRATION = ROOT / "docs/premium_evidence/s5_screen_registration.json"
NATIVE_MATRIX = ROOT / "docs/tradingview_only_evidence/native_matrix.json"


def verify_bars(bars, run):
    if (len(bars), bars[0]["time"], bars[-1]["time_close"]) != (
            run["bar_count"], run["first_bar_time"], run["cutoff"]):
        raise ValueError("Original Pine run and OHLC coverage differ")
    tick = float(run["mintick"])
    h1 = h2 = 0
    for bar in bars:
        for key in ("time", *COLUMNS[2:]):
            value = bar[key]
            q = -999999 if value is None else floor(
                (value if key in {"time", "volume"} else value / tick) + .5)
            h1 = (h1 * 131 + q) % 1_000_000_007
            h2 = (h2 * 137 + q) % 1_000_000_009
    if (h1, h2) != (run["checksum1"], run["checksum2"]):
        raise ValueError(f"Original Pine fingerprints differ from the serialized OHLC: {(h1, h2)}")


def atr_average(bars):
    atr, past = Smooth(14, wilder=True), []
    averages = []
    previous_close = bars[0]["close"]
    for bar in bars:
        tr = max(bar["high"] - bar["low"], abs(bar["high"] - previous_close),
                 abs(bar["low"] - previous_close))
        value = atr.add(tr)
        if value is not None:
            past.append(value)
        averages.append(mean(past[-40:]) if len(past) >= 40 else None)
        previous_close = bar["close"]
    return averages


def signal_features(campaign, bars, indices, averages, timeframe):
    meta, side = campaign["submitted_metadata"], campaign["side"]
    mode, symbol, tf, family, direction, seed_time, confirm_time = campaign["plan_id"].split("|")
    if (mode, symbol, tf, family, int(direction), int(confirm_time)) != (
            "S5", "OANDA:XAUUSD", timeframe, "2", side, meta["submitted_time"]):
        raise ValueError("Original S5 plan identity differs")
    seed_i, i = indices[int(seed_time)], meta["submitted_bar"]
    seed, signal = bars[seed_i], bars[i]
    op, high, low, close = (signal[k] for k in ("open", "high", "low", "close"))
    seed_age = i - seed_i
    if (i != indices[int(confirm_time)] or seed_i < 20 or not 1 <= seed_age <= 3 or
            not isclose(close, float(meta["reference"]), rel_tol=0, abs_tol=.00051)):
        raise ValueError("Original S5 confirmation time, seed age or price differs")
    range_key, extreme = ("low", min) if side == 1 else ("high", max)
    boundary = extreme(bar[range_key] for bar in bars[seed_i-20:seed_i])
    penetration = (boundary - seed["close"]) * side
    extension = (close - boundary) * side
    atr = float(meta["atr"])
    if (penetration <= 0 or extension <= 0 or atr <= 0 or
            (bars[seed_i-1]["close"] - boundary) * side < 0):
        raise ValueError("Original S5 breakout seed or reclaim differs")
    risk = (close - float(meta["initial_sl"])) * side
    prior = bars[i-20:i]
    obstacles = [max(bar["high"] for bar in prior), min(bar["low"] for bar in prior),
                 signal["priorDayHigh"], signal["priorDayLow"]]
    forward = [(level - close) * side for level in obstacles
               if level is not None and (level - close) * side > 0]
    candle_range = high - low
    wick = min(op, close) - low if side == 1 else high - max(op, close)
    location = close - low if side == 1 else high - close
    session = sessions(signal["time_close"])
    return {
        "signed_body_atr": side * (close - op) / atr,
        "seed_penetration_atr": penetration / atr,
        "native_h1_matches": int(meta["htf_bias"] == side) if timeframe == "5" else None,
        "rejection_wick_range": wick / candle_range if candle_range > 0 else None,
        "seed_age_bars": seed_age,
        "reclaim_extension_atr": extension / atr,
        "efficiency": float(meta["efficiency"]),
        "known_obstacle_room_r_proxy": min(forward) / risk if forward and risk > 0 else None,
        "atr_vs_sma40_proxy": atr / averages[i] if averages[i] else None,
        "in_london_or_ny": (None if session == "TIMEZONE_DATABASE_UNAVAILABLE"
                            else int(session != "OUTSIDE_DECLARED_LONDON_NY_WINDOWS")),
        "signed_3bar_close_change_atr": side * (close - bars[i-3]["close"]) / atr,
        "directional_close_location": location / candle_range if candle_range > 0 else None,
    }


def predicate(value, spec):
    if value is None:
        return False
    op, threshold = spec["op"], spec["threshold"]
    if op == "between":
        low, high = threshold
        return low <= value <= high
    return {"ge": ge, "le": le, "eq": eq}[op](value, threshold)


def values(rows):
    returns = [float(row["net_r"]) for row in rows]
    return {**stats(row["realized_net_cash"] for row in rows),
            "net_r_sum": sum(returns) if returns else None,
            "mean_net_r": mean(returns) if returns else None}


def summarize(rows, run, limits):
    fixed = values(rows)
    weeks = defaultdict(list)
    for row in rows:
        week = utc(row["entry_time"]).strftime("%G-W%V")
        weeks[week].append(row)
    start, end = limits
    development = [row for row in rows if row["last_exit_time"] < start]
    validation = [row for row in rows if start <= row["entry_time"] and row["last_exit_time"] < end]
    tick_cash = run["mintick"] * run["pointvalue"]
    parts = [part for row in rows for part in row["exit_rows"]]
    hit = lambda row, tag: any(part["exit_comment"] == tag for part in row["exit_rows"])
    return {
        **fixed,
        "campaign_close_drawdown": closed_drawdown(
            (row["last_exit_time"], i, row["realized_net_cash"]) for i, row in enumerate(rows)
        )["max_drawdown_cash"],
        "interval": block_bootstrap([float(row["net_r"]) for row in rows]),
        "stress_net_cash": sum((row["realized_net_cash"] - row["realized_commission_cash"] / 2
                                - (row["submitted_quantity"] + row["closed_quantity"]) * tick_cash
                                for row in rows), Decimal(0)),
        "fees_paid": sum((part["commission"] for part in parts), Decimal(0)),
        "native_closed_parts": len(parts),
        "tp_campaign_counts": {tag: sum(hit(row, tag) for row in rows) for tag in ("TP1", "TP2", "TP3")},
        "stop_before_tp1": sum(hit(row, "SL") and not hit(row, "TP1") for row in rows),
        "side": {name: values([row for row in rows if row["side"] == side])
                 for name, side in (("BUY", 1), ("SELL", -1))},
        "development": values(development),
        "validation": values(validation),
        "excluded_from_cohorts": len(rows) - len(development) - len(validation),
        "chronological_weeks": {week: values(group) for week, group in sorted(weeks.items())},
    }


def pass_flags(trial, control, thresholds):
    def exceeds(metric, delta=0):
        value = trial[metric]
        return value is not None and value >= control[metric] + delta

    return {
        "frequency": trial["count"] >= thresholds["min_retained_fraction"] * control["count"],
        "side_sample": all(group["count"] >= thresholds["min_retained_per_side"]
                           for group in trial["side"].values()),
        "chronological_sample": all(trial[key]["count"] >= thresholds["min_retained_per_development_and_validation"]
                                    for key in ("development", "validation")),
        "win_rate_gain": exceeds("win_rate", Decimal(str(thresholds["min_win_rate_improvement"]))),
        "mean_net_r_gain": exceeds("mean_net_r", thresholds["min_mean_net_r_improvement"]),
        "profit_factor": trial["profit_factor"] is not None and trial["profit_factor"] >= Decimal(
            str(thresholds["min_profit_factor"])),
        "average_winner": trial["average_win_cash"] is not None and trial["average_win_cash"] >= (
            control["average_win_cash"] * Decimal(str(thresholds["min_avg_winner_fraction"]))),
        "average_loser": trial["average_loss_cash"] is not None and abs(trial["average_loss_cash"]) <= (
            abs(control["average_loss_cash"]) * Decimal(str(thresholds["max_avg_loss_magnitude_fraction"]))),
        "net_cash": exceeds("net_cash"),
        "cost_stress": exceeds("stress_net_cash"),
        "campaign_close_drawdown": trial["campaign_close_drawdown"] <= control["campaign_close_drawdown"],
        "chronological_expectancy": all(
            trial[key]["mean_net_r"] is not None and trial[key]["mean_net_r"] > max(0, control[key]["mean_net_r"])
            for key in ("development", "validation")
        ),
    }


def screen(specs, campaigns, features, run, limits, control, thresholds):
    trials = {}
    for mode, spec in specs.items():
        if mode == "Q03" and run["timeframe"] != "5":
            trials[mode] = {"status": "BLOCKED_EXACT_H1_UNAVAILABLE"}
            continue
        kept, excluded, delta = [], [], []
        feature = spec["feature"]
        for row, fields in zip(campaigns, features):
            admitted = predicate(fields[feature], spec)
            (kept if admitted else excluded).append(row)
            delta.append(0. if admitted else -float(row["net_r"]))
        result = summarize(kept, run, limits)
        result.update({
            "status": "OBSERVATIONAL_ONLY",
            "retained_fraction": len(kept) / len(campaigns),
            "excluded": values(excluded),
            "missing_feature": sum(fields[feature] is None for fields in features),
            "net_r_increment_sign_flip_p": sign_flip_p(delta),
            "native_equity_drawdown": None,
        })
        if run["timeframe"] == "5":
            result["screen_gates"] = pass_flags(result, control, thresholds)
        trials[mode] = result
    if run["timeframe"] == "5":
        adjusted = holm([result["net_r_increment_sign_flip_p"] for result in trials.values()])
        for result, p in zip(trials.values(), adjusted):
            result["net_r_increment_holm_p"] = p
    return trials


def audit_frame(frame, source, args, registration, published, limits):
    path = args.logs_dir / source["log_name"]
    bars_path = ROOT / source["local_bars"]
    if sha(path.read_bytes()) != source["log_sha256"] or sha(bars_path.read_bytes()) != source["bars_sha256"]:
        raise ValueError("Pinned raw S5 input SHA differs")
    observed = inspect(path)
    run = observed["run"]
    if run["source_sha256"] != registration["source_sha256"] or any(
            run[field] != Decimal(str(published["run"][field])) if isinstance(run[field], Decimal)
            else run[field] != published["run"][field] for field in ALIGNMENT_FIELDS):
        raise ValueError("Pinned S5 native scope differs")
    bars = load_bars(bars_path)
    verify_bars(bars, run)
    campaigns = sorted(observed["campaigns"], key=lambda row: (row["last_exit_time"], row["entry_id"]))
    if any(row["state"] != "COMPLETE" or row["net_r"] is None for row in campaigns) or observed["unfilled"]:
        raise ValueError("Frozen S5 campaign coverage differs")
    control = summarize(campaigns, run, limits)
    source_summary = published["campaign_metrics"]
    for key in ("count", "wins", "losses", "win_rate", "profit_factor", "net_cash"):
        if not isclose(float(control[key]), source_summary[key], rel_tol=0, abs_tol=1e-12):
            raise ValueError(f"Frozen S5 metric differs: {key}")
    for original, saved in (
        (control["mean_net_r"], source_summary["net_r_ci95"]["mean"]),
        (observed["native_equity_drawdown"]["value"], published["native_equity_drawdown"]["value"]),
        (control["native_closed_parts"], published["native_parts"]),
    ):
        if not isclose(float(original), saved, rel_tol=0, abs_tol=1e-12):
            raise ValueError("Frozen S5 R, native drawdown or part coverage differs")
    control["native_equity_drawdown"] = observed["native_equity_drawdown"]["value"]
    averages = atr_average(bars)
    indices = {bar["time_close"]: i for i, bar in enumerate(bars)}
    features = [signal_features(row, bars, indices, averages, frame) for row in campaigns]
    specs = {k: v for k, v in registration["factor_definitions"].items() if k != "B0"}
    trials = screen(specs, campaigns, features, run, limits, control, registration["historical_screen_gates_primary_m5"])
    evidence = {"run": run, "pinned_input": source, "bar_checksums_recomputed": True,
                "baseline": control, "trials": trials}
    if frame == "5":
        evidence["decisions"] = [
            {"plan_id": row["plan_id"], "net_r": row["net_r"], "realized_net_cash": row["realized_net_cash"],
             "retained_by": [name for name, spec in specs.items() if predicate(f[spec["feature"]], spec)]}
            for row, f in zip(campaigns, features)
        ]
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs-dir", type=Path, default=ROOT.parent / "tv-final")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    registration = json.loads(REGISTRATION.read_bytes())
    raw_matrix = NATIVE_MATRIX.read_bytes()
    if sha(raw_matrix) != registration["native_matrix_sha256"]:
        raise ValueError("Archived native matrix changed")
    published = json.loads(raw_matrix)["timeframes"]
    history = registration["chronology"]
    limits = tuple(int(datetime.fromisoformat(history[key]).timestamp() * 1000) for key in (
        "validation_entry_start_utc", "validation_last_exit_before_utc"))
    evidence = {
        "schema": "qirvanta.premium.s5-screen.results.v1",
        "registration_sha256": sha(REGISTRATION.read_bytes()),
        "script_sha256": sha(Path(__file__).read_bytes()),
        "classification": registration["classification"],
        "status": "NO_VALIDATED_IMPROVEMENT",
        "scope": registration["scope"],
        "frames": {f: audit_frame(f, s, args, registration, published[f]["S5"], limits)
                   for f, s in registration["frames"].items()},
    }
    with args.output.open("x", encoding="utf-8", newline="\n") as file:
        json.dump(evidence, file, indent=2, ensure_ascii=False, default=json_default, allow_nan=False)
        file.write("\n")


if __name__ == "__main__":
    main()
