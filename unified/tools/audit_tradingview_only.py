"""Reconcile archived native evidence and run the TradingView-only FAST study."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path

from research.data.loader import DEFAULT_DATA, load_bars
from research.ultimate.fast import campaign_context, delayed_confirmation, performance, verify_native
from research.ultimate.replay import COSTS, replay
from research.ultimate.run import summarize
from research.ultimate.structure import structural_features
from research.validation.protocol import ROOT, sha
from research.validation.statistics import holm


REGISTRATION = ROOT / "docs/tradingview_only_evidence/fast_archive_registration.json"
ARCHIVES = {
    "5": "docs/platform_evidence/M5_native.audit.json",
    "15": "docs/platform_evidence/ui_parity.audit.json",
    "30": "docs/platform_evidence/M30_native.audit.json",
}
PRODUCTION = ROOT / "unified/QIRVANTA_UNIFIED.pine"


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def utc(stamp):
    return datetime.fromtimestamp(stamp / 1000, timezone.utc)


def sl_before_tp1(campaigns):
    strict, same_bar = 0, 0
    for campaign in campaigns:
        first_sl = min((row["exit_bar_index"] for row in campaign["exit_rows"] if row["exit_comment"] == "SL"),
                       default=None)
        first_tp = min((row["exit_bar_index"] for row in campaign["exit_rows"] if row["exit_comment"] == "TP1"),
                       default=None)
        if first_sl is not None:
            strict += first_tp is None or first_sl < first_tp
            same_bar += first_tp is not None and first_sl == first_tp
    return {"strict_count": strict, "mixed_same_bar": same_bar, "denominator": len(campaigns)}


def archives():
    production_sha = sha(PRODUCTION)
    common = read(ROOT / "research/reports/actual_tf_results.json")
    output = {}
    for tf, relative in ARCHIVES.items():
        path = ROOT / relative
        audit = read(path)
        rows = verify_native(audit)
        run = audit["run"]
        if (run["symbol"], run["timeframe"], run["source_sha256"], run["mintick"], run["pointvalue"]) != (
                "OANDA:XAUUSD", tf, production_sha, .001, 1.):
            raise ValueError(f"archived M{tf} provenance differs")
        native_cohort = common["native_cohorts"][tf]
        ids = set(native_cohort["complete_cohort_ids"])
        cohort = [r for r in rows if r["id"] in ids]
        if len(cohort) != len(ids):
            raise ValueError("missing archived cohort campaign")
        output[tf] = {
            "journal": relative, "journal_sha256": sha(path), "source_sha256": production_sha,
            "bar_count": run["bar_count"], "first_open_utc": utc(run["first_bar_time"]).isoformat(),
            "last_close_utc": utc(run["cutoff"]).isoformat(),
            "full_archived_metrics": {**performance(rows), "parts": sum(r["parts"] for r in rows),
                                      "fees_usd": round(sum(r["fees"] for r in rows), 6),
                                      "native_equity_dd_usd": audit["native_equity_max_drawdown"]["value"],
                                      "sl_before_tp1": sl_before_tp1(audit["campaigns"])},
            "common_entry_cohort": {**performance(cohort), "native_equity_dd_usd": None,
                                    "prewindow_carryover": native_cohort["prewindow_carryover_ids"],
                                    "end_censored": native_cohort["end_censored_ids"]},
        }
    f1_path = ROOT / "docs/ultimate_evidence/native_f1_comparison.json"
    f1 = read(f1_path)
    if not (f1["identical_bars_off_on_archived"] and f1["off_campaign_objects_equal_to_archived_except_run_id"]):
        raise ValueError("F1 native evidence does not match input cohort")
    return {
        "evidence_scope": "Prior actual TradingView exports, independently reconciled from archived fills in this run",
        "timeframes": output,
        "common_entry_window_utc": [utc(common["common_window"][key]).isoformat() for key in ("start", "end")],
        "common_scope": "Entry cohorts from already-run native accounts, not new flat-start runs",
        "f1_native_ab": {"report": f1_path.relative_to(ROOT).as_posix(), "sha256": sha(f1_path),
                         "source_sha256": f1["experimental_sha256"], "net_usd_change": f1["net_usd_change"],
                         "coverage": {key: {k: val[k] for k in ("campaign_metrics", "native_parts",
                                                               "native_equity_max_drawdown")}
                                      for key, val in f1["coverage"].items()}},
    }


def keeps(row, bars, features, trial, max_extension=.25):
    s = row["setup"]
    side, i = s.side, s.index
    current, previous = features[i], features[i - 1]
    stacked = current["h1"]["bias"] == current["h4"]["bias"] == side
    regime = "UPTREND" if side == 1 else "DOWNTREND"
    fresh = row["four_bar_signed_extension_atr"] <= max_extension
    target1 = row["target_rr_planned"][0]
    room = row["obstacle_room_r"]
    body = row["directional_body_fraction"]
    return {
        "F0": True, "F1": body is not None and body >= .55,
        "F3": stacked,
        "F5": room is not None and room >= max(1, target1),
        "F6": current["regime"] == previous["regime"] == regime and not current["gap"],
        "F7": fresh,
        "F8": row["directional_rsi"] >= 5 and (s.reference - bars[i - 2]["close"]) * side >= .25 * s.atr,
        "F9": row["session"] in {"LONDON", "NEW_YORK", "LONDON+NEW_YORK"},
        "F10": stacked and fresh,
    }[trial]


def research():
    registration = read(REGISTRATION)
    path = ROOT / "diagnostics/accounting/B-FAST.audit.json"
    if sha(path) != registration["standalone_native_fast_sha256"] or sha(DEFAULT_DATA) != registration["bar_sha256"]:
        raise ValueError("FAST archive hashes differ")
    if registration["cost_assumptions"] != COSTS:
        raise ValueError("FAST cost registration differs")
    native = read(path)
    originals = verify_native(native)
    if len(originals) != 50 or round(sum(x["net_cash"] for x in originals), 3) != -243.310:
        raise ValueError("original standalone FAST did not reconcile")
    bars = load_bars()
    features = structural_features(bars)
    rows = campaign_context(native["campaigns"], bars, features, originals)
    variants = [(name, name, .25, 1., 0) for name in registration["hypotheses"]]
    variants += [("F7_EXTENSION_020", "F7", .20, 1., 0), ("F7_EXTENSION_030", "F7", .30, 1., 0)]
    variants += [(name + "_COST_STRESS", name, .25, 1.5, 1) for name in ("F0", "F7", "F9")]
    runs = {}
    for name, rule, extension, multiplier, ticks in variants:
        accepted, rejected, setups, decisions = [], [], [], []
        for row in rows:
            setup = row["setup"]
            if rule in ("F2", "F4"):
                setup, reason = delayed_confirmation(bars, features, setup, rule)
                if setup is not None:
                    risk = (setup.reference - setup.stop) * setup.side
                    cost = 2 * COSTS["fee_per_unit_per_fill"] + 2 * COSTS["market_stop_slippage_ticks"] * COSTS["tick"]
                    if not .8 * setup.atr <= risk <= 2.4 * setup.atr or cost / risk > .2:
                        setup, reason = None, "RESEARCH_DELAYED_RISK_SCREEN"
            else:
                setup = setup if keeps(row, bars, features, rule, extension) else None
                reason = "FIXED_GATE_ACCEPTED" if setup is not None else "FIXED_GATE_REJECTED"
            decisions.append({"id": row["id"], "reason": reason,
                              "confirmed_at": bars[setup.index]["time_close"] if setup is not None else None})
            if setup is not None:
                accepted.append(row)
                setups.append(setup)
            else:
                rejected.append(row)

        result = replay(bars, features, setups, fee_multiplier=multiplier, extra_ticks=ticks)
        summary = summarize(result["plans"], result["paid_cash"], result["refusals"],
                            cash_events=result["cash_events"])
        months = sorted({utc(row["signal_at"]).strftime("%Y-%m") for row in rows})
        runs[name] = {
            "rule": registration["hypotheses"][rule], "cost_multiplier": multiplier, "extra_ticks": ticks,
            "admitted_setup_count": len(setups),
            "accepted_original": performance(accepted), "removed_original": performance(rejected),
            "unchanged_fill_cohort_applicable": rule not in ("F2", "F4") and multiplier == 1.,
            "historical_utc_signal_months_original_fill_attribution": {
                month: performance([row for row in accepted if utc(row["signal_at"]).strftime("%Y-%m") == month])
                for month in months
            },
            "anchored_ohlc_replay": summary, "decisions": decisions,
            "replay_campaign_outcomes": [{"id": p["id"], "state": p["state"], "net_cash": p["net_cash"],
                                           "net_r": p["net_r"], "paid_cash": p["paid_cash"],
                                           "part_count": len(p["exits"]),
                                           "exit_reasons": [x["reason"] for x in p["exits"]],
                                           "ambiguous": p["ambiguous"]} for p in result["plans"]],
        }
    adjusted = holm([r["anchored_ohlc_replay"]["one_sided_sign_flip_net_r_p"] for r in runs.values()])
    for result, probability in zip(runs.values(), adjusted):
        result["anchored_ohlc_replay"]["holm_p"] = probability
    return {"original_native_fast": {**performance(originals), "parts": sum(x["parts"] for x in originals),
                                     "native_equity_dd_usd": native["native_equity_max_drawdown"]["value"],
                                     "sl_before_tp1": sl_before_tp1(native["campaigns"])},
            "registration_sha256": sha(REGISTRATION),
            "observations": [{"id": row["id"], "signal_at": row["signal_at"], "side": row["side"],
                              "native_net_cash": row["net_cash"], "native_net_r": row["net_r"]}
                             for row in rows],
            "trials": runs, "promotion": False,
            "scope": "Archived selected-cohort attribution plus conservative Python replay. F3/F5/F6/F9 are explicit proxies."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("choose a new evidence file")
    result = {"schema": "qirvanta.tradingview-only.archive-analysis.v1",
              "created_utc": datetime.now(timezone.utc).isoformat(),
              "tool_sha256": sha(Path(__file__)), "native_archives": archives(),
              "fast_research": research(), "candidate_native_runtime_evidence": False}
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    print(json.dumps({"native_campaigns": sum(t["full_archived_metrics"]["n"] for t in
                                             result["native_archives"]["timeframes"].values()),
                      "fast_trials": len(result["fast_research"]["trials"]),
                      "output": str(args.output)}))


if __name__ == "__main__":
    main()
