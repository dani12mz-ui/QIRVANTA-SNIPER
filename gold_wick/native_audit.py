"""Verify TradingView logs, local decisions and a longer confirmed export."""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime
from hashlib import sha256
import json
from math import floor, isclose
from pathlib import Path

from gold_wick.engine import scan
from unified.tools.build_gold_wick import TARGETS


SYMBOL = "OANDA:XAUUSD"
FRAMES = (5, 15, 30)
START = int(datetime.fromisoformat("2026-11-10T00:00:00+00:00").timestamp() * 1000)
END = int(datetime.fromisoformat("2026-11-11T06:00:00+00:00").timestamp() * 1000)
PRIME = 1_000_000_007
TAGS = {"WICK_CTX": 24, "WICK_EVT": 17, "WICK_PLAN": 19, "WICK_EXEC": 10}


def number(value):
    return None if value == "NA" else float(value)


def integer(value):
    return None if value == "NA" else int(value)


def require(value, problem):
    if not value:
        raise ValueError(problem)


def native_rows(path, frame):
    rows = []
    context, higher, tokens = [], [], []
    counts = Counter()
    interval = frame * 60_000
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for item in csv.DictReader(stream):
            fields = tuple(item["Message"].split("|"))
            tag = fields[0]
            if tag not in TAGS:
                continue
            require(len(fields) == TAGS[tag] and fields[1] == "1", f"{path.name}: malformed {tag}")
            stamp = int(datetime.fromisoformat(item["Date"]).timestamp() * 1000)
            known = int(fields[5] if tag == "WICK_CTX" else fields[2])
            require(known == stamp + interval, f"{path.name}: unexpected close availability for {tag}")
            rows.append((known, fields))
            counts[tag] += 1
            if tag != "WICK_CTX":
                continue
            symbol, tf, opening, ending, o, h, l, c, volume_type, volume = fields[2:12]
            opened, closed = int(opening), int(ending)
            require((symbol, int(tf), opened) == (SYMBOL, frame, stamp), f"{path.name}: context identity")
            require(not context or opened >= context[-1]["time_close"], f"{path.name}: repeated/overlapping bar")
            bar = dict(zip(("open", "high", "low", "close"), map(float, (o, h, l, c))))
            raw_volume, tick = number(volume), float(fields[18])
            valid_volume = raw_volume is not None and raw_volume > 0
            require(volume_type == "tick" and (fields[19] == "1") == valid_volume, f"{path.name}: volume metadata")
            h1, h4 = (integer(fields[i]) for i in (15, 16))
            for availability, direction, max_age in ((integer(fields[13]), h1, 7_200_000),
                                                       (integer(fields[14]), h4, 28_800_000)):
                if direction is not None:
                    require(direction in (-1, 0, 1) and availability is not None
                            and 0 <= opened - availability <= max_age, f"{path.name}: HTF availability")
            bar.update(time=opened, time_close=closed, volume=raw_volume)
            context.append(bar)
            higher.append((h1, h4))
            weighted_price = sum(weight * floor(bar[key] / tick + .5) for key, weight in
                                 zip(("open", "high", "low", "close"), (5, 7, 11, 13)))
            token = opened // 1000 + 3 * closed // 1000 + weighted_price
            token += 17 * int(raw_volume % PRIME) if valid_volume else 0
            tokens.append(token % PRIME)
            if len(tokens) >= 40:
                require(sum(tokens[-40:]) % PRIME == integer(fields[17]), f"{path.name}: bar fingerprint mismatch")
    require(context and counts["WICK_EVT"], f"{path.name}: context and event evidence required")
    require(rows == sorted(rows, key=lambda entry: entry[0]), f"{path.name}: unordered evidence")
    return rows, context, higher, counts


def local_parity(rows, context, higher, frame):
    result = scan(context, minutes=frame, context=higher)
    require(sum(row["time_close"] < START for row in context) >= 40, "Insufficient native context before scoring")
    def flag(raw):
        return None if raw == "NA" else raw == "1"

    native_lifecycle = [
        (known, f[3], None if f[4] == "NA" else f[4].replace(":", "|"), int(f[5]), integer(f[6]),
         number(f[7]), number(f[8]), integer(f[10]), integer(f[11]), flag(f[12]), flag(f[13]),
         integer(f[14]), f[15], integer(f[16]))
        for known, f in rows if START <= known <= END and f[0] == "WICK_EVT"
    ]
    local_lifecycle = [
        (e["time_ms"], e["state"], e["formation_id"], e["side"], e["first_known_ms"], e["top"], e["bottom"],
         e["age_bars"], e["formation_bars"], e["at_structure"], e["formation_volume_available"],
         e["breakout_known_ms"], e["reason"], e["first_rejection_ms"])
        for e in result.events if START <= e["time_ms"] <= END
    ]
    by_key = {
        (e["time_ms"], e["model"], e["side"], e["plan_id"]): e
        for e in result.events if START <= e["time_ms"] <= END and e["state"] in ("ENTRY_VALID", "RISK_REJECTED")
    }
    compared = set()
    issues = []
    for known, f in rows:
        if not START <= known <= END or f[0] != "WICK_PLAN":
            continue
        key = known, f[4], int(f[5]), f[3].replace(":", "|")
        e = by_key.get(key)
        if e is None:
            issues.append({"key": key, "reason": "Missing local plan"})
            continue
        compared.add(key)
        expected = "OK" if e["state"] == "ENTRY_VALID" else e["reason"]
        if f[17] != expected:
            issues.append({"key": key, "reason": "Different price/risk decision", "native": f[17], "local": e["reason"]})
        def equal(raw, expected_value):
            actual = number(raw)
            if actual is None or expected_value is None:
                return actual is expected_value
            return isclose(actual, expected_value, abs_tol=1e-8, rel_tol=0)

        wanted = (e["reference"], e["stop"], *(e["targets"] or (None, None, None)),
                  result.features[e["bar"]]["prior_atr"], e["known_barrier"], e["net_room_r"])
        if not all(equal(f[i], value) for i, value in enumerate(wanted, start=6)):
            issues.append({"key": key, "reason": "Different risk levels"})
        for offset, name in ((14, "volume_dominance"), (15, "volume_activity"), (16, "htf_aligned")):
            if (f[offset] == "1") != bool(e[name]):
                issues.append({"key": key, "reason": f"Different {name} classification"})
    for key in sorted(set(by_key) - compared):
        issues.append({"key": key, "reason": "Missing native plan"})
    difference = None
    if native_lifecycle != local_lifecycle:
        nset, lset = set(native_lifecycle), set(local_lifecycle)
        difference = {"native_only": sorted(nset - lset, key=repr)[:5],
                      "local_only": sorted(lset - nset, key=repr)[:5]}
    return {
        "lifecycle_equal": difference is None, "lifecycle_diff_sample": difference,
        "native_lifecycle_count": len(native_lifecycle), "local_lifecycle_count": len(local_lifecycle),
        "matched_risk_assessments": len(compared), "risk_issue_count": len(issues), "risk_issue_sample": issues[:10],
    }


def native_execution(rows, context, frame):
    by_id = defaultdict(list)
    active = None
    by_open = {row["time"]: row["time_close"] for row in context}
    opens = list(by_open)
    open_index = {time: index for index, time in enumerate(opens)}
    # The emulator represents half-spread and commission together in its per-fill charge.
    per_fill_charge = 0.35
    plans = {f[3]: (known, f) for known, f in rows if f[0] == "WICK_PLAN" and f[18] == "OK"}
    for known, f in rows:
        if f[0] != "WICK_EXEC":
            continue
        state, campaign, executed, price, quantity, reason, net = f[3:]
        actual = integer(executed)
        if state == "SUBMITTED":
            require(active is None and actual == known, f"Duplicate or premature order: {campaign}")
            plan = plans.get(campaign)
            require(plan and plan[0] == known and plan[1][6] == price and plan[1][4] == reason
                    and float(quantity) == int(plan[1][5]) * 3, f"Submission differs from plan: {campaign}")
            active = campaign
        if state == "REJECTED":
            require(active is not None and reason == "POSITION_OCCUPIED", f"Unexplained native rejection: {campaign}")
            continue
        require(active == campaign, f"Execution event outside campaign: {campaign}")
        by_id[campaign].append((state, known, actual, number(price), number(quantity), reason, number(net)))
        if state == "CLOSED":
            active = None
    summary = []
    for campaign, events in by_id.items():
        submitted, = (event for event in events if event[0] == "SUBMITTED")
        fills = [event for event in events if event[0] == "FILLED"]
        parts = [event for event in events if event[0] == "EXIT_PART"]
        closes = [event for event in events if event[0] == "CLOSED"]
        require(len(fills) <= 1 and len(closes) <= 1, f"Repeated native campaign state: {campaign}")
        require(not (parts or closes) or fills, f"Missing fill: {campaign}")
        if fills:
            _, observed, filled, fill_price, signed_size, _, _ = fills[0]
            next_bar = open_index[submitted[1] - frame * 60_000] + 1
            require(filled == opens[next_bar] and by_open[filled] == observed,
                    f"Fill was not at the next chart open: {campaign}")
            plan_fields = plans[campaign][1]
            for _, observed_at, exited, exit_price, signed_part, reason, net in parts:
                require(filled <= exited <= observed_at and signed_part * signed_size > 0
                        and isclose(net, (exit_price - fill_price) * signed_part - 2 * per_fill_charge * abs(signed_part),
                                    abs_tol=1e-6), f"Exit accounting mismatch: {campaign}")
                side = int(plan_fields[5])
                if reason in ("TP1", "TP2", "TP3"):
                    require(side * (exit_price - float(plan_fields[7 + int(reason[-1])])) >= -1e-8,
                            f"Target fill worse than limit: {campaign}")
                elif reason == "SL":
                    require(side * (exit_price - float(plan_fields[7])) <= 1e-8,
                            f"Stop fill better than stop trigger: {campaign}")
        if closes:
            require(isclose(sum(event[4] for event in parts), fills[0][4], abs_tol=1e-9)
                    and isclose(sum(event[6] for event in parts), closes[0][6], abs_tol=1e-6),
                    f"Closed campaign accounting mismatch: {campaign}")
        summary.append({"id": campaign, "submitted": submitted[1], "fill": fills[0][2:5] if fills else None,
                        "parts": [part[2:] for part in parts], "net_usd": closes[0][6] if closes else None})
    return summary


def native_variants(directory, base):
    def observation(rows):
        return [(time, f[:18] if f[0] == "WICK_PLAN" else f)
                for time, f in rows if f[0] != "WICK_EXEC"]

    result = {}
    for label in ("retest", "volume_B", "volume_C"):
        path = directory / f"strategy_M5_{label}.csv"
        rows, context, _, counts = native_rows(path, 5)
        require(observation(rows) == observation(base), f"M5 {label}: variant changed underlying observations")
        gates = Counter(f[18] for known, f in rows if START <= known <= END and f[0] == "WICK_PLAN")
        result[label] = {"sha256": sha256(path.read_bytes()).hexdigest(), "counts": counts,
                         "selection_in_scored_window": gates, "observation_parity": True,
                         "native_execution": native_execution(rows, context, 5)}
    return result


def bounded_calculation(directory, baseline):
    path = directory / "indicator_M5_calculated_500.csv"
    rows, context, _, _ = native_rows(path, 5)
    full_first = next(int(f[4]) for _, f in baseline if f[0] == "WICK_CTX")
    require(context[0]["time"] > full_first,
            "Bounded calculation must begin after the full calculation")
    require(sum(bar["time_close"] < START for bar in context) >= 40, "Bounded calculation has insufficient warmup")
    bounded = [row for row in rows if START <= row[0] <= END]
    full = [row for row in baseline if START <= row[0] <= END]
    require(len(bounded) == len(full), "Native calculated bars coverage divergence")
    missing_context = Counter()
    for (full_stamp, expected), (bounded_stamp, actual) in zip(full, bounded):
        require(full_stamp == bounded_stamp and expected[0] == actual[0], "Native calculated bars lifecycle divergence")
        for index, (old, new) in enumerate(zip(expected, actual)):
            if old == new:
                continue
            if expected[0] == "WICK_CTX" and index in (13, 14, 15, 16) and new == "NA":
                missing_context[{13: "h1_close_time", 14: "h4_close_time", 15: "h1_side", 16: "h4_side"}[index]] += 1
            elif expected[0] == "WICK_PLAN" and index == 16 and (old, new) == ("1", "0"):
                missing_context["plan_alignment_unavailable"] += 1
            else:
                raise ValueError(f"Native calculated bars divergence at {full_stamp}, {expected[0]} field {index}")
    return {"price_and_no_context_decisions_equal": True, "calculated_bars": 500,
            "first_open_ms": context[0]["time"], "full_first_open_ms": full_first,
            "matched_rows": len(full), "htf_fields_unavailable": missing_context,
            "sha256": sha256(path.read_bytes()).hexdigest()}


def audit(directory):
    frames = {}
    for frame in FRAMES:
        indicator = directory / f"indicator_M{frame}.csv"
        strategy = directory / f"strategy_M{frame}.csv"
        ir, ctx, higher, counts = native_rows(indicator, frame)
        sr, sctx, _, scounts = native_rows(strategy, frame)
        require(ir == [row for row in sr if row[1][0] != "WICK_EXEC"], f"M{frame}: Pine role divergence")
        lifecycle = Counter(f[3] for stamp, f in ir if START <= stamp <= END and f[0] == "WICK_EVT")
        cases = {kind: next(([stamp, f[4], f[15]] for stamp, f in ir
                             if START <= stamp <= END and f[0] == "WICK_EVT" and f[3] == kind), None)
                 for kind in lifecycle}
        frames[str(frame)] = {
            "indicator_sha256": sha256(indicator.read_bytes()).hexdigest(),
            "strategy_sha256": sha256(strategy.read_bytes()).hexdigest(),
            "indicator_counts": counts, "strategy_counts": scounts, "role_parity": True,
            "first_open_ms": ctx[0]["time"], "last_close_ms": ctx[-1]["time_close"],
            "scored_lifecycle": lifecycle, "first_example": cases,
            "native_execution": native_execution(sr, sctx, frame), "local_parity": local_parity(ir, ctx, higher, frame),
        }
    extended = directory / "indicator_M5_extended.csv"
    xr, xctx, _, _ = native_rows(extended, 5)
    ir, ctx, _, _ = native_rows(directory / "indicator_M5.csv", 5)
    require(ir == [row for row in xr if row[0] <= END] and len(xctx) > len(ctx), "M5 confirmed-prefix divergence")
    passed = all(item["local_parity"]["lifecycle_equal"] and not item["local_parity"]["risk_issue_count"]
                 for item in frames.values())
    return {
        "status": "VERIFIED" if passed else "INCONCLUSIVE", "symbol": SYMBOL, "score_close_ms": [START, END],
        "pine_sources_sha256": {role: sha256(path.read_bytes()).hexdigest() for role, path in TARGETS.items()},
        "frames": frames, "extended_confirmed_prefix": {"equal": True, "extra_context_bars": len(xctx) - len(ctx),
            "extended_last_close_ms": xctx[-1]["time_close"], "sha256": sha256(extended.read_bytes()).hexdigest()},
        "native_variants_M5": native_variants(directory, native_rows(directory / "strategy_M5.csv", 5)[0]),
        "bounded_calculation_M5": bounded_calculation(directory, ir),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    report = audit(args.logs)
    with args.out.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    print(report["status"], "M5/M15/M30 native roles and M5 confirmed prefix")
    return int(report["status"] != "VERIFIED")


if __name__ == "__main__":
    raise SystemExit(main())
