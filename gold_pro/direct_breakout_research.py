"""Reproduce the OANDA historical Direct Trend Breakout comparison."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass, replace
from gzip import GzipFile
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from statistics import mean, median
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import ModuleType
from zipfile import ZipFile

from gold_pro.archive import native_outcomes
from gold_pro.rc1_qa import ROOT, compare_exports, read_export
from gold_pro.rc2_qa import MODES, early_events, extras, indicator_checks, native_checks
from gold_pro.rc2_research import chart_bars, early_observations, offers_from_ready, utc
from gold_pro.replay import Costs, Plan, round_away
from research.data.loader import load_bars, validate_bars
from unified.tools.audit_premium_s5 import verify_bars
from unified.tools.audit_tv_observations import inspect
from unified.tools.evaluate_v21_impulse import hourly_events


WSB_COMMIT = "2a3463db4c8595bc049681d9922d8a927044b8f2"
WSB_CODE = "gold_wick/wsb_forensic.py"
WSB_ARCHIVE = "research/wsb_missed_sell/evidence/native-logs.zip"
RC2_ARCHIVE = "releases/gold-pro-rc2/evidence/native-logs.zip"
PRESSURE_ARCHIVE = "research/rc2-pressure-meter/evidence/native-logs.zip"
EVENT_CONFIG = "docs/v21_evidence/event_evaluation_registration.json"
FORWARD_SEAL = "docs/gold_pro_evidence/forward_seal_m5.json"
M15_PREFIX = "research/direct_trend_breakout/evidence/registered_m15_warmup_prefix.json"
PINS = {
    WSB_CODE: "b12d8ae0652d524cfe222ed1d52c36feae57b4d4aa126f88192333128635197c",
    WSB_ARCHIVE: "23282ff59fd4c2cb1b2fbad116b4d8de304b331c8902b6994d608674f8771dfc",
    RC2_ARCHIVE: "a058bd9789bf9f5d258a08316217f3bebc8b9951460ee0c499aae848c761813a",
    PRESSURE_ARCHIVE: "ccffc719d8b3b2b9bba1e56ba5c04894df813175f9e2faaf4964d3b6a476307b",
    EVENT_CONFIG: "dc023d3c15e75327924bb3852cdcb620a079eee99a16edee7472fcdf9e8f04a7",
    FORWARD_SEAL: "09181ecaf9e30cde62c5203b524cb458d808a07c24029e961e32bb25c474afd1",
    M15_PREFIX: "e519925e61eaa80001ada52153a28511a28cd2ff999e4c5b5178004551760852",
    "archival_m5_bars.csv": "44971f52b0008fefc4b61dda51c71043f08e4a2e3bc6d08121380e95232ace1d",
    "archival_s5_native.csv": "b629dc4c3b1b624b5fc4632016ed1d568fbd65a83d26a17a8ee5aa909a7f0ef4",
}
HOUR = 3_600_000
WARMUP = 250
CAPITAL = 100_000
MAX_CASH_RISK = 250
MAX_OPEN_MOVE_ATR = 0.20
CASE_START = 1794396600000  # 2026-11-11 11:30 UTC
CASE_END = 1794414600000  # 2026-11-11 16:30 UTC
BASE = Costs()
STRESS = replace(BASE, spread=0.70, ticks=5, commission=0.2625)


@dataclass(frozen=True)
class Execution:
    plan: Plan
    state: str
    release: int
    entry_bar: int | None = None
    entry: float | None = None
    risk_usd: float | None = None
    exits: tuple = ()
    marked: tuple = ()
    blocked_by: str | None = None

    @property
    def net(self):
        return sum(part["net_usd"] for part in self.exits)


def pinned(path, raw=None):
    if raw is None:
        if path in (WSB_ARCHIVE, WSB_CODE):
            raw = subprocess.check_output(["git", "show", f"{WSB_COMMIT}:{path}"], cwd=ROOT)
        else:
            raw = (ROOT / path).read_bytes()
    if sha256(raw).hexdigest() != PINS[path]:
        raise ValueError(f"Changed research input: {path}")
    return raw


def wsb_model():
    """Load only the SHA-pinned independent reconstruction from the prior commit."""
    module = ModuleType("gold_wick._wsb_frozen_for_direct_breakout")
    module.__file__ = str(ROOT / WSB_CODE)
    sys.modules[module.__name__] = module
    exec(compile(pinned(WSB_CODE), f"{WSB_COMMIT}:{WSB_CODE}", "exec"), module.__dict__)
    return module


def archive_file(archive, name, directory):
    dest = directory / name.replace("/", "_")
    dest.write_bytes(archive.read(name))
    return dest


def simulate(plan, bars, costs, *, delay=1, lock_r=None):
    """Freeze brackets at confirmation; use quote-side prices and stop-first OHLC."""
    side, stop = plan.side, plan.stop
    first = plan.bar + delay
    if first >= len(bars):
        return Execution(plan, "CENSORED_PENDING", len(bars))
    if any(bars[i]["time"] != bars[i - 1]["time_close"] for i in range(plan.bar + 1, first + 1)):
        return Execution(plan, "ENTRY_SESSION_GAP", first)
    open_mid = bars[first]["open"]
    half_spread = costs.spread / 2
    fill = open_mid + side * (half_spread + costs.slip)
    unit_fee = 2 * costs.commission
    adverse_risk = side * (fill - stop)
    if adverse_risk < costs.tick or side * (open_mid - side * half_spread - stop) <= 0:
        return Execution(plan, "OPEN_THROUGH_STOP", first)
    if side * (plan.targets[0] - fill) <= 0:
        return Execution(plan, "TP1_ALREADY_PASSED", first)
    if abs(open_mid - plan.reference) > MAX_OPEN_MOVE_ATR * plan.atr + costs.tick * 1e-6:
        return Execution(plan, "OPEN_MOVE_GT_0_20_ATR", first)
    risk = plan.units * ((adverse_risk + costs.slip) * costs.pointvalue + unit_fee)
    if risk > MAX_CASH_RISK or plan.units * fill * costs.pointvalue > CAPITAL:
        return Execution(plan, "RISK_CAP_AT_OPEN", first)

    active = list(enumerate(plan.targets, 1))
    exits, marked = [], []
    queued_exit = False
    units = plan.units / 3
    for i in range(first, len(bars)):
        bar = bars[i]
        opening = bar["open"] - side * half_spread
        adverse = (bar["low"] if side > 0 else bar["high"]) - side * half_spread
        favorable = (bar["high"] if side > 0 else bar["low"]) - side * half_spread
        got_tp1 = False

        def settle(leg, price, reason):
            exits.append({
                "bar": i, "bar_open_utc": utc(bar["time"]), "known_at_utc": utc(bar["time_close"]),
                "leg": leg[0], "price": price, "reason": reason,
                "net_usd": side * (price - fill) * costs.pointvalue * units - unit_fee * units,
            })
            active.remove(leg)

        if side * (opening - stop) <= 0 or queued_exit:
            reason = "SL_GAP" if side * (opening - stop) <= 0 else "TIME_EXIT"
            for leg in active[:]:
                settle(leg, opening - side * costs.slip, reason)
        else:
            for leg in active[:]:
                if side * (opening - leg[1]) >= 0:
                    settle(leg, leg[1], f"TP{leg[0]}_AT_OPEN")
                    got_tp1 |= leg[0] == 1
            if side * (adverse - stop) <= 0:
                for leg in active[:]:
                    settle(leg, stop - side * costs.slip, "SL")
            else:
                for leg in active[:]:
                    if side * (favorable - leg[1]) >= 0:
                        settle(leg, leg[1], f"TP{leg[0]}")
                        got_tp1 |= leg[0] == 1
        if got_tp1 and lock_r is not None:
            next_stop = round_away(fill + side * adverse_risk * lock_r, costs.tick, -side)
            stop = max(stop, next_stop) if side > 0 else min(stop, next_stop)
        close_liquidation = bar["close"] - side * (half_spread + costs.slip)
        open_value = units * len(active) * (side * (close_liquidation - fill) * costs.pointvalue - unit_fee)
        marked.append((i, sum(part["net_usd"] for part in exits) + open_value))
        if not active:
            return Execution(plan, "CLOSED", i, first, fill, risk, tuple(exits), tuple(marked))
        queued_exit = i - first + 1 >= plan.max_hold
    return Execution(plan, "CENSORED_OPEN", len(bars), first, fill, risk, tuple(exits), tuple(marked))


def portfolio(plans, bars, costs, *, delay=1, use_s5_lock=False):
    outcomes = []
    occupied_until = reserved_at = -1
    occupant = None
    for plan in sorted(plans, key=lambda p: (p.bar, p.model not in {"S5_RAW", "RC2"}, p.id)):
        if plan.bar < occupied_until or plan.bar == reserved_at:
            outcome = Execution(plan, "POSITION_OCCUPIED", plan.bar, blocked_by=occupant)
        else:
            lock = 0.05 if use_s5_lock and plan.model == "S5_RAW" else None
            outcome = simulate(plan, bars, costs, delay=delay, lock_r=lock)
            occupied_until = outcome.release
            reserved_at = plan.bar
            occupant = plan.id
        outcomes.append(outcome)
    return outcomes


def drawdown(values):
    high = maximum = 0.
    for value in values:
        high = max(high, value)
        maximum = max(maximum, high - value)
    return maximum


def stats(results, bars, start, end, side=0):
    selected = [x for x in results if x.entry_bar is not None
                and start <= bars[x.entry_bar]["time"] < end and (not side or x.plan.side == side)]
    # Every cohort keeps crossing and unclosed positions visible, outside closed P&L.
    closed = sorted((x for x in selected if x.state == "CLOSED" and bars[x.release]["time_close"] <= end),
                    key=lambda x: (x.release, x.entry_bar, x.plan.id))
    net = [x.net for x in closed]
    risks = [x.net / x.risk_usd for x in closed]
    won = sum(value > 0 for value in net)
    gross_profit, gross_loss = sum(x for x in net if x > 0), -sum(x for x in net if x < 0)
    total = 0.
    marks = []
    for trade in sorted(selected, key=lambda x: (x.entry_bar, x.plan.id)):
        marks.extend(total + value for bar, value in trade.marked if bars[bar]["time_close"] <= end)
        if trade.state == "CLOSED" and bars[trade.release]["time_close"] <= end:
            total += trade.net
    return {
        "filled": len(selected), "closed": len(closed), "censored_or_straddles": len(selected) - len(closed),
        "wins": won, "losses": sum(value < 0 for value in net),
        "win_rate": won / len(net) if net else None,
        "net_usd": sum(net), "net_r": sum(risks),
        "expectancy_r": mean(risks) if risks else None,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "marked_bar_close_drawdown_usd": drawdown(marks),
        "median_entry_repricing_atr": median(
            x.plan.side * (bars[x.entry_bar]["open"] - x.plan.reference) / x.plan.atr for x in selected
        ) if selected else None,
    }


def independent_hours(bars, minutes, rules):
    directional, audit = hourly_events(bars, minutes, rules)
    events = {e["start"]: e for e in directional}
    groups = defaultdict(list)
    for index, bar in enumerate(bars):
        groups[bar["time"] // HOUR * HOUR].append(index)
    prior = deque(maxlen=24)
    categories = {}
    step = minutes * 60_000
    for start, indices in groups.items():
        if [bars[i]["time"] for i in indices] != list(range(start, start + HOUR, step)):
            continue
        block = [bars[i] for i in indices]
        width = max(b["high"] for b in block) - min(b["low"] for b in block)
        if indices[0] >= WARMUP and len(prior) == 24 and median(prior) > 0:
            initial, mid, final = block[0]["open"], block[len(block) // 2 - 1]["close"], block[-1]["close"]
            first_half, last_half = mid - initial, final - mid
            reversal = (first_half * last_half < 0 and abs(last_half) >= abs(first_half)
                        and min(abs(first_half), abs(last_half)) >= 0.5 * median(prior))
            if start in events:
                category = "DIRECTIONAL_BUY" if events[start]["side"] > 0 else "DIRECTIONAL_SELL"
            elif reversal:
                category = "REVERSAL"
            elif abs(final - initial) <= 0.35 * width:
                category = "SIDEWAYS"
            else:
                category = "OTHER"
            categories[start] = category
        prior.append(width)
    audit["eligible_regimes"] = dict(Counter(categories.values()))
    return directional, categories, audit


def state_stamp(row):
    return row.get("time_ms", row.get("known_ms", row.get("known_time")))


def event_audit(events, categories, decisions, results, bars):
    close_times = [b["time_close"] for b in bars]
    by_hour, by_hour_offer, by_hour_fill = defaultdict(list), defaultdict(list), defaultdict(list)
    for row in decisions:
        by_hour[state_stamp(row) // HOUR * HOUR, row["side"]].append(row)
    for item in results:
        by_hour_offer[close_times[item.plan.bar] // HOUR * HOUR, item.plan.side].append(item)
        if item.entry_bar is not None:
            by_hour_fill[bars[item.entry_bar]["time"] // HOUR * HOUR, item.plan.side].append(item)
    watches = {"WATCH", "RETEST WATCH", "FORMED"}
    confirmations = {"BREAKOUT CONFIRMED", "CONFIRMED", "SIGNAL"}
    matched_fills = set()
    detail = []
    for ev in events:
        start, side = ev["start"], ev["side"]
        key = start, side
        seen = by_hour[key]
        offered = by_hour_offer[key]
        filled = sorted(by_hour_fill[key], key=lambda x: (x.entry_bar, x.plan.id))
        matched_fills.update(x.plan.id for x in filled)
        first = filled[0] if filled else None
        excursion = None
        if first:
            observed = [first.entry, *(p["price"] for p in first.exits if p["bar"] <= ev["last_bar"])]
            observed.extend(bars[i]["high" if side > 0 else "low"] - side * BASE.spread / 2
                            for i in range(first.entry_bar, min(first.release, ev["last_bar"] + 1)))
            excursion = max(side * (price - first.entry) for price in observed) / ev["directional_extreme_distance"]
        delay = lambda stamps: (min(stamps) - start) / 60_000 if stamps else None
        detail.append({
            "start_utc": utc(start), "side": side, "directional_price_distance": ev["directional_extreme_distance"],
            "watch_delay_min": delay([state_stamp(r) for r in seen if r["state"] in watches]),
            "confirmation_delay_min": delay([state_stamp(r) for r in seen if r["state"] in confirmations]),
            "candidate_delay_min": delay([close_times[x.plan.bar] for x in offered]),
            "fill_delay_min": delay([bars[x.entry_bar]["time"] for x in filled]),
            "same_side_fills": len(filled), "first_fill_capture_fraction": excursion,
            "unfilled_offer_states": dict(Counter(x.state for x in offered if x.entry_bar is None)),
            "rejected_observations": dict(Counter(str(r.get("reason", r.get("reasons")))
                                                  for r in seen if r["state"] == "REJECTED")),
        })
    regimes = defaultdict(Counter)
    off_fills = off_losses = off_net = opposite = 0
    for item in results:
        if item.entry_bar is None:
            continue
        hour = bars[item.entry_bar]["time"] // HOUR * HOUR
        category = categories.get(hour, "INELIGIBLE_OR_PARTIAL")
        key = regimes[category]
        key["fills"] += 1
        if item.state == "CLOSED":
            key["closed_losses"] += item.net < 0
            key["net_usd"] += item.net
        if item.plan.id not in matched_fills:
            off_fills += 1
            off_losses += item.state == "CLOSED" and item.net < 0
            off_net += item.net if item.state == "CLOSED" else 0
            opposite += category == ("DIRECTIONAL_SELL" if item.plan.side > 0 else "DIRECTIONAL_BUY")
    return {
        "episodes": detail,
        "recall": {label: {
            "directional_hours": len(group),
            "watch": sum(r["watch_delay_min"] is not None for r in group),
            "confirmation": sum(r["confirmation_delay_min"] is not None for r in group),
            "candidate": sum(r["candidate_delay_min"] is not None for r in group),
            "fill": sum(r["same_side_fills"] > 0 for r in group),
            "missed": sum(not r["same_side_fills"] for r in group),
        } for label, side in (("all", 0), ("BUY", 1), ("SELL", -1))
            for group in [[r for r in detail if not side or r["side"] == side]]},
        "regimes_at_entry": dict(regimes),
        "outside_same_side_event": {"fills": off_fills, "closed_losses": off_losses, "closed_net_usd": off_net,
                                    "opposite_directional_fills": opposite},
    }


def failures(decisions, bars, end):
    """Keep every tested break close and audit a reclaim within 3 contiguous closes."""
    candidates = defaultdict(list)
    for row in decisions:
        index = row.get("bar")
        level = row.get("level")
        if (row["state"] in {"BREAKOUT CONFIRMED", "SIGNAL"} and index is not None
                and level is not None and WARMUP <= index < len(bars) - 3):
            candidates[index, row["side"]].append(level)
    rows = []
    for (index, side), levels in sorted(candidates.items()):
        future = bars[index + 1:index + 4]
        if (future[-1]["time_close"] > end or
                any(future[i]["time"] != bars[index + i]["time_close"] for i in range(3))):
            continue
        reclaim = next((bar for bar in future if all(side * (bar["close"] - level) <= 0 for level in levels)), None)
        rows.append({"confirm_utc": utc(bars[index]["time_close"]), "side": side, "levels": levels,
                     "all_supporting_edges_reclaimed_by_utc": utc(reclaim["time_close"]) if reclaim else None})
    applicable = any(r["state"] in {"BREAKOUT CONFIRMED", "SIGNAL"} and r.get("level") is not None for r in decisions)
    return {"applicable_to_this_tape": applicable, "observable_three_bar_breaks": len(rows),
            "failed": sum(r["all_supporting_edges_reclaimed_by_utc"] is not None for r in rows), "rows": rows}


def journal(outcome, bars):
    plan = outcome.plan
    return {
        "id": plan.id, "model": plan.model, "side": plan.side, "decision_utc": utc(bars[plan.bar]["time_close"]),
        "reference": plan.reference, "stop": plan.stop, "targets": plan.targets,
        "state": outcome.state, "next_open_utc": utc(bars[plan.bar + 1]["time"]) if plan.bar + 1 < len(bars) else None,
        "next_open_mid": bars[plan.bar + 1]["open"] if plan.bar + 1 < len(bars) else None,
        "entry_utc": utc(bars[outcome.entry_bar]["time"]) if outcome.entry_bar is not None else None,
        "entry": outcome.entry, "risk_usd": outcome.risk_usd, "realized_usd": outcome.net,
        "net_r": outcome.net / outcome.risk_usd if outcome.risk_usd is not None else None,
        "parts": outcome.exits, "blocked_by": outcome.blocked_by,
    }


def evaluate_lane(name, plans, decisions, bars, windows, events, categories):
    use_lock = name in {"S5_RAW", "S5_ARCHIVED_ADMITTED"}
    results = {}
    for label, costs, delay in (("next_open", BASE, 1), ("stressed_next_open", STRESS, 1), ("one_bar_later", BASE, 2)):
        outcomes = portfolio(plans, bars, costs, delay=delay, use_s5_lock=use_lock)
        results[label] = {
            "states": dict(Counter(item.state for item in outcomes)),
            "windows": {label: {direction: stats(outcomes, bars, start, end, side)
                                  for direction, side in (("all", 0), ("BUY", 1), ("SELL", -1))}
                        for label, (start, end) in windows.items()},
        }
        if label == "next_open":
            results[label]["opportunities"] = event_audit(events, categories, decisions, outcomes, bars)
            results[label]["journal"] = [journal(x, bars) for x in outcomes]
    timely = results["next_open"]["windows"]["full"]["all"]
    late = results["one_bar_later"]["windows"]["full"]["all"]
    results["late_minus_next_open"] = {key: late[key] - timely[key] for key in ("filled", "closed", "net_usd", "net_r")}
    results["late_minus_next_open"]["note"] = "Entire portfolio recomputed with unchanged brackets and one extra bar of entry delay."
    results["decision_counts_after_warmup"] = dict(Counter(
        r["state"] for r in decisions if windows["full"][0] <= state_stamp(r) < windows["full"][1]
    ))
    results["rejection_reasons_after_warmup"] = dict(Counter(
        reason for r in decisions if r["state"] == "REJECTED" and windows["full"][0] <= state_stamp(r) < windows["full"][1]
        for reason in (r.get("reasons") or (r["reason"],))
    ))
    results["failed_breakouts"] = failures(decisions, bars, windows["full"][1])
    return results


def native_plans(run, frame, name):
    plans = [
        Plan(offer.id, name, offer.side, offer.index, offer.reference, offer.stop, offer.targets, offer.atr,
             offer.max_hold)
        for offer in offers_from_ready(run, frame)
    ]
    decisions = [{**row, "state": row["event"]} for row in run.core]
    return plans, decisions


def compare_early_watch(path, bars, frame):
    probe = read_export(path, frame, "research")
    journal = extras(path)
    native = early_events(journal["RC2_EARLY"], probe, frame)
    start, end = bars[WARMUP]["time_close"], bars[-1]["time_close"]
    original = {(stamp, direction) for stamp, direction in native.items() if start <= stamp <= end}
    computed = {(item["known_ms"], "BUY" if item["side"] > 0 else "SELL") for item in early_observations(bars)
                if start <= item["known_ms"] <= end}
    describe = lambda events: [{"known_utc": utc(t), "direction": side} for t, side in sorted(events)]
    return {
        "native_indicator_checks": indicator_checks(probe, journal, frame, research=True),
        "first_loaded_native_open_utc": utc(probe.context[0].first_open),
        "first_loaded_replay_open_utc": utc(bars[0]["time"]),
        "observations_after_warmup": len(computed),
        "exact_timestamp_direction_matches": len(original & computed),
        "native_only": describe(original - computed),
        "replay_only": describe(computed - original),
        "scope": "Optional WATCH observation. It creates no entries, fills or risk outcomes.",
    }


def wsb_plans(wsb, bars, settings=None):
    reconstruction = wsb.reconstruct(bars, settings or wsb.Settings())
    decisions = [{**e, "level": (e["top"] if e["side"] > 0 else e["bottom"])} for e in reconstruction["events"]]
    return reconstruction["plans"], decisions


def matching_bar_tape(native, wsb_bars, prefix):
    volumes = {row["time"]: row for row in wsb_bars}
    volume_start = wsb_bars[0]["time"]
    volumes.update((row["time"], row) for row in prefix if row["time"] < volume_start)
    bars = []
    for row in chart_bars(native):
        match = volumes[row["time"]]
        if any(match[key] != value for key, value in row.items()):
            raise ValueError(f"WSB and RC2 OHLC differ at {utc(row['time'])}")
        bars.append({**row, "volume": match["volume"]})
    return bars


def test_panel(bars, frame, event_rules, lanes):
    first, split = bars[WARMUP]["time"], bars[WARMUP + (len(bars) - WARMUP) * 3 // 5]["time"]
    last = bars[-1]["time_close"]
    windows = {"full": (first, last), "early_60": (first, split), "later_40": (split, last)}
    events, categories, audit = independent_hours(bars, frame, event_rules)
    return {
        "bars": len(bars), "first_loaded_open_utc": utc(bars[0]["time"]),
        "windows_utc": {k: {"start": utc(v[0]), "end": utc(v[1])} for k, v in windows.items()},
        "independent_hour_catalog": audit,
        "models": {
            name: evaluate_lane(name, [p for p in plans if p.bar >= WARMUP], decisions, bars,
                                windows, events, categories)
            for name, (plans, decisions) in lanes.items()
        },
    }


def direct_lanes(bars):
    from gold_pro.direct_trend_breakout import MODELS, scan

    return {name: (plans, decisions) for name in sorted(MODELS)
            for decisions, plans in [scan(bars, enabled={name})]}


def wsb_native_overlap(plans, native, bars):
    start, end = bars[WARMUP]["time_close"], bars[-1]["time_close"]
    source = {(row["close_ms"], row["direction"]): tuple(row[key] for key in ("entry", "sl", "tp1", "tp2", "tp3"))
              for row in native["PLAN"] if row["entry"] is not None and start <= row["close_ms"] <= end}
    replayed = {(bars[p.bar]["time_close"], "BUY" if p.side > 0 else "SELL"): (p.reference, p.stop, *p.targets)
                for p in plans if p.bar >= WARMUP}
    mismatches = []
    for key in sorted(source.keys() | replayed.keys()):
        a, b = source.get(key), replayed.get(key)
        if a is None or b is None or any(abs(x - y) > 1e-6 for x, y in zip(a, b)):
            mismatches.append({"decision_utc": utc(key[0]), "direction": key[1], "native": a, "replay": b})
    return {"native_plans": len(source), "replayed_plans": len(replayed), "mismatches": mismatches}


def joined_rc2_case_plans(fixed, pressure, bars, frame):
    indexed = {bar["time_close"]: i for i, bar in enumerate(bars)}
    merged = {}
    for run in (fixed, pressure):
        for plan in native_plans(run, frame, "RC2")[0]:
            stamp = run.context[plan.bar].closing
            if stamp in indexed:
                replayed = replace(plan, bar=indexed[stamp])
                if merged.setdefault(plan.id, replayed) != replayed:
                    raise ValueError(f"RC2 tapes disagree about plan {plan.id}")
    return sorted(merged.values(), key=lambda plan: (plan.bar, plan.id))


def summarize_case(bars, wsb_bars, fixed, pressure, frame):
    from gold_pro.direct_trend_breakout import scan

    seen = {bar["time"]: bar for bar in bars}
    for source in (wsb_bars, chart_bars(pressure)):
        for bar in source:
            if bar["time"] < bars[0]["time"] or bar["time_close"] > CASE_END:
                continue
            old = seen.setdefault(bar["time"], bar)
            if any(old[key] != bar[key] for key in ("time_close", "open", "high", "low", "close")):
                raise ValueError(f"Case bars disagree at {utc(bar['time'])}")
    bars = [seen[key] for key in sorted(seen)]
    validate_bars(bars)
    decisions, plans = scan(bars)
    case = []
    for row in decisions:
        if not CASE_START <= state_stamp(row) <= CASE_END:
            continue
        next_index = row["bar"] + 1
        record = {**row, "known_at_utc": utc(state_stamp(row))}
        if row.get("plan_id") and next_index < len(bars) and bars[next_index]["time"] == state_stamp(row):
            record.update(next_open_utc=utc(bars[next_index]["time"]), next_open_mid=bars[next_index]["open"],
                          next_open_after_spread_and_slippage=bars[next_index]["open"] + row["side"] * (BASE.spread / 2 + BASE.slip))
        case.append(record)
    isolated = [journal(simulate(p, bars, BASE), bars)
                for p in plans if CASE_START <= bars[p.bar]["time_close"] <= CASE_END]
    def case_replay(proposals):
        return [journal(item, bars) for item in portfolio(proposals, bars, BASE)
                if CASE_START <= bars[item.plan.bar]["time_close"] <= CASE_END]

    rc2 = joined_rc2_case_plans(fixed, pressure, bars, frame)
    early = [r for r in early_observations(bars) if CASE_START <= r["known_ms"] <= CASE_END]
    return {"frame_minutes": frame, "first_loaded_open_utc": utc(bars[0]["time"]), "case_start_utc": utc(CASE_START),
            "last_close_utc": utc(bars[-1]["time_close"]), "shadow_confirmed_decisions": case,
            "independent_plan_executions": isolated, "shadow_one_slot_replay": case_replay(plans),
            "rc2_one_slot_replay": case_replay(rc2), "rc2_plus_shadow_one_slot_replay": case_replay([*rc2, *plans]),
            "published_rc2_early_observation_geometry_only": early}


def run():
    from gold_pro.direct_trend_breakout import scan

    event_rules = json.loads(pinned(EVENT_CONFIG))["event"]
    seal_start = json.loads(pinned(FORWARD_SEAL))["forward_start_ms"]
    wsb = wsb_model()
    report = {"purpose": "Already observed historical replay; no promotion or live execution.", "inputs_sha256": PINS,
              "wsb_reference_commit": WSB_COMMIT, "native_shared": {}, "case": {},
              "shadow_source_sha256": sha256((ROOT / "gold_pro/direct_trend_breakout.py").read_bytes()).hexdigest()}
    with (ZipFile(BytesIO(pinned(WSB_ARCHIVE))) as prior, ZipFile(BytesIO(pinned(RC2_ARCHIVE))) as rc2,
          ZipFile(BytesIO(pinned(PRESSURE_ARCHIVE))) as pressure, TemporaryDirectory() as temp):
        folder = Path(temp)
        archive_paths = [archive_file(prior, name, folder) for name in ("archival_m5_bars.csv", "archival_s5_native.csv")]
        for path in archive_paths:
            pinned(path.name, path.read_bytes())
        archived_m5 = load_bars(archive_paths[0])
        m15_prefix = json.loads(pinned(M15_PREFIX))["bars"]
        for frame in (5, 15):
            label = f"M{frame}"
            paths = [archive_file(rc2, f"{prefix}_{label}.csv", folder) for prefix in ("fixed", "parity")]
            fixed, parity = [read_export(path, frame, "audit") for path in paths]
            parity_status = compare_exports(fixed, parity, ("core", "ready"))
            if parity_status["status"] != "MATCH":
                raise ValueError(f"Current RC2 and S5 RAW tapes do not align: {parity_status}")
            if fixed.context[-1].closing >= seal_start:
                raise ValueError("Attempt to score the sealed forward window")
            wsb_raw = archive_file(prior, f"original_{label}.csv", folder)
            wsb_stream, wsb_bars, wsb_frame = wsb.read_native(wsb_raw)
            if wsb_frame != frame:
                raise ValueError(f"Wrong WSB timeframe {wsb_frame}")
            bars = matching_bar_tape(fixed, wsb_bars, archived_m5 if frame == 5 else m15_prefix)
            wsb_lane = wsb_plans(wsb, bars, wsb._native_settings(wsb_stream["SETTINGS"]))
            lanes = {"S5_RAW": native_plans(parity, frame, "S5_RAW"),
                     "RC2": native_plans(fixed, frame, "RC2"), "WSB": wsb_lane,
                     "RC2_EARLY_WATCH_OBSERVATIONS": ([], [{**x, "state": "WATCH"} for x in early_observations(bars)])}
            direct = direct_lanes(bars)
            lanes.update(direct)
            shadow_decisions, shadow_plans = scan(bars)
            lanes["RC2_PLUS_SHADOW"] = ([*lanes["RC2"][0], *shadow_plans], [*lanes["RC2"][1], *shadow_decisions])
            native_properties = {}
            for name, path, native in zip(("fixed", "parity"), paths, (fixed, parity)):
                native_properties[name] = native_checks(path, native, extras(path), frame, MODES[name])
            panel = test_panel(bars, frame, event_rules, lanes)
            panel["native"] = {"shared_core_and_ready": parity_status, "actual_emulator_lanes": native_properties}
            panel["wsb_native_plan_comparison_after_warmup"] = wsb_native_overlap(wsb_lane[0], wsb_stream, bars)
            panel["native"]["wsb_prefix_ohlcv_bars"] = sum(b["time"] < wsb_bars[0]["time"] for b in bars)
            panel["native"]["existing_early_watch"] = compare_early_watch(
                archive_file(rc2, f"research_{label}.csv", folder), bars, frame)
            report["native_shared"][label] = panel
            newest = archive_file(pressure, f"rc2/baseline_audit_{label}.csv", folder)
            report["case"][label] = summarize_case(bars, wsb_bars, fixed, read_export(newest, frame, "audit"), frame)

        bars, tape = archived_m5, inspect(archive_paths[1])
        verify_bars(bars, tape["run"])
        if tape["mode"] != "S5" or bars[-1]["time_close"] >= seal_start:
            raise ValueError("Invalid archival S5 source")
        original = native_outcomes(tape, BASE)
        archive_lanes = {
            "S5_ARCHIVED_ADMITTED": ([replace(x.plan, model="S5_RAW") for x in original], []),
            "WSB": wsb_plans(wsb, bars),
            **direct_lanes(bars),
        }
        report["longer_archival_M5"] = test_panel(bars, 5, event_rules, archive_lanes)
        report["longer_archival_M5"]["scope"] = (
            "S5 uses originally submitted plans only. Native rejected/admission counterfactuals and an RC2 tape "
            "for these dates are unavailable. This panel overlaps the recent sample and is descriptive."
        )
    report["execution"] = {
        "baseline_costs": asdict(BASE), "stress_costs": asdict(STRESS), "start_cash_usd": CAPITAL,
        "max_initial_all_in_risk_usd": MAX_CASH_RISK, "max_open_move_atr": MAX_OPEN_MOVE_ATR,
        "policy": "Market entry at next contiguous open plus half spread and adverse slippage. Signal brackets "
                  "stay frozen. Recheck entry geometry, deviation and budget before taking a simulated fill. "
                  "A single position or reserved order occupies the slot. Process known open gaps, then stop first "
                  "when both extremes are touched. Limits execute at target prices, market and stop prices pay adverse "
                  "slippage. Commission on each unit each way. S5 uses the observed +0.05R lock from the next bar "
                  "after TP1, others keep fixed stops. Report net R on all-in stop risk; drawdown at chart closes. "
                  "These are OHLC scenarios, not executable quotes or native broker fills.",
        "warmup_bars": WARMUP,
        "retrospective_episode_rule": "Use the pinned directional event clock-hour protocol for both directions. "
                                      "Non-directional eligible hours are classified separately as reversal when "
                                      "two half-hour moves oppose, the second dominates and both exceed half the "
                                      "prior 24-hour median range, or sideways when net move is <=35% of range. "
                                      "No episode outcome enters the signal scan or portfolio.",
    }
    return report


def compact(report):
    result = {"execution": report["execution"], "native_shared": {}, "longer_archival_M5": {},
              "shadow_source_sha256": report["shadow_source_sha256"]}
    panels = {**report["native_shared"], "longer_archival_M5": report["longer_archival_M5"]}
    for name, panel in panels.items():
        condensed = {key: value for key, value in panel.items() if key != "models"}
        condensed["models"] = {}
        for model, lane in panel["models"].items():
            rows = lane["next_open"]["opportunities"]
            condensed["models"][model] = {
                "next_open_windows": lane["next_open"]["windows"],
                "next_open_states": lane["next_open"]["states"],
                "stressed_next_open_full": lane["stressed_next_open"]["windows"]["full"],
                "late_minus_next_open": lane["late_minus_next_open"],
                "recall": rows["recall"],
                "regimes_at_entry": rows["regimes_at_entry"],
                "outside_same_side_event": rows["outside_same_side_event"],
                "failed_breakouts": {k: v for k, v in lane["failed_breakouts"].items() if k != "rows"},
                "decision_counts_after_warmup": lane["decision_counts_after_warmup"],
                "rejection_reasons_after_warmup": lane["rejection_reasons_after_warmup"],
            }
        if name == "longer_archival_M5":
            result[name] = condensed
        else:
            result["native_shared"][name] = condensed
    result["case"] = report["case"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--summary", type=Path)
    args = parser.parse_args()
    report = run()
    payload = (json.dumps(report, ensure_ascii=False, allow_nan=False, default=str, separators=(",", ":")) + "\n").encode()
    if args.output.suffix == ".gz":
        with args.output.open("wb") as raw, GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as compressed:
            compressed.write(payload)
    else:
        args.output.write_bytes(payload)
    if args.summary:
        args.summary.write_text(json.dumps(compact(report), indent=2, ensure_ascii=False, allow_nan=False,
                                           default=str) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
