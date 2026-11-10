"""Reconcile V2.1 impulse Pine Logs with the native strategy snapshot."""
import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "unified/experimental/QIRVANTA_SNIPER_V21_IMPULSE.pine"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(path):
    groups = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if len(row) < 2 or not row[1].startswith("V21|"):
                continue
            _, kind, stamp, *payload = row[1].split("|")
            groups.setdefault(kind, []).append((int(stamp), payload))
    snapshots = groups.get("SNAPSHOT", [])
    if len(snapshots) != 1:
        raise ValueError(f"{path}: expected exactly one native snapshot")
    known, payload = snapshots[0]
    (symbol, frame, first, last, bars, digest1, digest2, watches, confirmed, valid, rejected, expired,
     unfilled, nclosed, tv_net, drawdown, position, tick, point, orders_from, orders_until, cutoff) = payload
    if symbol != "OANDA:XAUUSD" or frame not in ("5", "15", "30"):
        raise ValueError(f"{path}: unexpected symbol or timeframe")
    if known != int(last) + int(frame) * 60_000 or known > int(cutoff):
        raise ValueError(f"{path}: invalid close or audit cutoff")
    groups = {key: [(at, p) for at, p in rows if at <= known] for key, rows in groups.items()}
    planned = {p[-1]: (at, float(p[5]), float(p[-5]), float(p[-4]))
               for at, p in groups.get("ENTRY_VALID", [])}
    closed = []
    for observed, p in groups.get("CLOSED", []):
        key, filled, exited, entry, exit_, qty, native_profit, fee, reason = p
        filled, exited = int(filled), int(exited)
        if key not in planned or planned[key][0] > filled or not filled <= exited <= observed <= known:
            raise ValueError(f"{path}: invalid known/fill/exit ordering for {key}")
        entry, exit_, qty, native_profit, fee = map(float, (entry, exit_, qty, native_profit, fee))
        gross = (exit_ - entry) * qty * float(point)
        net = gross - fee
        # TradingView profit accessor semantics are verified against observed fills.
        if min(abs(native_profit - gross), abs(native_profit - net)) > 0.02:
            raise ValueError(f"{path}: reported P&L does not match observed prices for {key}")
        plan_time, plan_close, sl, tp = planned[key]
        direction = 1 if qty > 0 else -1
        closed.append({"id": key, "known_plan": plan_time, "fill": filled, "exit": exited, "entry_price": entry,
                       "exit_price": exit_, "signed_qty": qty, "net_usd": round(net, 8), "fee": fee, "reason": reason,
                       "plan_close": plan_close, "initial_stop": sl, "target": tp,
                       "fill_risk_price": round((entry - sl) * direction, 8),
                       "fill_vs_plan_adverse_price": round((entry - plan_close) * direction, 8)})
    if len(closed) != int(nclosed) or abs(sum(r["net_usd"] for r in closed) - float(tv_net)) > 0.02:
        raise ValueError(f"{path}: missing rows or net profit mismatch; log the full calculation window")
    counts = {k: len(v) for k, v in groups.items()}
    for event, snapshot_count in (("WATCH", watches), ("CONFIRMED", confirmed), ("ENTRY_VALID", valid),
                                  ("ENTRY_REJECTED", rejected), ("EXPIRED", expired), ("NATIVE_UNFILLED", unfilled)):
        if counts.get(event, 0) != int(snapshot_count):
            raise ValueError(f"{path}: {event} log/snapshot mismatch")
    wins = sum(r["net_usd"] for r in closed if r["net_usd"] > 0)
    losses = sum(-r["net_usd"] for r in closed if r["net_usd"] < 0)
    return {
        "symbol": symbol, "frame": frame, "input_log_sha256": sha(path), "first_bar_open_ms": int(first),
        "last_bar_close_ms": known, "requested_cutoff_ms": int(cutoff), "bars": int(bars),
        "data_fingerprints": [int(digest1), int(digest2)], "counts": counts,
        "native_net_usd": float(tv_net), "native_max_drawdown_usd": float(drawdown), "position_at_cutoff": float(position),
        "order_window_ms": [int(orders_from), int(orders_until)],
        "closed_by_side": dict(Counter("BUY" if r["signed_qty"] > 0 else "SELL" for r in closed)),
        "close_reasons": dict(Counter(r["reason"] for r in closed)),
        "entry_reasons": dict(Counter(p[-1] for _, p in groups.get("ENTRY_REJECTED", []))),
        "win_count": sum(r["net_usd"] > 0 for r in closed), "profit_factor": wins / losses if losses else None,
        "native_fees_usd": round(sum(r["fee"] for r in closed), 8),
        "closed": closed,
        "case_nov10_decisions": sorted(
            ({"known_close_ms": at, "event": k, "values": p}
             for k, v in groups.items() if k in {"WATCH", "CONFIRMED", "ENTRY_VALID", "ENTRY_REJECTED", "EXPIRED"}
             for at, p in v if 1794268800000 <= at <= 1794322800000),
            key=lambda row: row["known_close_ms"],
        ),
    }


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--logs", type=Path, nargs="+", required=True)
    cli.add_argument("--out", type=Path, required=True)
    args = cli.parse_args()
    cells = [audit(p) for p in args.logs]
    if len({c["frame"] for c in cells}) != len(cells):
        raise ValueError("Duplicate timeframe")
    payload = {"status": "NATIVE_IMPULSE_ROWS_RECONCILED", "source_sha256": sha(SOURCE),
               "scope": "TradingView simulated fills; no separate spread or broker execution.", "cells": cells}
    with args.out.open("x", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, allow_nan=False, indent=2)
        f.write("\n")
    print(payload["status"], [c["frame"] for c in cells])


if __name__ == "__main__":
    main()
