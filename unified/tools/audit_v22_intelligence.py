"""Audit complete V2.2 Intelligence TradingView OHLC and observation exports."""

import argparse
from collections import Counter
from copy import deepcopy
import csv
from dataclasses import dataclass, replace
from decimal import Decimal
import hashlib
import json
from math import isclose
from pathlib import Path
from statistics import mean

from unified.tools.evaluate_v22_signals import feature_rows, scan


REGISTRATION = Path(__file__).resolve().parents[2] / "docs/v22_evidence/historical_registration.json"


def number(raw):
    return None if raw == "-" else float(raw)


def integer(raw):
    return None if raw == "-" else int(raw)


def near(a, b):
    return a is None and b is None or a is not None and b is not None and isclose(a, b, rel_tol=0, abs_tol=1e-7)


def bias(close, fast, slow):
    if close is None or fast is None or slow is None:
        return 0
    return 1 if close > fast > slow else -1 if close < fast < slow else 0


@dataclass(frozen=True)
class Bar:
    known: int
    open_time: int
    open: float
    high: float
    low: float
    close: float
    atr: float | None
    ema21: float | None
    ema55: float | None
    adx: float | None
    di_plus: float | None
    di_minus: float | None
    prior_high: float | None
    prior_low: float | None
    trend: int
    structure: str
    regime: str
    h1_at: int | None
    h1_bias: int
    h4_at: int | None
    h4_bias: int
    h1_values: tuple
    h4_values: tuple

    @classmethod
    def parse(cls, known, payload):
        return cls(
            known, int(payload[0]), *map(number, payload[1:13]),
            int(payload[13]), payload[14], payload[15],
            integer(payload[16]), int(payload[17]), integer(payload[18]), int(payload[19]),
            tuple(map(number, payload[20:23])), tuple(map(number, payload[23:26])),
        )


def records(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            message = row["Message"]
            if message.startswith("V22I|"):
                _, event, known, *payload = message.split("|")
                yield event, int(known), payload


def fingerprint(bars, tick):
    a = b = 0
    for row in bars:
        o, h, l, c = (
            int((Decimal(str(value)) / tick).to_integral_value())
            for value in (row.open, row.high, row.low, row.close)
        )
        stamp = row.open_time // 1000
        a = (31 * a + stamp + 3 * o + 5 * h + 7 * l + 11 * c) % 1_000_000_007
        b = (131 * b + stamp + 13 * o + 17 * h + 19 * l + 23 * c) % 1_000_000_009
    return a, b


def audit_htf(bars):
    result = {}
    for label in ("h1", "h4"):
        previous = None
        seen = {}
        initialized = []
        initialized_after_warmup = 0
        directions = Counter()
        for i, row in enumerate(bars):
            stamp = getattr(row, f"{label}_at")
            side = getattr(row, f"{label}_bias")
            values = getattr(row, f"{label}_values")
            if stamp is None:
                if side:
                    raise ValueError(f"{label} bias lacks a known close at {row.known}")
                continue
            if stamp > row.open_time or previous is not None and stamp < previous:
                raise ValueError(f"{label} timestamp outside causal order at {row.known}")
            if side != bias(*values) or stamp in seen and seen[stamp] != values:
                raise ValueError(f"{label} inconsistent confirmed values at {row.known}")
            previous = stamp
            seen[stamp] = values
            if all(value is not None for value in values):
                initialized.append(row.known)
                initialized_after_warmup += i >= 250
                directions[side] += 1
        result[label] = {
            "distinct_closed_bars": len(seen),
            "ltf_rows_with_initialized_emas": len(initialized),
            "ltf_rows_with_initialized_emas_after_warmup": initialized_after_warmup,
            "first_initialized_ltf_close_ms": initialized[0] if initialized else None,
            "initialized_bias_counts": dict(sorted(directions.items())),
        }
    return result


def pivot_pairs(bars):
    """Return confirmed pivot pairs available before each chart bar."""
    highs, lows, pairs = [], [], []
    for i, row in enumerate(bars):
        high = highs[-1] if highs else None
        low = lows[-1] if lows else None
        if not (near(row.prior_high, high) and near(row.prior_low, low)):
            raise ValueError(f"strict pivot disclosure mismatch at {row.known}")
        pairs.append((tuple(highs[-2:]), tuple(lows[-2:])))
        if i >= 4:
            group = bars[i - 4:i + 1]
            center = group[2]
            if all(center.high > other.high for j, other in enumerate(group) if j != 2):
                highs.append(center.high)
            if all(center.low < other.low for j, other in enumerate(group) if j != 2):
                lows.append(center.low)
    return pairs, len(highs), len(lows)


def audit_regime(bars, pairs, observed):
    actual = {}
    previous = ""
    break_bias = 0
    trs = []
    momentum = []
    for i, (row, (highs, lows)) in enumerate(zip(bars, pairs, strict=True)):
        tr = (row.high - row.low if i == 0 else
              max(row.high - row.low, abs(row.high - bars[i - 1].close), abs(row.low - bars[i - 1].close)))
        trs.append(tr)
        ready = i >= 250 and row.atr is not None and row.atr > 0
        prior_high = highs[-1] if highs else None
        prior_low = lows[-1] if lows else None
        hhhl = len(highs) == len(lows) == 2 and highs[-1] > highs[-2] and lows[-1] > lows[-2]
        lhll = len(highs) == len(lows) == 2 and highs[-1] < highs[-2] and lows[-1] < lows[-2]
        local = bias(row.close, row.ema21, row.ema55)
        trend = 0
        slope_bar = bars[i - 3] if i >= 3 else None
        if (local and slope_bar is not None and slope_bar.ema21 is not None and slope_bar.ema55 is not None
                and local * (row.ema21 - slope_bar.ema21) > 0 and local * (row.ema55 - slope_bar.ema55) > 0):
            directional = (row.adx is not None and row.adx >= 18 and row.di_plus is not None and row.di_minus is not None
                           and local * (row.di_plus - row.di_minus) > 0)
            if directional or local == 1 and hhhl or local == -1 and lhll:
                trend = local
        bos = 0
        if ready and prior_high is not None and bars[i - 1].close <= prior_high < row.close:
            bos = 1
        elif ready and prior_low is not None and bars[i - 1].close >= prior_low > row.close:
            bos = -1
        mss = bool(bos and break_bias == -bos)
        if bos:
            break_bias = bos
        structure = ("MSS UP" if bos == 1 else "MSS DOWN") if mss else (
            "BOS UP" if bos == 1 else "BOS DOWN" if bos == -1 else "HH / HL" if hhhl else "LH / LL" if lhll else "MIXED")
        momentum.append((row.close - bars[i - 3].close) / row.atr if ready else None)
        if not ready:
            regime, reason = "WARMUP", "INSUFFICIENT_BARS"
        else:
            candle_range = row.high - row.low
            ema_distance = abs(row.close - row.ema21) / row.atr
            wick_exhaustion = False
            if candle_range > 0 and momentum[-2] is not None:
                wick_exhaustion = (
                    momentum[-2] >= 0.80 and row.high - row.ema21 >= 2.50 * row.atr
                    and row.high - max(row.open, row.close) >= 0.50 * candle_range and momentum[-1] < momentum[-2]
                    or momentum[-2] <= -0.80 and row.ema21 - row.low >= 2.50 * row.atr
                    and min(row.open, row.close) - row.low >= 0.50 * candle_range and momentum[-1] > momentum[-2]
                )
            narrow = (max(b.high for b in bars[i - 3:i + 1]) - min(b.low for b in bars[i - 3:i + 1])
                      <= 1.50 * row.atr and mean(trs[-4:]) <= 0.80 * row.atr)
            contraction = row.atr <= 0.80 * mean(b.atr for b in bars[i - 19:i + 1])
            if ema_distance > 2.50 or wick_exhaustion:
                regime, reason = "EXHAUSTION / OVEREXTENSION", "EMA_DISTANCE" if ema_distance > 2.50 else "WICK_AND_DECELERATION"
            elif mss:
                regime, reason = "POTENTIAL REVERSAL", "OPPOSITE_STRUCTURE_BREAK"
            elif tr >= 1.50 * row.atr:
                regime, reason = "VOLATILITY EXPANSION", "TRUE_RANGE_EXPANSION"
            elif narrow or contraction:
                regime, reason = "COMPRESSION", "NARROW_BOX" if narrow else "ATR_CONTRACTION"
            elif trend:
                regime = "BULLISH TREND" if trend == 1 else "BEARISH TREND"
                reason = "EMA_DIRECTION_WITH_STRUCTURE_OR_DMI"
            else:
                regime, reason = "SIDEWAYS / RANGE", "NO_QUALIFIED_TREND"
        if (row.trend, row.structure, row.regime) != (trend, structure, regime):
            raise ValueError(f"regime or structure mismatch at {row.known}")
        if regime != previous:
            actual[row.known] = f"{previous or 'INITIAL'}>{regime}:{reason}"
            previous = regime
    if actual != observed:
        raise ValueError("regime transition timestamps or reasons differ")
    return Counter(row.regime for row in bars)


def audit_candidates(observations, candidates, bars, pairs):
    by_close = {row.known: i for i, row in enumerate(bars)}
    states = {}
    watched = {}
    confirmed = {}
    counts = Counter()
    for at, fields in observations:
        model, side, setup_id, state, _, level = fields
        if model == "REGIME":
            continue
        if at not in by_close or model not in {"A", "B", "C"} or side not in {"1", "-1"}:
            raise ValueError("invalid observation context")
        counts[(model, state)] += 1
        if state == "WATCH" and setup_id not in states:
            watched[setup_id] = model, int(side), at, number(level)
        elif state == "CONFIRMING" and states.get(setup_id) == "WATCH":
            pass
        elif state == "CONFIRMED" and states.get(setup_id) == "CONFIRMING":
            confirmed[setup_id] = at
        elif state in {"INVALIDATED", "EXPIRED"} and states.get(setup_id) in {"WATCH", "CONFIRMING"}:
            pass
        else:
            raise ValueError(f"unexpected {state} after {states.get(setup_id)} for {setup_id}")
        states[setup_id] = state
    seen = set()
    for at, fields in candidates:
        model, side, setup_id, watch_at, level, reference, stop, barrier, atr, extension, spike, conflict = fields
        d, t = int(side), int(watch_at)
        if setup_id in seen or confirmed.get(setup_id) != at or watched.get(setup_id) != (model, d, t, number(level)):
            raise ValueError(f"unlinked or duplicate candidate {setup_id}")
        seen.add(setup_id)
        i, j = by_close[at], by_close[t]
        row = bars[i]
        if j > i or (model != "A" and j == i):
            raise ValueError(f"candidate precedes its causal watch delay {setup_id}")
        if model == "A":
            planned_stop = row.prior_low if d == 1 else row.prior_high
        elif model == "B":
            planned_stop = min(b.low for b in bars[j:i + 1]) if d == 1 else max(b.high for b in bars[j:i + 1])
        else:
            planned_stop = min(b.low for b in bars[j - 3:j + 1]) if d == 1 else max(b.high for b in bars[j - 3:j + 1])
        swings = pairs[i][0 if d == 1 else 1]
        ahead = [price for price in swings if d * (price - row.close) > 0]
        obstacle = (min(ahead) if d == 1 else max(ahead)) if ahead else None
        tr = max(row.high - row.low, abs(row.high - bars[i - 1].close), abs(row.low - bars[i - 1].close))
        if not all((near(number(reference), row.close), near(number(stop), planned_stop),
                    near(number(barrier), obstacle), near(number(atr), row.atr),
                    near(number(extension), abs(row.close - row.ema21) / row.atr),
                    int(spike) == int(tr > 2.50 * row.atr),
                    int(conflict) == int(row.h1_bias == -d or row.h4_bias == -d))):
            raise ValueError(f"native candidate geometry differs for {setup_id}")
    if seen != confirmed.keys():
        raise ValueError("confirmed observations are missing raw candidates")
    return {model: {state: n for (m, state), n in sorted(counts.items()) if m == model} for model in ("A", "B", "C")}


def compare_replay(bars, observations, candidates, flags, settings):
    settings = deepcopy(settings)
    settings["A"]["require_displacement"] = flags[-1]
    enabled = {m for m, on in zip("ABC", flags[:3], strict=True) if on}
    tape = [{"time": b.open_time, "time_close": b.known, "open": b.open, "high": b.high,
             "low": b.low, "close": b.close} for b in bars]
    local = feature_rows(tape, settings)
    # H1/H4 market aggregation and prehistory are part of the native feed.
    # Replay the same decisions with its exported, independently audited values.
    aligned = [replace(f, h1=b.h1_bias, h4=b.h4_bias) for f, b in zip(local, bars, strict=True)]
    replayed, replayed_candidates = scan(tape, aligned, settings)
    native_events = Counter((t, f[0], int(f[1]), f[2], f[3], f[4]) for t, f in observations if f[0] in enabled)
    replay_events = Counter((d["time"], d["model"], d["side"], d["setup_id"], d["state"], d["reason"])
                            for d in replayed if d["model"] in enabled)
    extra = list((native_events - replay_events).elements())
    missing = list((replay_events - native_events).elements())
    native_plans = {(t, f[0], int(f[1])): f for t, f in candidates if f[0] in enabled}
    replay_plans = {(c.time, c.model, c.side): c for c in replayed_candidates if c.model in enabled}
    bad = []
    for key in sorted(native_plans.keys() | replay_plans.keys()):
        c, f = replay_plans.get(key), native_plans.get(key)
        if c is None or f is None or not all((
            c.watch_time == int(f[3]), near(c.reference, number(f[5])),
            near(c.stop_base, number(f[6])), near(c.barrier, number(f[7])),
            near(c.atr, number(f[8])), near(c.extension, number(f[9])),
            c.spike == bool(int(f[10])), c.htf_conflict == bool(int(f[11])),
        )):
            bad.append(key)
    cuts = sorted({n for n in (251, len(tape) // 3, len(tape) // 2, 2 * len(tape) // 3, len(tape) - 1)
                   if 0 < n < len(tape)})
    prefix_diffs = []
    for n in cuts:
        prefix = feature_rows(tape[:n], settings)
        prefix = [replace(f, h1=b.h1_bias, h4=b.h4_bias) for f, b in zip(prefix, bars[:n], strict=True)]
        events, plans = scan(tape[:n], prefix, settings)
        if events != [d for d in replayed if d["bar"] < n] or plans != [c for c in replayed_candidates if c.index < n]:
            prefix_diffs.append(n)
    return {
        "status": "MATCHED" if not (extra or missing or bad or prefix_diffs) else "MISMATCH",
        "scope": "Independent LTF feature and model replay with the native confirmed H1/H4 context.",
        "native_observations": sum(native_events.values()),
        "native_candidate_count": len(native_plans),
        "extra_native_observations": extra,
        "missing_native_observations": missing,
        "candidate_differences": bad,
        "prefix_lengths": cuts,
        "prefix_mismatches": prefix_diffs,
    }


def audit(path):
    tape = list(records(path))
    snapshots = [(at, fields) for kind, at, fields in tape if kind == "SNAPSHOT"]
    if len(snapshots) != 1:
        raise ValueError(f"{path.name} requires one snapshot")
    known, data = snapshots[0]
    symbol, frame, first, last, count, hash1, hash2, obs_count, candidate_count, regimes, cutoff, tick, *flags = data
    if symbol != "OANDA:XAUUSD" or frame not in {"5", "15", "30"} or known > int(cutoff):
        raise ValueError("unexpected chart or audit cutoff")
    bars = [Bar.parse(at, fields) for kind, at, fields in tape if kind == "BAR" and at <= known]
    if not bars or (len(bars), bars[0].open_time, bars[-1].open_time, bars[-1].known) != (
        int(count), int(first), int(last), known
    ):
        raise ValueError("export lacks the full calculation window")
    if any(bar.open_time >= bar.known or i and bars[i - 1].known > bar.open_time for i, bar in enumerate(bars)):
        raise ValueError("overlapping or invalid chart bars")
    if fingerprint(bars, Decimal(tick)) != (int(hash1), int(hash2)):
        raise ValueError("OHLC fingerprints differ")
    observations = [(at, fields) for kind, at, fields in tape if kind == "OBS" and at <= known]
    candidates = [(at, fields) for kind, at, fields in tape if kind == "CANDIDATE" and at <= known]
    observed_regimes = {at: fields[4] for at, fields in observations if fields[0] == "REGIME"}
    if (len(observations) - len(observed_regimes), len(candidates), len(observed_regimes)) != tuple(
        map(int, (obs_count, candidate_count, regimes))
    ):
        raise ValueError("event totals differ from native snapshot")
    htf = audit_htf(bars)
    pairs, highs, lows = pivot_pairs(bars)
    regime_counts = audit_regime(bars, pairs, observed_regimes)
    model_counts = audit_candidates(observations, candidates, bars, pairs)
    settings = json.loads(REGISTRATION.read_text(encoding="utf-8"))["signals"]
    enabled = [bool(int(flag)) for flag in flags]
    replay = compare_replay(bars, observations, candidates, enabled, settings)
    return {
        "status": "VERIFIED" if replay["status"] == "MATCHED" else "MISMATCH",
        "frame": frame,
        "first_open_utc_ms": int(first),
        "last_close_utc_ms": known,
        "bars": len(bars),
        "fingerprints": [int(hash1), int(hash2)],
        "confirmed_htf_bars": {label: details["distinct_closed_bars"] for label, details in htf.items()},
        "confirmed_htf_coverage": htf,
        "strict_confirmed_swings": {"highs": highs, "lows": lows},
        "regime_transitions": len(observed_regimes),
        "regime_bars": dict(sorted(regime_counts.items())),
        "model_states": model_counts,
        "candidates": len(candidates),
        "enabled_a_b_c_and_a_displacement": enabled,
        "matched_replay": replay,
        "raw_log_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs", type=Path, nargs="+", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    cells = [audit(path) for path in args.logs]
    status = "VERIFIED" if all(cell["status"] == "VERIFIED" for cell in cells) else "MISMATCH"
    with args.out.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump({"status": status, "cells": cells}, stream, indent=2)
        stream.write("\n")
    print(status, [(cell["frame"], cell["bars"], cell["candidates"]) for cell in cells])
    return int(status != "VERIFIED")


if __name__ == "__main__":
    raise SystemExit(main())
