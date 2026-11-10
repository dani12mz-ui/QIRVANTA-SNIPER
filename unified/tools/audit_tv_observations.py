"""Reconcile separately exported TradingView runs without synthesizing trades."""
import argparse
from collections import Counter, defaultdict
from decimal import Decimal
import gzip
import io
import json
from pathlib import Path

from diagnostics.accounting.audit import audit, json_default, read_jsonl, stats
from research.validation.native_report import sessions, utc
from research.validation.statistics import block_bootstrap, holm, quantile, sign_flip_p, wilson
from unified.tools.audit_tradingview_only import ARCHIVES
from unified.tools.audit_ultimate_native import underwater_duration, without_run_id
from unified.tools.build_tradingview_final import CHAMPION_SHA, OUTPUT, ROOT, sha


FRAMES = ("5", "15", "30")
S_MODES = tuple(f"S{i}" for i in range(10) if i != 6)
F_MODES = tuple(f"F{i}" for i in range(11))
EXPECTED_GRID = {tf: S_MODES + F_MODES + (("S6", "S10") if tf == "15" else ()) for tf in FRAMES}
ALIGNMENT_FIELDS = ("symbol", "timeframe", "bar_count", "first_bar_time", "last_bar_open",
                    "cutoff", "checksum1", "checksum2", "pointvalue", "mintick",
                    "account_currency", "symbol_currency")
POSITION_SIZE = Decimal("3")
ROUND_TRIP_FEE_PER_UNIT = Decimal(".350")


def validate_execution(result):
    plans = [row["submitted_metadata"] for row in result["campaigns"]]
    plans.extend(result["submitted_without_recorded_fill"])
    if any(plan["quantity"] != POSITION_SIZE for plan in plans):
        raise ValueError("Observed submitted size differs from fixed matrix size")
    if any(part["commission"] != abs(part["size"]) * ROUND_TRIP_FEE_PER_UNIT
           for row in result["campaigns"] for part in row["exit_rows"]):
        raise ValueError("Observed part commission differs from fixed matrix cost")


def inspect(path):
    records, _ = read_jsonl(path, allow_pine_prefix=True, origin_override="REAL_TRADINGVIEW")
    run = next(record for record in records if record["kind"] == "run")
    if not run["run_id"].startswith(("tv_final|", "ui_only|")) or run["symbol"] != "OANDA:XAUUSD":
        raise ValueError("Unexpected TradingView registration")
    mode = run["run_id"].split("|")[1] if run["run_id"].startswith("tv_final|") else "CHAMPION"
    if run["timeframe"] not in FRAMES or run["run_id"].split("|")[-1] != "Всички":
        raise ValueError("Unexpected timeframe or family filter")
    expected_source = CHAMPION_SHA if mode == "CHAMPION" else sha(OUTPUT.read_bytes())
    if run["source_sha256"] != expected_source:
        raise ValueError("Unexpected TradingView release source")
    result = audit(records)
    if result["status"] != "PASS_OBSERVED_JOURNAL" or not result["cash_reconciliation"]["matches"]:
        raise ValueError(f"Incomplete or unreconciled native evidence: {path.name}")
    validate_execution(result)
    campaigns = [row for row in result["campaigns"] if row["state"] == "COMPLETE"]
    campaigns.sort(key=lambda row: (row["last_exit_time"], row["entry_id"]))
    metrics = result["complete_campaign_metrics"]
    returns = [float(row["net_r"]) for row in campaigns if row["net_r"] is not None]
    metrics = {**metrics, "net_r_ci95": block_bootstrap(returns),
               "campaign_win_rate_ci95": wilson(metrics["wins"], len(campaigns)),
               "campaign_drawdown_duration": underwater_duration(
                   result["complete_campaign_close_drawdown"]["curve"],
                   min((row["entry_time"] for row in campaigns), default=run["cutoff"]), run["cutoff"]),
               "tp_campaign_counts": {
                   f"TP{i}": sum(any(part["exit_comment"] == f"TP{i}" for part in row["exit_rows"])
                                 for row in campaigns) for i in (1, 2, 3)},
               "stop_before_tp1": sum(any(p["exit_comment"] == "SL" for p in row["exit_rows"])
                                      and not any(p["exit_comment"] == "TP1" for p in row["exit_rows"])
                                      for row in campaigns)}
    return {"mode": mode, "run": run, "log_name": path.name, "log_sha256": sha(path.read_bytes()),
            "status": result["status"], "native_equity_drawdown": result["native_equity_max_drawdown"],
            "native_parts": result["observed_closed_row_metrics"]["count"],
            "coverage": result["coverage"], "states": result["campaign_state_counts"],
            "unfilled": result["submitted_without_recorded_fill"],
            "cash": result["cash_reconciliation"], "campaign_metrics": metrics,
            "campaigns": result["campaigns"]}


def collect(paths):
    results = {}
    for path in sorted(paths, key=lambda p: p.name):
        observed = inspect(path)
        run = observed["run"]
        frame = results.setdefault(run["timeframe"], {})
        if observed["mode"] in frame:
            raise ValueError("Repeated mode in one timeframe")
        frame[observed["mode"]] = observed
    alignment = {}
    for minutes, frame in results.items():
        comparison = next(iter(frame.values()))["run"]
        alignment[minutes] = all(all(row["run"][field] == comparison[field] for field in ALIGNMENT_FIELDS)
                                 for row in frame.values())
    return {"schema": "qirvanta.tv_only.native.v1", "observed_checksum_alignment": alignment,
            "timeframes": results, "independent_forward_evidence": False}


def compare_s0(frames):
    checks = {}
    for tf, runs in frames.items():
        if "S0" not in runs:
            continue
        current = runs["S0"]
        path = ROOT / ARCHIVES[tf]
        baseline = json.loads(path.read_bytes())
        actual = json.loads(json.dumps(current["campaigns"], default=json_default, allow_nan=False))
        run = json.loads(json.dumps(current["run"], default=json_default, allow_nan=False))
        checks[tf] = {
            "preserved_audit": ARCHIVES[tf],
            "preserved_audit_sha256": sha(path.read_bytes()),
            "fingerprint_matches": all(run[k] == baseline["run"][k] for k in ALIGNMENT_FIELDS),
            "exact_campaign_objects_except_run_id": without_run_id(actual) == without_run_id(baseline["campaigns"]),
            "native_drawdown_matches": current["native_equity_drawdown"]["value"] == Decimal(
                str(baseline["native_equity_max_drawdown"]["value"])),
        }
    return checks


def key_campaign(row, mode, *, seed_only):
    plan = row["plan_id"]
    prefix, raw = plan.split("|", 1)
    parts = raw.split("|")
    if prefix != mode or len(parts) != 6 or parts[0] != "OANDA:XAUUSD":
        raise ValueError(f"Unexpected FAST identity: {plan}")
    seed_time, confirmation_time = map(int, parts[-2:])
    if confirmation_time != row["submitted_metadata"]["submitted_time"] or seed_time > confirmation_time:
        raise ValueError(f"FAST identity timing differs: {plan}")
    return "|".join(parts[:-1]) if seed_only else raw


def compare_fast(frame):
    if "F0" not in frame:
        return {}

    def index(mode, seed_only):
        rows = frame[mode]["campaigns"]
        indexed = {key_campaign(row, mode, seed_only=seed_only): row for row in rows}
        if len(indexed) != len(rows):
            raise ValueError("FAST origin or admission identity collides")
        return indexed

    def outcome(rows):
        done = [row for row in rows if row["state"] == "COMPLETE"]
        return {**stats(row["realized_net_cash"] for row in done),
                "censored": len(rows) - len(done), "plan_ids": [row["plan_id"] for row in rows]}

    deltas = {}
    for mode in F_MODES[1:]:
        if mode not in frame:
            continue
        delayed = mode in {"F2", "F4"}
        original, trial = index("F0", delayed), index(mode, delayed)
        shared = sorted(original.keys() & trial.keys())
        removed = [original[k] for k in sorted(original.keys() - trial.keys())]
        added = [trial[k] for k in sorted(trial.keys() - original.keys())]
        deltas[mode] = {
            "match_basis": "raw signal seed only, later confirmation and geometry" if delayed
                           else "plan identity excluding mode prefix",
            "shared_count": len(shared),
            "shared_changed_confirmation_count": sum(
                original[k]["submitted_metadata"]["submitted_time"] != trial[k]["submitted_metadata"]["submitted_time"]
                for k in shared),
            "shared_changed_outcome_count": sum(
                (original[k]["state"], original[k]["realized_net_cash"], original[k]["last_exit_time"]) !=
                (trial[k]["state"], trial[k]["realized_net_cash"], trial[k]["last_exit_time"]) for k in shared),
            "f0_only": outcome(removed), "trial_only": outcome(added),
            "shared_f0_net_cash": sum(original[k]["realized_net_cash"] for k in shared),
            "shared_trial_net_cash": sum(trial[k]["realized_net_cash"] for k in shared),
            "native_net_cash_delta": frame[mode]["cash"]["native_netprofit"] - frame["F0"]["cash"]["native_netprofit"],
            "scope": "Independent native admissions and changed occupancy; F0-only does not identify the rejection mechanism.",
        }
    return deltas


def distributions(campaigns, span, tf):
    groups = {name: defaultdict(list) for name in ("entry_month_utc", "side", "trend_relation", "regime", "session")}
    ages, delay_bars, delay_minutes, risk_cash, risk_atr, risk_reward = [], [], [], [], [], defaultdict(list)
    for row in campaigns:
        meta = row["submitted_metadata"]
        stamp = row["entry_time"]
        for name, value in (
            ("entry_month_utc", utc(stamp).strftime("%Y-%m")), ("side", "BUY" if row["side"] == 1 else "SELL"),
            ("trend_relation", row["trend_relation"]), ("regime", row["regime"]), ("session", sessions(stamp)),
        ):
            groups[name][value].append(row)
        try:
            seed = int(row["plan_id"].split("|")[-2])
        except (ValueError, IndexError):
            seed = None
        if seed is not None:
            ages.append((meta["submitted_time"] - seed) / 60000)
        delay_bars.append(row["entry_bar_index"] - meta["submitted_bar"])
        delay_minutes.append((stamp - meta["submitted_time"]) / 60000)
        if row["initial_risk_cash"] is None:
            continue
        risk_cash.append(float(row["initial_risk_cash"]))
        distance = abs(row["actual_entry"] - meta["initial_sl"])
        if meta["atr"] > 0:
            risk_atr.append(float(distance / meta["atr"]))
        for tp in ("tp1", "tp2", "tp3"):
            risk_reward[tp].append(float((meta[tp] - row["actual_entry"]) * row["side"] / distance))

    def description(values):
        return {"n": len(values), "p50": quantile(values, .5), "p95": quantile(values, .95)}

    return {
        "breakdown": {name: {key: stats(c["realized_net_cash"] for c in rows)
                             for key, rows in sorted(buckets.items())} for name, buckets in groups.items()},
        "frequency_complete_campaigns_per_1000_loaded_bars": len(campaigns) / span * 1000,
        "signal_age_seed_to_submission_minutes": description(ages),
        "entry_delay_bars": description(delay_bars), "entry_delay_minutes": description(delay_minutes),
        "holding_bars": description([row["holding_bars"] for row in campaigns]),
        "holding_calendar_minutes": description([(row["last_exit_time"] - row["entry_time"]) / 60000
                                                 for row in campaigns]),
        "nominal_48_bar_horizon_minutes": 48 * int(tf),
        "initial_actual_risk_usd": description(risk_cash),
        "initial_actual_stop_distance_atr": description(risk_atr),
        "actual_entry_to_frozen_target_rr": {key: description(values) for key, values in risk_reward.items()},
    }


def summarize_run(observed, window):
    campaigns = observed["campaigns"]
    complete = sorted((c for c in campaigns if c["state"] == "COMPLETE"),
                      key=lambda c: (c["last_exit_time"], c["entry_id"]))
    metrics = observed["campaign_metrics"]
    returns = [float(c["net_r"]) for c in complete if c["net_r"] is not None]
    run, n = observed["run"], len(complete)
    cohort = [c for c in campaigns if window["start"] <= c["entry_time"] < window["end"]]
    closed_cohort = [c for c in cohort if c["state"] == "COMPLETE" and c["last_exit_time"] < window["end"]]
    carry = [c for c in campaigns if c["entry_time"] < window["start"] and
             (c["state"] != "COMPLETE" or c["last_exit_time"] >= window["start"])]
    summary = {k: v for k, v in observed.items() if k not in {"campaigns", "unfilled"}}
    summary.update({
        "net_r_sum": sum(returns) if returns else None,
        "unscored_complete_r": n - len(returns),
        "one_sided_sign_flip_net_r_p": sign_flip_p(returns) if len(returns) > 5 else None,
        "submitted_without_recorded_fill": len(observed["unfilled"]),
        "closed_part_reason_counts_including_censored": dict(Counter(
            x["exit_comment"] or "<NO_COMMENT>" for c in campaigns for x in c["exit_rows"])),
        "campaigns_with_tp_and_sl_recorded_on_same_bar": sum(
            bool({x["exit_bar_index"] for x in c["exit_rows"] if x["exit_comment"] == "SL"}
                 & {x["exit_bar_index"] for x in c["exit_rows"] if x["exit_comment"] in {"TP1", "TP2", "TP3"}})
            for c in campaigns
        ),
        "tp_campaign_hit_rates": {k: v / n if n else None for k, v in metrics["tp_campaign_counts"].items()},
        "sl_before_tp1_rate": metrics["stop_before_tp1"] / n if n else None,
        "common_entry_cohort": {
            **stats(c["realized_net_cash"] for c in closed_cohort),
            "entry_cohort_count": len(cohort), "right_censored": len(cohort) - len(closed_cohort),
            "carryover": len(carry),
        },
        "fixed_fill_cost_stress": {
            "fee_multiplier": 1.5, "additional_adverse_ticks_per_fill": 1,
            "scope": "Cash penalty on fixed fills, including all entry and exit quantities. No order replay.",
            **stats(c["realized_net_cash"] - c["realized_commission_cash"] / 2
                    - (c["submitted_quantity"] + c["closed_quantity"])
                    * Decimal(str(run["mintick"])) * Decimal(str(run["pointvalue"])) for c in complete),
        },
        **distributions(complete, run["bar_count"], run["timeframe"]),
    })
    return summary


def sparse_records(tf, mode, observed):
    for row in observed["campaigns"]:
        yield {
            "kind": "campaign", "timeframe": tf, "mode": mode,
            **{k: v for k, v in row.items() if k not in {"exit_rows", "closed_indices", "submitted_metadata",
                 "incomplete_reasons", "campaign_max_drawdown", "campaign_max_runup", "excursion_limitation"}},
            "submitted": without_run_id(row["submitted_metadata"]),
            "parts": [without_run_id(part) for part in row["exit_rows"]],
        }
    for unfilled in observed["unfilled"]:
        yield {"kind": "submitted_without_recorded_fill", "timeframe": tf, "mode": mode,
               "submitted": without_run_id(unfilled)}


def packaged(paths, ledger_name):
    observed = collect(paths)
    frames = observed["timeframes"]
    if not all(observed["observed_checksum_alignment"].values()):
        raise ValueError("Per-frame bar fingerprints or contract specifications do not match")
    parity = compare_s0(frames)
    if any(not all(check[field] for field in ("fingerprint_matches",
               "exact_campaign_objects_except_run_id", "native_drawdown_matches")) for check in parity.values()):
        raise ValueError("Observed S0 differs from preserved Champion")
    missing = {tf: [mode for mode in expected if mode not in frames.get(tf, {})]
               for tf, expected in EXPECTED_GRID.items()}
    if any(mode not in EXPECTED_GRID[tf] for tf, runs in frames.items() for mode in runs):
        raise ValueError("Unexpected mode in native evidence matrix")
    common = json.loads((ROOT / "research/reports/actual_tf_results.json").read_bytes())["common_window"]
    window = {"start": common["start"], "end": common["end"],
              "scope": "Preserved entry window with exits before end; inherits prior run occupancy."}
    summaries = {tf: {mode: summarize_run(runs[mode], window) for mode in EXPECTED_GRID[tf] if mode in runs}
                 for tf, runs in sorted(frames.items(), key=lambda item: int(item[0]))}
    tested = [(tf, mode) for tf in FRAMES for mode in S_MODES[1:] + F_MODES[1:]]
    raw_p = [summaries.get(tf, {}).get(mode, {}).get("one_sided_sign_flip_net_r_p") for tf, mode in tested]
    adjusted = holm([p if p is not None else 1. for p in raw_p])
    multiple = {
        "scope": "54 planned exploratory cells. S0/F0 controls, blocked S6/S10 excluded. "
                 "Missing or at most five scored returns assigned p=1 for adjustment only. "
                 "Sign-flip assumes exchangeability and does not establish performance under dependent returns.",
        "cells": [{"timeframe": tf, "mode": mode, "raw_p": p, "holm_p": q if p is not None else None}
                  for (tf, mode), p, q in zip(tested, raw_p, adjusted)],
    }
    ledger = io.BytesIO()
    n_campaigns = n_parts = n_unfilled = 0
    with gzip.GzipFile(filename="", mode="wb", fileobj=ledger, mtime=0) as compressed:
        for tf, runs in summaries.items():
            for mode in runs:
                for row in sparse_records(tf, mode, frames[tf][mode]):
                    if row["kind"] == "campaign":
                        n_campaigns += 1
                        n_parts += len(row["parts"])
                    else:
                        n_unfilled += 1
                    record = json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
                                        default=json_default, allow_nan=False)
                    compressed.write((record + "\n").encode("utf-8"))
    data = ledger.getvalue()
    return {
        "schema": "qirvanta.tv_only.native.summary.v2",
        "source_sha256": sha(OUTPUT.read_bytes()), "audit_tool_sha256": sha(Path(__file__).read_bytes()),
        "status": "HISTORICAL_EXPLORATORY_NOT_FORWARD_VALIDATION",
        "observed_checksum_alignment": observed["observed_checksum_alignment"],
        "expected_cells": sum(map(len, EXPECTED_GRID.values())), "observed_cells": sum(map(len, summaries.values())),
        "missing_modes": missing, "s0_preserved_campaign_parity": parity, "common_window": window,
        "observed_cost_policy": {"quantity": POSITION_SIZE, "commission_usd_per_unit_per_fill": ROUND_TRIP_FEE_PER_UNIT / 2,
                                 "commission_and_size_verified_on_rows": True,
                                 "slippage_ticks_in_source_and_properties": 2},
        "fast_f0_identity_comparison": {tf: compare_fast(runs) for tf, runs in frames.items()},
        "exploratory_multiple_testing": multiple, "timeframes": summaries,
        "ledger": {"file": ledger_name, "sha256": sha(data), "format": "JSONL gzip, UTF-8, sparse observations",
                   "campaigns": n_campaigns, "closed_parts": n_parts, "submitted_without_recorded_fill": n_unfilled},
    }, data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", type=Path, nargs="+")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ledger", type=Path, help="Write a separate sparse gzip JSONL ledger and concise matrix summary")
    parser.add_argument("--require-complete-grid", action="store_true", help="Reject a matrix with missing expected modes")
    args = parser.parse_args()
    if args.require_complete_grid and not args.ledger:
        parser.error("--require-complete-grid requires --ledger")
    if args.ledger:
        if args.ledger.resolve() == args.output.resolve() or args.ledger.exists() or args.output.exists():
            parser.error("Use distinct new paths for summary and ledger")
        evidence, data = packaged(args.logs, args.ledger.name)
        if args.require_complete_grid and any(evidence["missing_modes"].values()):
            parser.error(f"Native matrix is incomplete: {evidence['missing_modes']}")
        with args.ledger.open("xb") as stream:
            stream.write(data)
    else:
        evidence = collect(args.logs)
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(evidence, stream, ensure_ascii=False, indent=2, default=json_default, allow_nan=False)
        stream.write("\n")


if __name__ == "__main__":
    main()
