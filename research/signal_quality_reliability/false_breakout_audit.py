"""Evaluate three frozen admission gates on pre-November-11 Direct Trend research offers."""

import argparse
from collections import Counter
from dataclasses import asdict, replace
from gzip import decompress
from hashlib import sha256
from io import BytesIO
import json
from math import isclose
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from gold_pro.archive import native_outcomes
from gold_pro.direct_breakout_research import (
    BASE, EVENT_CONFIG, FORWARD_SEAL, PINS, ROOT, STRESS, WARMUP, WSB_ARCHIVE,
    drawdown, independent_hours, pinned, portfolio, simulate, stats, utc,
)
from gold_pro.direct_trend_breakout import CONSOLIDATION, RETEST, STRUCTURAL, scan
from research.data.loader import load_bars
from unified.tools.audit_premium_s5 import verify_bars
from unified.tools.audit_tv_observations import inspect


HERE = Path(__file__).resolve().parent
PREVIOUS = ROOT / "research/direct_trend_breakout/evidence/historical_replay.json.gz"
PRECISION = ROOT / "docs/precision_evidence/fast_diagnostic.json"
PREVIOUS_SHA = "732a893c5d525b467b206965127415c6f941ad9cb1ecfd1dae34842de7fd5f76"
PRECISION_SHA = "22bc72e05c254f26a63db5fee964969aa1672c9f7dda63094b84483c8134c840"
ARCHIVE_CSV = "archival_m5_bars.csv"
S5_CSV = "archival_s5_native.csv"
DECLINE_DAY = 1794355200000  # 2026-11-11 00:00 UTC
HOUR = 3_600_000
CLEARANCE = .15
BODY = .75
WINDOW = 3
GATES = ("control", "minimum_clearance_0_15_atr", "minimum_body_0_75_atr", "one_more_close_held")
TARGET_MODELS = {STRUCTURAL, CONSOLIDATION}
SIDES = {1: "BUY", -1: "SELL"}


def checked(path, expected):
    data = path.read_bytes()
    if sha256(data).hexdigest() != expected:
        raise ValueError(f"Unexpected archived input: {path}")
    return data


def inputs(archive_bars, s5_log):
    earlier = json.loads(decompress(checked(PREVIOUS, PREVIOUS_SHA)))
    precision = json.loads(checked(PRECISION, PRECISION_SHA))
    sources = ((ARCHIVE_CSV, archive_bars), (S5_CSV, s5_log))
    with TemporaryDirectory() as folder:
        if archive_bars is None or s5_log is None:
            with ZipFile(BytesIO(pinned(WSB_ARCHIVE))) as archive:
                for name, supplied in sources:
                    if supplied is None:
                        (Path(folder) / name).write_bytes(archive.read(name))
        bar_source, journal_source = [supplied or Path(folder) / name for name, supplied in sources]
        pinned(ARCHIVE_CSV, bar_source.read_bytes())
        pinned(S5_CSV, journal_source.read_bytes())
        bars, native = load_bars(bar_source), inspect(journal_source)
    end = bars[-1]["time_close"]
    seal_start = json.loads(pinned(FORWARD_SEAL))["forward_start_ms"]
    if end >= min(DECLINE_DAY, seal_start):
        raise ValueError("The evaluation cannot use case-day or sealed forward bars")
    published = earlier["longer_archival_M5"]
    if published["bars"] != len(bars) or published["windows_utc"]["full"]["end"] != utc(end):
        raise ValueError("Archive and published control do not share the same bars")
    if native["mode"] != "S5" or native["run"]["timeframe"] != "5" or native["unfilled"]:
        raise ValueError("Missing matching completed S5 native evidence")
    verify_bars(bars, native["run"])
    return bars, published, precision, native


def verify_published_outcomes(outcomes, rows):
    if len(rows) != len(outcomes):
        raise ValueError("Registered control count mismatch")
    for outcome, row in zip(outcomes, rows):
        if (outcome.plan.id, outcome.state, outcome.entry) != (row["id"], row["state"], row["entry"]):
            raise ValueError(f"Registered control changed at {row['id']}")
        if not isclose(outcome.net, row["realized_usd"], abs_tol=1e-8):
            raise ValueError(f"Registered control net changed at {row['id']}")


def verify_registered_controls(bars, published):
    counts = {}
    for model in (STRUCTURAL, CONSOLIDATION, RETEST):
        _, plans = scan(bars, enabled={model})
        outcomes = portfolio([plan for plan in plans if plan.bar >= WARMUP], bars, BASE)
        rows = published["models"][model]["next_open"]["journal"]
        verify_published_outcomes(outcomes, rows)
        counts[model] = len(outcomes)
    return counts


def existing_offers(bars):
    events, plans = scan(bars)
    candidates = {row["plan_id"]: row for row in events if row["state"] == "ENTRY CANDIDATE"}
    plans = [plan for plan in plans if plan.bar >= WARMUP]
    for plan in plans:
        row = candidates[plan.id]
        if not row["level_known_ms"] <= row["time_ms"] == bars[plan.bar]["time_close"]:
            raise ValueError(f"Unobservable edge or plan timing: {plan.id}")
    return plans, candidates


def decision(gate, plan, candidate, bars):
    """Return admission, observed decision index and reason at the indicated close."""
    index = plan.bar
    if gate == "control" or plan.model not in TARGET_MODELS:
        return plan, index, "CONTROL"
    bar = bars[index]
    if gate == "minimum_clearance_0_15_atr":
        enough = plan.side * (bar["close"] - candidate["level"]) >= CLEARANCE * plan.atr
        return plan if enough else None, index, "CLEARANCE_PASS" if enough else "THIN_CLEARANCE"
    if gate == "minimum_body_0_75_atr":
        enough = plan.side * (bar["close"] - bar["open"]) >= BODY * plan.atr
        return plan if enough else None, index, "BODY_PASS" if enough else "SMALL_BODY"
    if index + 1 == len(bars):
        return None, None, "AWAITING_CLOSE_AT_CUTOFF"
    held = bars[index + 1]
    if held["time"] != bar["time_close"]:
        return None, index + 1, "SESSION_GAP_BEFORE_HOLD_CLOSE"
    if plan.side * (held["close"] - candidate["level"]) <= 0:
        return None, index + 1, "EDGE_RECLAIMED_AT_HOLD_CLOSE"
    return replace(plan, bar=index + 1), index + 1, "HELD_AT_NEXT_CLOSE"


def audit_reclaim(plan, candidate, bars):
    """Offline label only, never passed into a decision or execution function."""
    index = plan.bar
    future = bars[index + 1:index + WINDOW + 1]
    if len(future) != WINDOW or any(row["time"] != bars[index + j]["time_close"] for j, row in enumerate(future)):
        return None
    return next((utc(row["time_close"]) for row in future if plan.side * (row["close"] - candidate["level"]) <= 0), "")


def outcome_counts(outcomes):
    closed = [x for x in outcomes if x.state == "CLOSED"]
    return {
        "offers": len(outcomes),
        "filled": sum(x.entry_bar is not None for x in outcomes),
        "closed_winners": sum(x.net > 0 for x in closed),
        "closed_losers": sum(x.net < 0 for x in closed),
        "closed_flat": sum(x.net == 0 for x in closed),
        "closed_net_usd": sum(x.net for x in closed),
        "states": dict(Counter(x.state for x in outcomes)),
    }


def matched_fills(outcomes, bars, events):
    directional = {(event["start"], event["side"]) for event in events}
    matched = set()
    for outcome in outcomes:
        if outcome.entry_bar is not None:
            when = bars[outcome.entry_bar]["time"] // HOUR * HOUR
            if (when, outcome.plan.side) in directional:
                matched.add((when, outcome.plan.side))
    return matched


def study(bars, plans, candidates, rules, split):
    start, end = bars[WARMUP]["time"], bars[-1]["time_close"]
    events, _, _ = independent_hours(bars, 5, rules)
    events = [event for event in events if start <= event["start"] and event["start"] + HOUR <= end]
    control = {name: portfolio(plans, bars, cost) for name, cost in (("base", BASE), ("stress", STRESS))}
    isolated = {name: {plan.id: simulate(plan, bars, cost) for plan in plans}
                for name, cost in (("base", BASE), ("stress", STRESS))}
    targets = [plan for plan in plans if plan.model in TARGET_MODELS]
    reclaims = {plan.id: audit_reclaim(plan, candidates[plan.id], bars) for plan in targets}
    report = {}
    for gate in GATES:
        accepted = []
        gate_rows = {}
        for plan in plans:
            admitted, known, reason = decision(gate, plan, candidates[plan.id], bars)
            if admitted is not None:
                accepted.append(admitted)
            if plan.model in TARGET_MODELS:
                gate_rows[plan.id] = (admitted, known, reason)
        blocked = {plan_id for plan_id, (admitted, _, _) in gate_rows.items() if admitted is None}
        # On every historical prefix, only outcomes already known by its end may match.
        for size in (len(bars) // 2, len(bars) * 3 // 4, len(bars) - 1):
            for plan in targets:
                if plan.bar >= size:
                    continue
                observed = decision(gate, plan, candidates[plan.id], bars[:size])
                whole = gate_rows[plan.id]
                if whole[1] is not None and whole[1] < size and observed != whole:
                    raise ValueError(f"Gate differs on available prefix: {gate} {plan.id}")
        rows = {}
        for name, cost in (("base", BASE), ("stress", STRESS)):
            scenario = portfolio(accepted, bars, cost)
            scenario_entries = {item.plan.id: item for item in scenario if item.entry_bar is not None}
            original_entries = {item.plan.id: item for item in control[name] if item.entry_bar is not None}
            both = scenario_entries.keys() & original_entries.keys()
            kept_but_repriced = sorted(pid for pid in both
                                       if scenario_entries[pid].entry_bar != original_entries[pid].entry_bar)
            control_fills_lost = sorted(original_entries.keys() - scenario_entries.keys())
            new_fills = sorted(scenario_entries.keys() - original_entries.keys())
            original_hits = matched_fills(control[name], bars, events)
            revised_hits = matched_fills(scenario, bars, events)
            encode_hours = lambda hits: [dict(start_utc=utc(at), side=SIDES[side]) for at, side in sorted(hits)]
            rows[name] = {
                "all_offers": outcome_counts(scenario),
                "metrics": {window: {side: stats(scenario, bars, a, b, direction)
                                     for side, direction in (("all", 0), ("BUY", 1), ("SELL", -1))}
                            for window, (a, b) in {"full": (start, end), "early_60": (start, split),
                                                   "later_40": (split, end)}.items()},
                "blocked_offers_isolated_original_outcomes": outcome_counts([isolated[name][pid] for pid in sorted(blocked)]),
                "lost_control_fills": outcome_counts([original_entries[pid] for pid in control_fills_lost]),
                "lost_control_fill_ids": control_fills_lost,
                "newly_admitted_fills": outcome_counts([scenario_entries[pid] for pid in new_fills]),
                "newly_admitted_fill_ids": new_fills,
                "retained_but_delayed_fill_ids": kept_but_repriced,
                "same_side_directional_hours_hit": encode_hours(revised_hits),
                "same_side_directional_hours_lost": encode_hours(original_hits - revised_hits),
                "same_side_directional_hours_new": encode_hours(revised_hits - original_hits),
            }
        report[gate] = {
            "tested_breakout_offers": len(targets), "allowed_breakout_offers": len(targets) - len(blocked),
            "unchanged_retest_offers": len(plans) - len(targets),
            "gate_disposition": dict(Counter(reason for _, _, reason in gate_rows.values())),
            "false_break_label_eligible": sum(value is not None for value in reclaims.values()),
            "false_break_label_observed": sum(bool(value) for value in reclaims.values()),
            "false_breaks_removed": sum(bool(reclaims[pid]) for pid in blocked),
            "false_breaks_remained": sum(bool(value) for pid, value in reclaims.items() if pid not in blocked),
            "unresolved_break_labels_among_removed": sum(reclaims[pid] is None for pid in blocked),
            "outcomes": rows,
            "removed_offer_journal": [
                {
                    "id": pid, "known_utc": utc(candidates[pid]["time_ms"]),
                    "gate_decision_utc": utc(bars[gate_rows[pid][1]]["time_close"]) if gate_rows[pid][1] is not None else None,
                    "side": SIDES[candidates[pid]["side"]], "reason": gate_rows[pid][2],
                    "edge_reclaim_utc": reclaims[pid] or None,
                    "edge_reclaim_observable_within_three": reclaims[pid] is not None,
                    "isolated_control_state": isolated["base"][pid].state,
                    "isolated_control_net_usd": isolated["base"][pid].net if isolated["base"][pid].state == "CLOSED" else None,
                } for pid in sorted(blocked)
            ],
        }
    return {
        "registered_directional_hours_by_side": dict(Counter(SIDES[row["side"]] for row in events)),
        "gates": report,
    }


def precision_check(precision):
    """Existing executed FAST control only. No occupancy simulation or other FAST entry models."""
    closed = [row for row in precision["campaigns"] if row["state"] == "CLOSED" and row["model"] == "BREAKOUT"]
    blocked = [row for row in closed if row["anchor_distance_at_signal_atr"] < CLEARANCE]
    def summary(rows):
        return {
            "closed": len(rows),
            "winners": sum(row["net_usd"] > 0 for row in rows),
            "losers": sum(row["net_usd"] < 0 for row in rows),
            "base_net_usd": sum(row["net_usd"] for row in rows),
            "stress_net_usd": sum(row["stress_net_usd"] for row in rows),
            "retrospective_precision_false_breakout_tag": sum("FALSE_BREAKOUT" in row["diagnostic_flags"] for row in rows),
        }
    return {
        "scope": "Fixed originally filled FAST BREAKOUT campaigns. Threshold transferred without refitting. Same underlying M5 market period, different engine and cost semantics. No open/occupancy backfill or unseen evaluation.",
        "source_sha256": PRECISION_SHA,
        "all_closed": summary(closed),
        "blocked_under_minimum_signal_anchor_clearance": summary(blocked),
        "retained": summary([row for row in closed if row not in blocked]),
        "removed_ids": [row["campaign_id"] for row in blocked],
    }


def archived_s5_check(bars, published, native):
    """Keep the matched archived S5 source in a separate reference lane."""
    start, end = bars[WARMUP]["time"], bars[-1]["time_close"]
    original = native_outcomes(native, BASE)
    plans = [replace(row.plan, model="S5_RAW") for row in original if row.plan.bar >= WARMUP]
    ledger = {cost: portfolio(plans, bars, inputs, use_s5_lock=True)
              for cost, inputs in (("base", BASE), ("stress", STRESS))}
    verify_published_outcomes(ledger["base"], published["models"]["S5_ARCHIVED_ADMITTED"]["next_open"]["journal"])
    filled = [row for row in original if start <= bars[row.entry_bar]["time"] < end]
    closed = sorted((row for row in filled if row.state == "CLOSED"), key=lambda row: (row.release, row.plan.id))
    modeled_spread_net = [row.net for row in closed]
    native_cash = [row.net + BASE.spread * BASE.pointvalue * row.plan.units for row in closed]
    running = 0.
    marks = []
    for profit in native_cash:
        running += profit
        marks.append(running)
    return {
        "scope": "Same matched OHLC and dates, immutable archived native S5. Originally submitted plans only, no RC2 tape and no rejected or unsubmitted counterfactuals. Separate reference; no policy gates tested on S5 and no combined-slot results.",
        "journal_sha256": PINS[S5_CSV],
        "native_ohlc_fingerprints": [native["run"]["checksum1"], native["run"]["checksum2"]],
        "matched_native_fills": len(filled),
        "matched_native_closed": len(closed),
        "native_closed_cash_usd": sum(native_cash),
        "native_closed_cash_with_additional_modeled_base_spread_usd": sum(modeled_spread_net),
        "native_campaign_close_drawdown_usd": drawdown(marks),
        "reproduced_published_admitted_offer_count": len(plans),
        "fixed_admitted_replay": {
            cost: {side: stats(outcomes, bars, start, end, direction)
                   for side, direction in (("all", 0), ("BUY", 1), ("SELL", -1))}
            for cost, outcomes in ledger.items()
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-bars", type=Path, help="Optional exact SHA-pinned archival_m5_bars.csv")
    parser.add_argument("--s5-log", type=Path, help="Optional exact SHA-pinned archival_s5_native.csv")
    parser.add_argument("--output", type=Path, default=HERE / "false_breakout_evidence.json")
    args = parser.parse_args()
    bars, published, precision, native = inputs(args.archive_bars, args.s5_log)
    reproduced = verify_registered_controls(bars, published)
    plans, candidates = existing_offers(bars)
    split = bars[WARMUP + (len(bars) - WARMUP) * 3 // 5]["time"]
    report = {
        "schema": "qirvanta.false-breakout-admission-audit.v1",
        "source": "Historical OHLC research on OANDA:XAUUSD M5",
        "sample": {"bar_count": len(bars), "archive_sha256": PINS[ARCHIVE_CSV],
                   "published_control_sha256": PREVIOUS_SHA, "loaded_open_utc": utc(bars[0]["time"]),
                   "scored_first_open_utc": utc(bars[WARMUP]["time"]), "final_close_utc": utc(bars[-1]["time_close"]),
                   "chronological_descriptive_split_utc": utc(split), "case_date_and_forward_seal_excluded": True},
        "hypotheses": {
            "minimum_clearance_0_15_atr": "Directional distance from the known breakout edge to the confirmation close must be at least 0.15 prior ATR.",
            "minimum_body_0_75_atr": "Directional confirmation candle body must be at least 0.75 prior ATR.",
            "one_more_close_held": "Next contiguous completed close must still be beyond the original edge. No earlier reservation or trade; submit then for following open with original geometry and original deviation cap.",
        },
        "control": "Original all-model Shadow scanner, existing approved diagnostic offers only, grouped one slot. Compare each frozen gate independently. Retests unchanged. Reproduce previously registered independent per-model journals first.",
        "limit": "All dates archival and same market. No S5/native changes, optimization or independent forward evidence. Retrospective three-close reclaim is an evaluation label only. Closed campaign P&L excludes censored outcomes. Stress changes only execution costs, preserving original offers. Chronological sections are descriptive. No statistical significance or promotion claim.",
        "reproduced_prior_per_model_control_counts": reproduced,
        "costs": {"base": asdict(BASE), "stress": asdict(STRESS)},
        "direct_trend": study(bars, plans, candidates, json.loads(pinned(EVENT_CONFIG))["event"], split),
        "archived_s5_separate_reference": archived_s5_check(bars, published, native),
        "precision_fast_cross_check": precision_check(precision),
    }
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                           encoding="utf-8", newline="\n")
    print(f"Audited {len(plans)} frozen Direct Trend offers on {len(bars)} pinned M5 bars.")


if __name__ == "__main__":
    main()

