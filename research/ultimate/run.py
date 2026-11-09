"""Execute frozen historical research and export sparse derived evidence."""
import argparse
from collections import Counter
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import platform

from research.backtesting.engine import BacktestConfig, run_backtest
from research.data.loader import DEFAULT_DATA, load_bars
from research.ultimate.expected_r import expected_r_report
from research.ultimate.fast import (campaign_context, delayed_confirmation, drawdown_duration,
                                    gate, loss_patterns, performance, verify_native)
from research.ultimate.models import research_models
from research.ultimate.replay import COSTS, EXIT_RULES, replay
from research.ultimate.strategies import RULES, discover
from research.ultimate.structure import structural_features
from research.validation.protocol import ROOT, sha
from research.validation.statistics import holm, sign_flip_p


HERE = Path(__file__).resolve().parent


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def dataset_record(path, bars):
    archive = path.with_name("dataset_manifest.json")
    metadata = read_json(archive) if archive.is_file() else {}
    return {"sha256": sha(path), "source": "TradingView observed OANDA:XAUUSD export",
            "timezone": "UTC", "first_open": bars[0]["time"], "last_close": bars[-1]["time_close"],
            "bars": len(bars), "interval_minutes": (bars[0]["time_close"] - bars[0]["time"]) // 60000,
            "retained_time_gaps": sum(a["time_close"] != b["time"] for a, b in zip(bars, bars[1:])),
            "volume": "Provider-supplied volume, unverified as exchange-traded",
            "bid_ask": "UNAVAILABLE", "classification": "ALREADY_OBSERVED",
            "archive_manifest_sha256": sha(archive) if archive.is_file() else None,
            "archive_created_utc": metadata.get("created_utc", metadata.get("created_at_utc")),
            "exact_download_utc": None}


def completed(plans):
    return [{"id": p["id"], "exit_time": p["final_exit_time"], "net_cash": p["net_cash"],
             "net_r": p["net_r"], "exit_reasons": [part["reason"] for part in p["exits"]]}
            for p in plans if p["state"] == "COMPLETE"]


def summarize(plans, paid_cash, refusals, *, cash_events=None):
    done = completed(plans)
    return {
        **performance(done),
        "states": dict(Counter(plan["state"] for plan in plans)),
        "paid_cash_including_open": paid_cash,
        "paid_cash_drawdown_hours": drawdown_duration(cash_events) if cash_events is not None else None,
        "part_count": sum(len(plan["exits"]) for plan in plans),
        "ambiguous_plans": sum(plan["ambiguous"] for plan in plans),
        "rejections": dict(Counter(row["reason"] for row in refusals)),
        "one_sided_sign_flip_net_r_p": sign_flip_p([row["net_r"] for row in done
                                                   if row["net_r"] is not None], repetitions=1000),
        "native_equity_drawdown": None,
    }


def engine_journal(engine):
    plans = [{"id": p["plan_id"], "side": p["side"], "state": p["status"],
              "decision_time": p["confirmation_time"], "reference": p["reference"],
              "initial_stop": p["initial_stop"], "entry": p["entry_price"], "entry_time": p["entry_time"],
              "initial_risk_cash": p["initial_risk_cash"], "remaining_quantity": p["remaining_quantity"],
              "ambiguous": p["ambiguous"], "net_cash": p["net_cash"], "net_r": p["net_r"],
              "paid_entry_fee": p["entry_fee_paid"], "final_exit_time": p["final_exit_time"],
              "mfe_upper_price": p["mfe_price_upper_bound"], "mae_upper_price": p["mae_price_upper_bound"],
              "holding_bars": p["duration_bars"], "targets": p["targets"],
              "exits": [{"id": x["exit_component_id"], "time": x["time"], "size": x["quantity"],
                         "price": x["price"], "reason": x["reason"], "net": x["net_cash"],
                         "fees": x["commission_cash"], "precision": x["time_precision"]} for x in p["exits"]]}
             for p in engine["plans"]]
    return plans


def execute():
    registration = read_json(HERE / "registration.json")
    scope = read_json(HERE / "registration_scope.json")
    if sha(DEFAULT_DATA) != registration["dataset_sha256"]:
        raise ValueError("registered M15 dataset hash changed")
    bars = load_bars()
    features = structural_features(bars)
    audits, observed, native_summary = {}, {}, {}
    for key, expected_hash in registration["audit_sha256"].items():
        path = ROOT / "diagnostics/accounting" / (key + ".audit.json")
        if sha(path) != expected_hash:
            raise ValueError("native audit hash changed")
        audit = read_json(path)
        run = audit["run"]
        if (run["symbol"], run["timeframe"], run["first_bar_time"], run["cutoff"]) != (
                registration["symbol"], "15", bars[0]["time"], bars[-1]["time_close"]):
            raise ValueError("native audit interval or symbol differs")
        audits[key], observed[key] = audit, verify_native(audit)
        native_summary[key] = {
            **performance(observed[key]), "partial_exits": sum(x["parts"] for x in observed[key]),
            "fees": sum(x["fees"] for x in observed[key]),
            "native_equity_drawdown": audit["native_equity_max_drawdown"]["value"],
        }
    actual = native_summary["B-ALL"]
    if (actual["n"], actual["partial_exits"], actual["wins"], actual["losses"], round(actual["net_cash"], 3)) != (
            72, 212, 33, 39, -144.492):
        raise ValueError("champion historical baseline differs")
    champion = audits["B-ALL"]["campaigns"]
    context = campaign_context(champion, bars, features, observed["B-ALL"])
    fast = [row for row in context if row["family"] == 2]
    if len(fast) != 49 or round(sum(row["net_cash"] for row in fast), 3) != -257.245:
        raise ValueError("FAST original count or economics differ")
    setups = [row["setup"] for row in fast]
    runs, journals, fast_trials, blocked = {}, {}, {}, {}

    def anchored(key, setup_rows, variant, source_bars=bars, source_features=features, **cost_stress):
        result = replay(source_bars, source_features, setup_rows, variant, **cost_stress)
        runs[key] = summarize(result["plans"], result["paid_cash"], result["refusals"],
                              cash_events=result["cash_events"])
        runs[key].update(engine="ANCHORED_CONSERVATIVE_OHLC", rule=EXIT_RULES[variant],
                         market_window=[source_bars[0]["time"], source_bars[-1]["time_close"]],
                         fee_multiplier=cost_stress.get("fee_multiplier", 1),
                         extra_adverse_ticks=cost_stress.get("extra_ticks", 0))
        journals[key] = {"plans": result["plans"], "admission_refusals": result["refusals"]}

    for trial in registration["fast_trials"]:
        selected, excluded, choices = [], [], []
        for row in fast:
            setup = row["setup"]
            if trial in {"F2", "F4"}:
                proposed, reason = delayed_confirmation(bars, features, setup, trial)
                choices.append({"id": setup.id, "reason": reason,
                                "confirmed_at": bars[proposed.index]["time_close"] if proposed else None})
                if proposed is not None:
                    selected.append(proposed)
                continue
            passed = trial == "F11" or gate(row, bars, features, trial)
            if passed:
                selected.append(setup)
            else:
                excluded.append(row)
        anchored(trial, selected, "INVALIDATION" if trial == "F11" else "BASE")
        fast_trials[trial] = {
            "rule": registration["fast_trials"][trial], "selected_original_ids": [s.id for s in selected],
            "excluded_original_ids": ([row["id"] for row in excluded] if trial not in {"F2", "F4"} else
                                      [row["id"] for row in choices if row["confirmed_at"] is None]),
            "native_kept": (performance([row for row in fast if row["id"] in {s.id for s in selected}])
                            if trial not in {"F2", "F4", "F11"} else None),
            "native_excluded": (performance(excluded) if trial not in {"F2", "F4", "F11"} else None),
            "delayed_observations": choices,
            "native_subset_only": trial not in {"F2", "F4", "F11"},
        }
    for variant in registration["exit_trials"]:
        if variant not in {"BASE", "INVALIDATION"}:
            anchored("EXIT_" + variant, setups, variant)
    anchored("F0_COST_STRESS", setups, "BASE", fee_multiplier=1.5, extra_ticks=1)

    for key in ([f"S{i}" for i in range(1, 11)] + [f"S{i}_COST_STRESS" for i in (3, 5, 8)]):
        family = key.split("_")[0]
        stressed = key.endswith("_COST_STRESS")
        config = BacktestConfig(enabled_families=(family,),
                                commission_per_unit=.175 * (1.5 if stressed else 1),
                                slippage_ticks=3 if stressed else 2)
        engine = run_backtest(bars, config)
        if engine["blocked_families"]:
            blocked[key] = engine["blocked_families"]
        plans = engine_journal(engine)
        runs[key] = summarize(plans, engine["summary"]["cash_paid_net"], [])
        runs[key].update(engine="FROZEN_RESEARCH_ENGINE", config=engine["config"],
                         rejections=engine["summary"]["rejection_reasons"],
                         market_window=[bars[0]["time"], bars[-1]["time_close"]])
        journals[key] = {"plans": plans}

    data = {"archived_M15": dataset_record(DEFAULT_DATA, bars)}
    structural = {
        "rule_count": len(RULES), "rules": RULES,
        "feature_event_counts": {name: sum(bool(f[name]) for f in features)
                                 for name in ("bos", "choch", "mss", "sweep_up", "sweep_down", "causal_fvg",
                                              "order_block_proxy", "asian_range_utc", "repeated_sweep_up",
                                              "repeated_sweep_down")},
        "candidate_rejections": {},
    }
    generated, refusals = discover(bars, features)
    structural["candidate_rejections"]["archived_M15"] = refusals
    for key, setup_rows in generated.items():
        anchored(key, setup_rows, "BASE")
    source_registry = read_json(ROOT / scope["common_window"]["registry"])
    window = source_registry["common_window"]
    common_start, common_end = window["start"], window["end"]
    for source in source_registry["sources"]:
        tf = source["timeframe"]
        path = ROOT / source["local_csv"]
        if not path.is_file():
            blocked[f"COMMON_M{tf}"] = "LOCAL_OBSERVED_EXPORT_UNAVAILABLE"
            continue
        if sha(path) != scope["common_window"]["dataset_sha256"][tf]:
            raise ValueError(f"M{tf} export does not match registration")
        observed_bars = load_bars(path)
        data[f"actual_M{tf}"] = dataset_record(path, observed_bars)
        series = [b for b in observed_bars if b["time_close"] <= common_end]
        fseries = structural_features(series)
        generated, _ = discover(series, fseries)
        for key, setups_all in generated.items():
            in_window = [s for s in setups_all if common_start <= series[s.index]["time_close"] < common_end]
            run_key = f"M{tf}_COMMON_{key}"
            anchored(run_key, in_window, "BASE", series, fseries)
            runs[run_key]["admission_window"] = [common_start, common_end]

    distinct = list(runs)
    adjusted = holm([runs[key]["one_sided_sign_flip_net_r_p"] for key in distinct])
    for key, p in zip(distinct, adjusted):
        runs[key]["holm_p"] = p
    results = {
        "native_verified": native_summary, "fast_original": [
            {key: value for key, value in row.items() if key != "setup"} for row in fast],
        "native_family_in_ALL": {name: performance([row for row in context if row["family"] == family])
                                for family, name in ((1, "RETEST"), (2, "FAST"), (3, "REVERSAL"))},
        "fast_failure_patterns": loss_patterns(fast), "fast_trials": fast_trials,
        "historical_runs": runs, "blocked": blocked, "holm_distinct_trial_count": len(distinct),
        "baseline_equal_native": False, "inference": "Historical exploratory results. No promotion.",
        "limitations": ["Only originally submitted FAST universe is available for anchored replay.",
                        "Cohort filters keep original fills, fees and outcomes for accepted positions.",
                        "Neither Python engine has native broker-emulator parity.",
                        "No genuine independent holdout, bid/ask, spread history or broker execution.",
                        "Sign-flip assumes sign symmetry; Holm is descriptive on already-observed data."],
    }
    models = research_models(context, champion)
    return results, models, structural, journals, data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="new output directory")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("choose a new output directory")
    started = datetime.now(timezone.utc).isoformat()
    results, models, structural, journals, datasets = execute()
    args.output.mkdir(parents=True, exist_ok=False)
    outputs = {
        "phase2_results.json": results,
        "phase3_models.json": models,
        "phase3_expected_r.json": expected_r_report(models["events"]),
        "phase4_structure.json": structural,
    }
    for name, obj in outputs.items():
        (args.output / name).write_text(json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
                                        encoding="utf-8", newline="\n")
    encoded = json.dumps(journals, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    (args.output / "sparse_campaign_journals.json.gz").write_bytes(gzip.compress(encoded, mtime=0))
    paths = sorted(p for p in HERE.iterdir() if p.suffix in {".py", ".json"} and p.is_file())
    frozen_paths = [ROOT / p for p in ("research/backtesting/engine.py", "research/features/causal.py",
                                     "research/strategies/library.py", "research/data/loader.py",
                                     "research/validation/native_report.py", "research/validation/statistics.py",
                                     "research/models/logistic.py")]
    manifest = {
        "schema": "qirvanta.ultimate.research_run.v1", "started_utc": started,
        "finished_utc": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
        "base_commit": read_json(HERE / "registration.json")["source_commit"],
        "sha256_inputs": {p.relative_to(ROOT).as_posix(): sha(p) for p in paths + frozen_paths},
        "sha256_outputs": {p.name: sha(p) for p in sorted(args.output.iterdir())},
        "datasets": datasets, "costs": COSTS, "distinct_runs": results["holm_distinct_trial_count"],
        "provenance": "Public export contains native context summaries, sparse simulated plans, outcome labels and metrics only.",
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps({"historical_runs": manifest["distinct_runs"], "native": results["native_verified"]["B-ALL"],
                      "model_targets": list(models["objectives"]), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
