"""Audit S5 protection and allocation on preserved native fills and matched OHLC."""

import argparse
from collections import Counter
import csv
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import gzip
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from diagnostics.accounting.audit import audit, json_default, read_jsonl, stats
from unified.tools.audit_tv_observations import ALIGNMENT_FIELDS, validate_execution
from unified.tools.build_sniper_premium import BASELINE_SHA, ROOT, sha


D = Decimal
FRAMES = {"5": "M5_dataset", "15": "ui_dataset", "30": "M30_dataset"}
ALLOCATIONS = {
    "equal": (D(1), D(1), D(1)),
    "50/30/20": (D("1.5"), D("0.9"), D("0.6")),
    "33/33/34": (D("0.99"), D("0.99"), D("1.02")),
}
BRACKETS = {"T1": 0, "T2": 1, "T3": 2}
EVIDENCE = ROOT / "docs/premium_evidence"
ARCHIVE = ROOT / "docs/tradingview_only_evidence"
PAIR = EVIDENCE / "native_pair_20261110_1200Z"
PROFIT_LOCK_R = D("0.05")
ROUND_TRIP_FEE = D("0.350")
SLIPPAGE_TICKS = 2


def mean(values):
    return sum(values) / len(values) if values else None


def bracket(row):
    return BRACKETS.get(row["exit_id"].rsplit("|", 1)[-1])


def resized_net(campaign, quantities):
    pending = set(range(3))
    cash = D(0)
    for row in sorted(campaign["exit_rows"], key=lambda item: item["trade_index"]):
        part = bracket(row)
        if part is None:
            size = sum(quantities[index] for index in pending)
            if (len(pending) != abs(row["size"])
                    or row["exit_comment"] not in ("ИЗХОД СЛЕД ГАП", "ИЗТЕКЪЛ СРОК")):
                raise ValueError(f"Market exit did not close the remaining units: {campaign['entry_id']}")
            pending.clear()
        else:
            if part not in pending or abs(row["size"]) != 1:
                raise ValueError(f"Unexpected bracket size or repeat: {campaign['entry_id']}")
            size = quantities[part]
            pending.remove(part)
        cash += row["net_cash"] * size / abs(row["size"])
    if pending:
        raise ValueError(f"Incomplete allocation tape: {campaign['entry_id']}")
    return cash


def allocations(campaigns):
    result = {}
    for name, quantities in ALLOCATIONS.items():
        cash = [resized_net(c, quantities) for c in campaigns]
        if name == "equal" and cash != [c["realized_net_cash"] for c in campaigns]:
            raise ValueError("Equal allocation must reproduce every observed campaign")
        returns = [p / c["initial_risk_cash"] for p, c in zip(cash, campaigns) if c["initial_risk_cash"]]
        result[name] = {"quantities": quantities, **stats(cash), "mean_net_r": mean(returns),
                        "scored_campaigns": len(returns),
                        "total_round_trip_commission_usd": ROUND_TRIP_FEE * sum(quantities) * len(campaigns)}
    return result


def round_stop(price, side, tick):
    method = ROUND_FLOOR if side == 1 else ROUND_CEILING
    return (price / tick).to_integral_value(rounding=method) * tick


def lock_price(entry, stop, side, tick):
    risk = (entry - stop) * side
    level = round_stop(entry + side * risk * PROFIT_LOCK_R, side, tick)
    return max(stop, level) if side == 1 else min(stop, level)


def observed_protection(campaigns, tick):
    counts = Counter()
    cash = []
    for c in campaigns:
        rows = c["exit_rows"]
        tp1 = next((row for row in rows if row["exit_comment"] == "TP1"), None)
        if tp1 is None:
            continue
        counts["tp1_campaigns"] += 1
        post = [row for row in rows if row["exit_bar_index"] > tp1["exit_bar_index"]]
        counts["remainder_beyond_tp1_bar_campaigns"] += bool(post)
        sl = [row for row in post if row["exit_comment"] == "SL"]
        if not sl:
            continue
        counts["campaigns_with_post_tp1_sl"] += 1
        side, entry = c["side"], c["actual_entry"]
        initial = c["submitted_metadata"]["initial_sl"]
        if (entry - initial) * side <= 0:
            counts["campaigns_with_invalid_initial_risk"] += 1
            continue
        expected = lock_price(entry, initial, side, tick) - side * SLIPPAGE_TICKS * tick
        for row in sl:
            cash.append(row["net_cash"])
            counts["post_tp1_sl_rows"] += 1
            counts["sl_rows_exact_nominal_lock_after_slippage"] += row["exit_price"] == expected
            counts["sl_rows_more_adverse_than_nominal_lock"] += (row["exit_price"] - expected) * side < 0
            counts["sl_rows_price_favorable_but_cash_negative"] += (
                (row["exit_price"] - entry) * side > 0 and row["net_cash"] < 0)
            counts["sl_rows_cash_negative"] += row["net_cash"] < 0
    return {"counts": dict(counts), "observed_post_tp1_sl_net_cash_usd": sum(cash, D(0)),
            "scope": "Actual SL rows strictly after the native TP1 bar; nominal model comparison, not a stop-movement log."}


def load_bars(folder, expected):
    manifest = json.loads((folder / "dataset_manifest.json").read_bytes())
    raw = (folder / "bars.csv").read_bytes()
    if sha(raw) != manifest["canonical_csv"]["sha256"]:
        raise ValueError(f"OHLC bytes differ from their manifest: {folder}")
    for field in ALIGNMENT_FIELDS:
        if manifest["run_metadata"][field] != expected[field]:
            raise ValueError(f"OHLC fingerprint does not match archived S5: {field}")
    bars = []
    for row in csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))):
        bars.append((int(row["time"]), int(row["time_close"]),
                     *(D(row[key]) for key in ("open", "high", "low", "close"))))
    if (len(bars) != expected["bar_count"] or bars[0][0] != expected["first_bar_time"]
            or bars[-1][:2] != (expected["last_bar_open"], expected["cutoff"])):
        raise ValueError(f"OHLC coverage does not match archived S5: {folder}")
    return bars, sha(raw)


def threshold_observations(campaigns, bars, tick, pointvalue):
    counts = {name: Counter() for name in ("initial_sl", "gross_be", "native_005R_lock", "cost_be")}
    examples = []
    for c in campaigns:
        rows = c["parts"]
        tp1 = next((row for row in rows if row["exit_comment"] == "TP1"), None)
        if tp1 is None:
            continue
        first = tp1["exit_bar_index"]
        last = max(row["exit_bar_index"] for row in rows)
        if not 0 <= first <= last < len(bars) or any(
            bars[row["exit_bar_index"]][0] != row["exit_time"] for row in rows
        ):
            raise ValueError(f"Native exit bar/time does not align: {c['entry_id']}")
        if first == last:
            continue
        side, entry, initial = c["side"], c["actual_entry"], c["submitted"]["initial_sl"]
        target_bar = min((row["exit_bar_index"] for row in rows
                          if row["exit_comment"] in ("TP2", "TP3") and row["exit_bar_index"] > first),
                         default=None)
        prices = {
            "initial_sl": initial,
            "gross_be": entry,
            "native_005R_lock": lock_price(entry, initial, side, tick),
            "cost_be": entry + side * (ROUND_TRIP_FEE / pointvalue + SLIPPAGE_TICKS * tick),
        }
        for name, price in prices.items():
            out = counts[name]
            out["post_tp1_cohort"] += 1
            if (bars[first][5] - price) * side < tick / 2:
                out["not_protective_at_known_tp1_close"] += 1
                continue
            out["eligible_at_known_tp1_close"] += 1
            for index in range(first + 1, last + 1):
                opening, high, low = bars[index][2:5]
                if ((low if side == 1 else high) - price) * side > 0:
                    continue
                out["first_touch_by_observed_close"] += 1
                out["first_touch_opens_at_or_beyond_level"] += (opening - price) * side <= 0
                if target_bar is not None:
                    out["touch_before_observed_next_tp"] += index < target_bar
                    out["touch_on_observed_next_tp_bar_order_unknown"] += index == target_bar
                if name == "cost_be" and target_bar is not None and index <= target_bar and len(examples) < 3:
                    examples.append({"entry_id": c["entry_id"], "threshold": price,
                                     "touch_bar_open": bars[index][0], "observed_next_tp_bar_open": bars[target_bar][0]})
                break
    return {"counts": {name: dict(values) for name, values in counts.items()}, "examples": examples,
            "scope": "Only from the bar after known TP1 through observed campaign closure. "
            "Threshold touches do not simulate an order, resolve intrabar ordering, or project survival after closure."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset-root", required=True, type=Path, help="Directory with the three verified local OHLC datasets")
    parser.add_argument("--output", required=True, type=Path, help="New JSON report")
    args = parser.parse_args()
    pair = json.loads(PAIR.with_suffix(".json").read_bytes(), parse_float=D)
    matrix = json.loads((ARCHIVE / "native_matrix.json").read_bytes())
    old_archive = ARCHIVE / "native_campaigns.jsonl.gz"
    if sha(old_archive.read_bytes()) != matrix["ledger"]["sha256"]:
        raise ValueError("Older native ledger bytes differ from the published matrix")
    with gzip.open(old_archive, "rt", encoding="utf-8") as stream:
        old = [json.loads(line, parse_float=D) for line in stream]
    output = {}
    archive_path = PAIR.with_name(PAIR.name + "_logs.zip")
    with TemporaryDirectory() as temp, ZipFile(archive_path) as archive:
        for tf, dataset in FRAMES.items():
            cell = next(row["baseline"] for row in pair["cells"] if row["mode"] == "S5" and row["timeframe"] == tf)
            raw = archive.read(f"baseline_S5_M{tf}.csv")
            if sha(raw) != cell["log_sha256"]:
                raise ValueError(f"Premium S5 raw hash mismatch: M{tf}")
            path = Path(temp) / "log.csv"
            path.write_bytes(raw)
            result = audit(read_jsonl(path, allow_pine_prefix=True, origin_override="REAL_TRADINGVIEW")[0])
            run = result["run"]
            if (result["status"] != "PASS_OBSERVED_JOURNAL" or not result["cash_reconciliation"]["matches"]
                    or run["source_sha256"] != BASELINE_SHA or run["run_id"] != "tv_final|S5|Всички"
                    or any(run[key] != cell["data"][key] for key in ALIGNMENT_FIELDS)):
                raise ValueError(f"Unexpected S5 journal: M{tf}")
            validate_execution(result)
            campaigns = [c for c in result["campaigns"] if c["state"] == "COMPLETE"]
            expected = matrix["timeframes"][tf]["S5"]["run"]
            bars, bars_sha = load_bars(args.dataset_root / dataset, expected)
            older = [c for c in old if c.get("mode") == "S5" and c.get("timeframe") == tf and c.get("state") == "COMPLETE"]
            if len(older) != matrix["timeframes"][tf]["S5"]["campaign_metrics"]["count"]:
                raise ValueError(f"Older native cohort count does not match published S5: M{tf}")
            output[tf] = {
                "newer_native": {"cutoff": run["cutoff"], "log_sha256": sha(raw),
                                 "campaign_states": result["campaign_state_counts"],
                                 "equity_max_drawdown": result["native_equity_max_drawdown"],
                                 "observed_protection": observed_protection(campaigns, run["mintick"]),
                                 "fixed_tape_allocation_sensitivity": allocations(campaigns)},
                "older_native_plus_ohlc": {"cutoff": expected["cutoff"], "ohlc_sha256": bars_sha,
                                          "complete_campaigns": len(older),
                                          "threshold_observations": threshold_observations(older, bars, run["mintick"], run["pointvalue"])},
            }
    with args.output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump({"schema": "qirvanta.premium_v2.management_audit.v1",
                   "classification": "ARCHIVED_NATIVE_PLUS_OFFLINE_SENSITIVITY",
                   "variant_native_runs": 0, "quantity_grid_verified": False,
                   "native_archive_sha256": sha(archive_path.read_bytes()), "frames": output},
                  stream, ensure_ascii=False, indent=2, allow_nan=False, default=json_default)
        stream.write("\n")


if __name__ == "__main__":
    main()
