"""Verify native V2.1 observations with the matching OHLC tape and causal prefix replay."""

import argparse
from collections import Counter
import csv
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from unified.tools.evaluate_v21_impulse import REGISTRATION, model_offer, occupy, scan


def read_log(path):
    records = []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            message = row["Message"]
            if message.startswith("V21|"):
                _, kind, known, *values = message.split("|")
                records.append((kind, int(known), values))
    snapshots = [(known, values) for kind, known, values in records if kind == "SNAPSHOT"]
    if len(snapshots) != 1:
        raise ValueError(f"{path.name}: expected exactly one snapshot")
    cutoff, snapshot = snapshots[0]
    return [(k, t, v) for k, t, v in records if t <= cutoff], cutoff, snapshot


def native_bars(path, records, snapshot):
    tick = Decimal(snapshot[17])
    bars = []
    d1 = d2 = 0
    for kind, close_time, values in records:
        if kind != "BAR":
            continue
        opened, *prices = values
        time = int(opened)
        if not time < close_time or (bars and bars[-1]["time_close"] > time):
            raise ValueError(f"{path.name}: out-of-order OHLC timestamps")
        o, h, l, c = (int((Decimal(p) / tick).to_integral_value()) for p in prices)
        stamp = time // 1000
        d1 = (31 * d1 + stamp + 3 * o + 5 * h + 7 * l + 11 * c) % 1_000_000_007
        d2 = (131 * d2 + stamp + 13 * o + 17 * h + 19 * l + 23 * c) % 1_000_000_009
        bar = dict(zip(("open", "high", "low", "close"), map(float, prices), strict=True))
        bars.append({"time": time, "time_close": close_time, **bar})
    expected = tuple(map(int, snapshot[2:7]))
    if not bars or (bars[0]["time"], bars[-1]["time"], len(bars), d1, d2) != expected:
        raise ValueError(f"{path.name}: OHLC rows do not match the complete native fingerprint")
    return bars, float(tick)


def side_text(direction):
    return "BUY" if direction > 0 else "SELL"


def compare_native(records, decisions):
    kinds = {"WATCH", "CONFIRMED", "EXPIRED"}
    expected = Counter((d.state, d.time, side_text(d.side)) for d in decisions if d.state in kinds)
    actual = Counter((kind, at, values[0]) for kind, at, values in records if kind in kinds)
    geometry = {(d.time, side_text(d.side)): d for d in decisions if d.state in {"RISK_VALID", "REJECTED"}}
    plans = {(t, v[0]): (k, v) for k, t, v in records if k in {"ENTRY_VALID", "ENTRY_REJECTED"}}
    differences = []
    for key in sorted(geometry.keys() | plans.keys()):
        expected_plan = geometry.get(key)
        actual_plan = plans.get(key)
        if expected_plan is None or actual_plan is None:
            differences.append({"key": key, "error": "missing decision"})
            continue
        kind, values = actual_plan
        mapping = {"OVEREXTENSION": "OVEREXTENDED", "STOP_TOO_SMALL": "RISK TOO NARROW",
                   "STOP_TOO_WIDE": "RISK TOO WIDE"}
        reasons = set(values[-1].split(", ")) if kind == "ENTRY_REJECTED" else set()
        expected_reasons = ({mapping[part] for part in expected_plan.reason.split("+")}
                            if expected_plan.state == "REJECTED" else set())
        if (abs(float(values[12]) - expected_plan.stop) > 1e-7
                or abs(float(values[13]) - expected_plan.target) > 1e-7
                or reasons.intersection(mapping.values()) != expected_reasons):
            differences.append({"key": key, "error": "geometry or risk gate mismatch"})
    return {
        "native_observations": sum(actual.values()),
        "native_plan_decisions": len(plans),
        "missing_native_observations": list((expected - actual).elements()),
        "extra_native_observations": list((actual - expected).elements()),
        "plan_differences": differences,
    }


def compare_execution(records, decisions, bars, tick, snapshot, settings):
    model = settings["impulse_model"]
    point = Decimal(snapshot[18])
    start, end = map(int, snapshot[19:21])
    offers = [
        model_offer(d, bars, settings["execution"], tick, point, model["maximum_hold_bars"])
        for d in decisions if d.state == "RISK_VALID" and start <= d.time <= end
    ]
    modeled, blocked = occupy(offers, "IMPULSE")
    planned = {f"IMP-{side_text(t.side)}-{t.decision_time}": t for t in modeled}
    native_plans = {v[-1] for kind, _, v in records if kind == "ENTRY_VALID"}
    closed = {v[0]: v for kind, _, v in records if kind == "CLOSED"}
    fills = {v[0]: (int(v[1]), Decimal(v[2]), Decimal(v[3])) for kind, _, v in records if kind == "FILL"}
    fills.update({k: (int(v[1]), Decimal(v[3]), Decimal(v[5])) for k, v in closed.items()})
    diffs = []
    modeled_net = Decimal(0)
    native_net = Decimal(0)
    epsilon = Decimal("0.0000001")
    reasons = {"STOP": "SL", "STOP_GAP": "SL", "TARGET": "TP", "TARGET_GAP": "TP", "TIME": "TIME"}
    for key, t in planned.items():
        if t.entry_time is None:
            if key in fills:
                diffs.append({"id": key, "error": "unexpected native fill"})
        elif key not in fills or fills[key][0] != t.entry_time or (
            abs(fills[key][1] - Decimal(str(t.entry_price))) > epsilon or fills[key][2] != t.side * t.units
        ):
            diffs.append({"id": key, "error": "native entry differs"})
        native = closed.get(key)
        exit_ = t.exits[0] if t.exits else None
        if exit_ is not None:
            modeled_net += exit_.net
        if (native is None) != (exit_ is None):
            diffs.append({"id": key, "error": "native close count differs"})
            continue
        if native is not None and exit_ is not None:
            fee = Decimal(native[7])
            net = (Decimal(native[4]) - Decimal(native[3])) * Decimal(native[5]) * point - fee
            native_net += net
            if (
                int(native[2]) != exit_.time or abs(Decimal(native[4]) - Decimal(str(exit_.price))) > epsilon
                or native[8] != reasons[exit_.reason] or abs(net - exit_.net) > epsilon or fee != exit_.commission
            ):
                diffs.append({"id": key, "error": "native exit or costs differ"})
    if abs(native_net - Decimal(snapshot[14])) > epsilon:
        diffs.append({"error": "native net cash does not reconcile"})
    missing = sorted(planned.keys() - native_plans)
    extra = sorted(native_plans - planned.keys())
    return {
        "status": "MATCHED" if not (diffs or missing or extra) else "MISMATCH",
        "native_submissions": len(native_plans),
        "native_fills": len(fills),
        "native_closures": len(closed),
        "modeled_occupancy_rejections": len(blocked),
        "modeled_net_usd": str(modeled_net),
        "native_net_usd": snapshot[14],
        "missing_native_submissions": missing,
        "extra_native_submissions": extra,
        "differences": diffs,
    }


def verify(path, settings):
    records, cutoff, snapshot = read_log(path)
    bars, tick = native_bars(path, records, snapshot)
    cfg = settings["impulse_model"]
    full = scan(bars, cfg, tick)
    cuts = sorted({n for n in (251, len(bars) // 3, len(bars) // 2, 2 * len(bars) // 3, len(bars) - 1)
                   if 0 < n < len(bars)})
    prefix_differences = [n for n in cuts if scan(bars[:n], cfg, tick) != [d for d in full if d.bar < n]]
    comparison = compare_native(records, full)
    matched = not any(comparison[key] for key in ("missing_native_observations", "extra_native_observations", "plan_differences"))
    execution = compare_execution(records, full, bars, tick, snapshot, settings)
    return {
        "symbol": snapshot[0],
        "timeframe": snapshot[1],
        "input_log_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "last_close_ms": cutoff,
        "ohlc_bars": len(bars),
        "prefix_lengths": cuts,
        "prefix_mismatches": prefix_differences,
        **comparison,
        "execution": execution,
        "status": "MATCHED_NATIVE_AND_PREFIX" if matched and not prefix_differences else "MISMATCH",
    }


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--logs", type=Path, nargs="+", required=True)
    cli.add_argument("--out", type=Path, required=True)
    args = cli.parse_args()
    settings = json.loads(REGISTRATION.read_text(encoding="utf-8"))
    cells = [verify(path, settings) for path in args.logs]
    matches = all(cell["status"] == "MATCHED_NATIVE_AND_PREFIX" and cell["execution"]["status"] == "MATCHED" for cell in cells)
    with args.out.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump({"status": "PASS" if matches else "MISMATCH", "cells": cells}, stream, indent=2)
        stream.write("\n")
    print("PASS" if matches else "MISMATCH", [c["timeframe"] for c in cells])
    return int(not matches)


if __name__ == "__main__":
    raise SystemExit(main())
