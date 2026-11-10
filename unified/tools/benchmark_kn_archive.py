"""Run the registered common OHLC accounting protocol on two observed signal streams."""
import argparse
from collections import Counter
from datetime import datetime
from math import isclose
from pathlib import Path
import json

from research.backtesting import engine
from research.data.loader import DEFAULT_DATA, ROOT, load_bars
from research.strategies.library import stop_round
from research.ultimate.fast import performance, verify_native
from research.ultimate.run import completed, dataset_record, engine_journal, summarize
from research.validation.protocol import sha


REGISTRATION = ROOT / "docs/tradingview_only_evidence/kn_benchmark_registration.json"


def decision(signal_id, side, reference, stop):
    return {"signal_id": signal_id, "side": side, "reference": reference,
            "initial_stop": stop, "family": "S1", "status": "CONFIRMED"}


def kn_proposals(bars, kn, warmup, tick):
    proposals = {}
    fast_alpha = 2 / (kn["fast_ema"] + 1)
    slow_alpha = 2 / (kn["slow_ema"] + 1)
    length = kn["atr_length"]
    fast = slow = previous_close = bars[0]["close"]
    atr_seed = 0.
    atr = None
    for i, bar in enumerate(bars):
        close = bar["close"]
        old_fast, old_slow = fast, slow
        if i:
            fast = fast_alpha * close + (1 - fast_alpha) * fast
            slow = slow_alpha * close + (1 - slow_alpha) * slow
        true_range = bar["high"] - bar["low"]
        if i:
            true_range = max(true_range, abs(bar["high"] - previous_close),
                             abs(bar["low"] - previous_close))
        previous_close = close
        if i < length:
            atr_seed += true_range
            if i == length - 1:
                atr = atr_seed / length
        else:
            atr = ((length - 1) * atr + true_range) / length
        if i < warmup or atr is None:
            continue
        if fast > slow and old_fast <= old_slow:
            side = 1
        elif fast < slow and old_fast >= old_slow:
            side = -1
        else:
            continue
        stop = stop_round(close - side * kn["atr_stop_multiple"] * atr, side, tick)
        proposals[i] = decision(f"KN|{side}|{bar['time_close']}", side, close, stop)
    return proposals


def champion_proposals(bars, audit, warmup):
    verify_native(audit)
    proposals = {}
    for campaign in audit["campaigns"]:
        meta = campaign["submitted_metadata"]
        i = meta["submitted_bar"]
        side, reference, stop = campaign["side"], meta["reference"], meta["initial_sl"]
        if (i < warmup or i in proposals or bars[i]["time_close"] != meta["submitted_time"]
                or not isclose(bars[i]["close"], reference, abs_tol=.00051)
                or side not in (-1, 1) or (reference - stop) * side <= 0):
            raise ValueError("Pinned B-ALL proposal could not be matched to its archived decision bar")
        proposals[i] = decision(campaign["plan_id"], side, reference, stop)
    return proposals


def run_common(bars, proposals, cfg):
    def on_close(i, _, __):
        return [proposals[i]] if i in proposals else []

    result = engine.run_backtest(bars, cfg, candidate_provider=on_close)
    journal = engine_journal(result)
    rejected = [{"signal_id": row["signal_id"], "time": row["confirmation_time"],
                 "side": row["side"], "reasons": row["reasons"]}
                for row in result["decisions"] if row["status"] == "REJECTED"]
    refusals = [{"reason": reason} for row in rejected for reason in row["reasons"]]
    totals = result["summary"]
    if (len(proposals) != len(journal) + len(rejected) or
            not isclose(totals["closed_parts_net"] - totals["unallocated_entry_fees"],
                        totals["cash_paid_net"], abs_tol=1e-7)):
        raise ValueError("Proposal or cash reconciliation failed")

    def sl_first(plan):
        return plan["state"] == "COMPLETE" and bool(plan["exits"]) and all(
            part["reason"] in {"SL", "SL_GAP"} for part in plan["exits"])

    summary = summarize(journal, totals["cash_paid_net"], refusals,
                        cash_events=[(event["time"], event["cash_change"])
                                     for event in result["equity_cash_paid"]])
    summary.update(
        proposals=len(proposals), submitted=len(journal), rejected=len(rejected),
        sl_before_tp1=sum(map(sl_first, journal)),
        gap_invalid_plans=sum(p.get("gap_invalid", False) for p in result["plans"]),
        net_r_sum=totals["net_r"],
        closed_parts_net=totals["closed_parts_net"],
        commission_paid=sum(p["fees_paid"] for p in result["plans"]),
        unallocated_entry_fees=totals["unallocated_entry_fees"],
        cash_paid_drawdown=totals["cash_paid_drawdown"],
        cash_drawdown_scope=totals["cash_scope"],
        part_exit_reasons=dict(Counter(part["reason"] for p in journal for part in p["exits"])),
        by_side={name: performance(completed([p for p in journal if p["side"] == side]))
                 for name, side in (("BUY", 1), ("SELL", -1))},
    )
    # Keep individual outcomes and execution chronology, without copying dense price data.
    sparse_journal = [{**{key: p[key] for key in (
        "id", "side", "state", "decision_time", "entry_time", "final_exit_time", "holding_bars",
        "remaining_quantity", "ambiguous", "initial_risk_cash", "net_cash", "net_r", "paid_entry_fee")},
        "exits": [{key: x[key] for key in ("id", "time", "size", "reason", "net", "fees", "precision")}
                  for x in p["exits"]]} for p in journal]
    return {"summary": summary, "journal": sparse_journal, "refusals": rejected}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kn-source", type=Path, required=True, help="Local read-only source snapshot")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    registration = json.loads(REGISTRATION.read_text(encoding="utf-8"))
    archive, kn = registration["archive"], registration["kn"]
    audit_path = ROOT / archive["champion_audit"]
    if args.output.resolve() in {p.resolve() for p in (args.kn_source, REGISTRATION, audit_path, DEFAULT_DATA)}:
        parser.error("Output must not overwrite an input")
    if sha(args.kn_source) != kn["read_only_snapshot_sha256"]:
        raise ValueError("KN read-only source SHA does not match")
    if sha(audit_path) != archive["champion_audit_sha256"] or sha(DEFAULT_DATA) != archive["bars_sha256"]:
        raise ValueError("Pinned archive SHA does not match")
    bars = load_bars()
    dataset = dataset_record(DEFAULT_DATA, bars)
    first_open, last_close = (
        int(datetime.fromisoformat(archive[key]).timestamp() * 1000)
        for key in ("first_open_utc", "last_close_utc")
    )
    if (len(bars), dataset["interval_minutes"], dataset["first_open"], dataset["last_close"]) != (
            archive["bars"], archive["interval_minutes"], first_open, last_close):
        raise ValueError("Archive scope does not match registration")
    config = dict(registration["engine"])
    del config["implementation"]
    config["target_rs"] = tuple(config["target_rs"])
    cfg = engine.BacktestConfig(**config)
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    evidence = {
        "schema": "qirvanta.kn-normalized-archive.evidence.v1",
        "classification": registration["classification"],
        "status": "NORMALIZED_OFFLINE_COMPARISON_COMPLETE",
        "native_exact_strategy_comparison": "UNVERIFIED",
        "kn_source_copied_to_evidence": False,
        "kn_read_only_source_sha256": kn["read_only_snapshot_sha256"],
        "registration_sha256": sha(REGISTRATION),
        "tool_sha256": sha(Path(__file__)),
        "engine_sha256": sha(Path(engine.__file__)),
        "champion_audit_sha256": archive["champion_audit_sha256"],
        "dataset": dataset, "config": config,
        "replay_limitation": "Both use real observed bars and the same custom candidate-provider hook. "
                             "Champion uses already-admitted B-ALL proposals only; KN uses every qualifying crossover. "
                             "No intrabar or native order-emulator parity.",
        "models": {
            "QIRVANTA_ARCHIVED_ADMITTED_PROPOSALS": run_common(
                bars, champion_proposals(bars, audit, archive["warmup_bars"]), cfg),
            "KN_CLOSED_BAR_CROSSOVERS": run_common(
                bars, kn_proposals(bars, kn, archive["warmup_bars"], cfg.mintick), cfg),
        },
    }
    args.output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
                           encoding="utf-8")
    print(json.dumps({name: row["summary"] for name, row in evidence["models"].items()}, allow_nan=False))


if __name__ == "__main__":
    main()
