"""Descriptive attribution of reconciled native campaigns; no trading simulation."""
from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal, ROUND_FLOOR, ROUND_CEILING
import hashlib
import json
from pathlib import Path

D = Decimal
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
RUNS = ("B-ALL", "B-RETEST", "B-FAST", "B-REVERSAL")
SMALL_N = 20
EFFICIENCY_CUT = D("0.25")


def number(value):
    return None if value is None else D(str(value))


def expected_locked_exit(campaign, tick, lock_r=D("0.05"), slippage_ticks=2):
    entry = number(campaign["actual_entry"])
    initial = number(campaign["submitted_metadata"]["initial_sl"])
    side = D(campaign["side"])
    risk = (entry - initial) * side
    if risk <= 0:
        return None
    raw = entry + side * risk * lock_r
    rounded = (raw / tick + (D("1e-8") if side > 0 else -D("1e-8"))).to_integral_value(rounding=ROUND_FLOOR if side > 0 else ROUND_CEILING) * tick
    stop = max(initial, rounded) if side > 0 else min(initial, rounded)
    return stop - side * tick * slippage_ticks


def classify_exit(campaign, tick):
    rows = campaign["exit_rows"]
    comments = [r.get("exit_comment") or "" for r in rows]
    quantities = defaultdict(lambda: D(0))
    for row, comment in zip(rows, comments):
        quantities[comment or "NO_COMMENT"] += abs(number(row["size"]))
    total = sum(quantities.values(), D(0))
    if abs(total - number(campaign["closed_quantity"])) > D("1e-8"):
        raise ValueError("Exit quantity does not reconcile")
    tp = [r for r in rows if (r.get("exit_comment") or "") in ("TP1", "TP2", "TP3")]
    tp1 = [r for r in rows if r.get("exit_comment") == "TP1"]
    sl = [r for r in rows if r.get("exit_comment") == "SL"]
    expected = expected_locked_exit(campaign, tick)
    if any("СРОК" in c or c == "TIME" for c in comments):
        kind = "TIME"
    elif any("ГАП" in c or c == "GAP" for c in comments):
        kind = "GAP_LABEL"
    elif sl and len(sl) == len(rows):
        kind = "SL_FULL"
    elif tp1 and sl:
        first_tp1_bar = min(r["exit_bar_index"] for r in tp1)
        later = all(r["exit_bar_index"] > first_tp1_bar for r in sl)
        prices_match = expected is not None and all(abs(number(r["exit_price"]) - expected) <= D("1e-7") for r in sl)
        if later and prices_match:
            kind = "TP1_THEN_LOCKED_SL_CONSISTENT"
        elif any(r["exit_bar_index"] == first_tp1_bar for r in sl):
            kind = "TP_AND_SAMEBAR_SL"
        else:
            kind = "TP_THEN_OTHER_SL"
    elif tp and len(tp) == len(rows) and set(comments) == {"TP1", "TP2", "TP3"}:
        kind = "ALL_TARGETS"
    else:
        kind = "MIXED_OTHER"
    parts = []
    for row in sorted(rows, key=lambda r: (r["exit_time"], r["trade_index"])):
        parts.append({"trade_index": row["trade_index"], "comment": row.get("exit_comment"), "quantity": abs(number(row["size"])), "bar": row["exit_bar_index"], "time": row["exit_time"], "price": number(row["exit_price"]), "gross": number(row["gross_cash_from_prices"]), "commission": number(row["commission"]), "net": number(row["net_cash"])})
    return {"exit_class": kind, "quantity_by_exit_comment": dict(quantities), "expected_locked_exit_with_declared_slippage": expected, "parts": parts}


def diagnose(campaign, run):
    if campaign["state"] != "COMPLETE":
        raise ValueError("Only complete reconciled campaigns may be classified")
    p = campaign["submitted_metadata"]
    side = D(campaign["side"])
    entry, reference = number(campaign["actual_entry"]), number(p["reference"])
    deviation = (entry - reference) * side  # positive means less favorable entry
    atr = number(p["atr"])
    planned_risk = (reference - number(p["initial_sl"])) * side
    atr_ratio = deviation / atr if atr and atr > 0 else None
    r_ratio = deviation / planned_risk if planned_risk > 0 else None
    efficiency = number(p.get("efficiency"))
    row = {"plan_id": campaign["plan_id"], "family": campaign["family"], "side": campaign["side"], "trend_relation": campaign["trend_relation"], "original_regime": campaign["regime"], "original_countertrend": p.get("countertrend"), "local_aligned": p.get("local_aligned"), "efficiency": efficiency, "efficiency_bin": "MISSING" if efficiency is None else "LT_0.25" if efficiency < EFFICIENCY_CUT else "GE_0.25", "entry": entry, "reference": reference, "signal_atr": atr, "initial_planned_risk": planned_risk, "signed_deviation_price": deviation, "signed_deviation_atr": atr_ratio, "signed_deviation_planned_r": r_ratio, "abs_atr_deviation_bin": "MISSING" if atr_ratio is None else "LE_0.20" if abs(atr_ratio) <= D("0.20") else "GT_0.20", "signed_r_deviation_bin": "MISSING" if r_ratio is None else "LE_0" if r_ratio <= 0 else "GT_0_LE_0.10" if r_ratio <= D("0.10") else "GT_0.10", "quantity": number(campaign["closed_quantity"]), "gross": number(campaign["realized_gross_cash"]), "commission": number(campaign["realized_commission_cash"]), "net": number(campaign["realized_net_cash"]), "net_r": number(campaign["net_r"]), "initial_risk_cash": number(campaign["initial_risk_cash"]), "holding_bars": campaign["holding_bars"], "entry_time": campaign["entry_time"], "last_exit_time": campaign["last_exit_time"]}
    row.update(classify_exit(campaign, number(run["mintick"])))
    if abs(row["gross"] - row["commission"] - row["net"]) > D("1e-7"):
        raise ValueError("Cost decomposition does not reconcile")
    return row


def metrics(rows):
    n = len(rows)
    gross = sum((r["gross"] for r in rows), D(0))
    fee = sum((r["commission"] for r in rows), D(0))
    net = sum((r["net"] for r in rows), D(0))
    rs = [r["net_r"] for r in rows if r["net_r"] is not None]
    gains = sum((r["net"] for r in rows if r["net"] > 0), D(0))
    losses = -sum((r["net"] for r in rows if r["net"] < 0), D(0))
    return {"n": n, "small_n": n < SMALL_N, "quantity": sum((r["quantity"] for r in rows), D(0)), "wins": sum(r["net"] > 0 for r in rows), "losses": sum(r["net"] < 0 for r in rows), "gross": gross, "commission": fee, "net": net, "mean_net": net / n if n else None, "net_r_n": len(rs), "net_r": sum(rs, D(0)), "mean_net_r": sum(rs, D(0)) / len(rs) if rs else None, "profit_factor": gains / losses if losses else None, "gross_positive_but_net_nonpositive": sum(r["gross"] > 0 and r["net"] <= 0 for r in rows)}


def groups(rows, key):
    buckets = defaultdict(list)
    for row in rows:
        value = row[key]
        name = "MISSING" if value is None else str(value)
        buckets[name].append(row)
    return {name: metrics(values) for name, values in sorted(buckets.items())}


def distribution(values):
    values = sorted(v for v in values if v is not None)
    n = len(values)
    if not n:
        return {"n": 0}
    median = values[n // 2] if n % 2 else (values[n // 2 - 1] + values[n // 2]) / 2
    return {"n": n, "min": values[0], "median": median, "max": values[-1], "mean": sum(values, D(0)) / n}


def analyze_audit(audit):
    if audit["status"] != "PASS_OBSERVED_JOURNAL" or audit["issues"] or not audit["cash_reconciliation"]["matches"]:
        raise ValueError("Accounting must pass before diagnosis")
    rows = [diagnose(c, audit["run"]) for c in audit["campaigns"] if c["state"] == "COMPLETE"]
    m = metrics(rows)
    if abs(m["net"] - number(audit["complete_campaign_metrics"]["net_cash"])) > D("1e-7"):
        raise ValueError("Campaign contribution sum differs from accounting")
    dimensions = ("exit_class", "family", "efficiency_bin", "local_aligned", "original_countertrend", "trend_relation", "original_regime", "abs_atr_deviation_bin", "signed_r_deviation_bin")
    family = {}
    for f in sorted({r["family"] for r in rows}):
        selected = [r for r in rows if r["family"] == f]
        family[str(f)] = {"metrics": metrics(selected), "groups": {dim: groups(selected, dim) for dim in dimensions if dim != "family"}}
    return {"run": audit["run"], "metrics": m, "groups": {dim: groups(rows, dim) for dim in dimensions}, "family_contributions": family, "entry_deviation": {"signed_atr": distribution([r["signed_deviation_atr"] for r in rows]), "signed_planned_r": distribution([r["signed_deviation_planned_r"] for r in rows]), "absolute_atr": distribution([abs(r["signed_deviation_atr"]) if r["signed_deviation_atr"] is not None else None for r in rows])}, "campaigns": rows}


def json_default(value):
    if isinstance(value, D):
        return float(value)
    raise TypeError(type(value).__name__)


def main():
    plan = HERE / "analysis_plan.json"
    if not plan.exists():
        raise SystemExit("Register descriptive plan first")
    target = HERE / "descriptive_results.json"
    if target.exists():
        raise SystemExit("Refusing to overwrite descriptive results")
    result = {"scope": "DESCRIPTIVE_REAL_NATIVE_RECORDS_NOT_CAUSAL_OR_FILTERED_BACKTEST", "analysis_plan_sha256": hashlib.sha256(plan.read_bytes()).hexdigest(), "input_audit_sha256": {}, "runs": {}}
    for name in RUNS:
        path = REPO / "diagnostics/accounting" / f"{name}.audit.json"
        audit = json.loads(path.read_text(encoding="utf-8"), parse_float=D)
        result["input_audit_sha256"][name] = hashlib.sha256(path.read_bytes()).hexdigest()
        result["runs"][name] = analyze_audit(audit)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=json_default) + "\n", encoding="utf-8", newline="\n")
    for name, run in result["runs"].items():
        print(name, json.dumps({"metrics": run["metrics"], "exit": run["groups"]["exit_class"], "efficiency": run["groups"]["efficiency_bin"], "local": run["groups"]["local_aligned"], "htf": run["groups"]["trend_relation"], "deviation": run["entry_deviation"]}, ensure_ascii=False, default=json_default))


if __name__ == "__main__":
    main()
