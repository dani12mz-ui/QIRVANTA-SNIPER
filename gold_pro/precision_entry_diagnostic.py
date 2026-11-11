"""Reconstruct the observed RC2 + Direct Trend Shadow losses on pinned OANDA tapes."""

from __future__ import annotations

import argparse
from collections import Counter
import csv
from dataclasses import asdict, replace
from datetime import datetime
from gzip import decompress
from hashlib import sha256
from io import BytesIO, StringIO
import json
from math import isclose
from pathlib import Path
from statistics import mean, median
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from gold_pro import direct_breakout_research as old
from gold_pro.direct_trend_breakout import MAX_HOLD, MIN_NET_ROOM_R, RETEST, UNITS, scan
from gold_pro.rc1_qa import compare_exports, read_export
from gold_pro.replay import Plan
from research.data.loader import load_bars


PRIOR = "research/direct_trend_breakout/evidence/"
REPLAY = PRIOR + "historical_replay.json.gz"
TRACE = PRIOR + "base-native-logs.zip"
PINS = {
    REPLAY: "732a893c5d525b467b206965127415c6f941ad9cb1ecfd1dae34842de7fd5f76",
    TRACE: "60ee8e06a5a54d2871903fd8eb33cd79d980be4b7ada386cef316fdc631d9a68",
}
TERMINALS = {"CANCELLED", "EXPIRED", "REJECTED", "CONFIRMED"}


def pinned_local(path):
    raw = (old.ROOT / path).read_bytes()
    if sha256(raw).hexdigest() != PINS[path]:
        raise ValueError(f"Diagnostic input changed: {path}")
    return raw


def epoch(iso):
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def matched_tapes():
    """Use the same exact loaded context, OHLC and archived READY streams as the old report."""
    wsb = old.wsb_model()
    with (
        ZipFile(BytesIO(old.pinned(old.WSB_ARCHIVE))) as previous,
        ZipFile(BytesIO(old.pinned(old.RC2_ARCHIVE))) as native,
        TemporaryDirectory() as temporary,
    ):
        folder = Path(temporary)
        archive_path = old.archive_file(previous, "archival_m5_bars.csv", folder)
        old.pinned(archive_path.name, archive_path.read_bytes())
        prefixes = {5: load_bars(archive_path), 15: json.loads(old.pinned(old.M15_PREFIX))["bars"]}
        for frame in (5, 15):
            fixed, raw = (
                read_export(old.archive_file(native, f"{label}_M{frame}.csv", folder), frame, "audit")
                for label in ("fixed", "parity")
            )
            parity = compare_exports(fixed, raw, ("core", "ready"))
            if parity["status"] != "MATCH":
                raise ValueError(f"Archived S5 and RC2 M{frame} source streams diverged")
            _, wsb_bars, wsb_frame = wsb.read_native(old.archive_file(previous, f"original_M{frame}.csv", folder))
            if wsb_frame != frame:
                raise ValueError("Wrong WSB timeframe")
            bars = old.matching_bar_tape(fixed, wsb_bars, prefixes[frame])
            seal = json.loads(old.pinned(old.FORWARD_SEAL))["forward_start_ms"]
            if bars[-1]["time_close"] >= seal:
                raise ValueError("Diagnostic overlaps the sealed period")
            plans, _ = old.native_plans(fixed, frame, "RC2")
            yield frame, bars, plans, parity


def scored_closed(outcome, bars, start, end):
    return (outcome.entry_bar is not None and outcome.state == "CLOSED"
            and start <= bars[outcome.entry_bar]["time"] < end
            and bars[outcome.release]["time_close"] <= end)


def cohort(outcomes):
    outcomes = list(outcomes)
    values = [x.net for x in outcomes]
    losers = [value for value in values if value < 0]
    return {
        "closed": len(values), "wins": sum(value > 0 for value in values), "losses": len(losers),
        "net_usd": round(sum(values), 9),
        "net_r": round(sum(x.net / x.risk_usd for x in outcomes), 9),
        "mean_loss_usd": round(mean(losers), 9) if losers else None,
    }


def stopped_path(outcome, decision, bars, cutoff):
    """Post-outcome description, never a signal input. Resolve the stopped bar conservatively."""
    plan = outcome.plan
    first = outcome.entry_bar
    side, half = plan.side, old.BASE.spread / 2
    stop_legs = [part for part in outcome.exits if part["reason"] in {"SL", "SL_GAP"}]
    stop_bar = stop_legs[0]["bar"] if stop_legs else None
    # A stopped bar's other extreme cannot prove favorable movement preceded the SL.
    last_favorable = stop_bar - 1 if stop_bar is not None else outcome.release
    favorable = max(
        (side * ((bar["high"] if side > 0 else bar["low"]) - side * half - outcome.entry)
         for bar in bars[first:last_favorable + 1]),
        default=0.,
    )
    ambiguous = False
    if stop_legs and stop_legs[0]["reason"] == "SL":
        bar = bars[stop_bar]
        favorable_at_open = side * (bar["open"] - side * half)
        favorable_at_extreme = side * ((bar["high"] if side > 0 else bar["low"]) - side * half)
        ambiguous = any(favorable_at_open < side * plan.targets[part["leg"] - 1] <= favorable_at_extreme
                        for part in stop_legs)
    risk_distance = side * (outcome.entry - plan.stop)
    obstacle = decision["obstacle"]
    exit_friction = half + old.BASE.slip + 2 * old.BASE.commission / old.BASE.pointvalue
    room_at_fill = (
        (min(risk_distance, side * (obstacle - outcome.entry)) - exit_friction) / risk_distance
        if obstacle is not None else None
    )
    costless = replace(old.BASE, spread=0., ticks=0, commission=0.)
    counterfactual = old.simulate(plan, bars, costless)
    costless_closed_in_window = (counterfactual.state == "CLOSED"
                                 and bars[counterfactual.release]["time_close"] <= cutoff)
    return {
        "stopped_legs": len(stop_legs),
        "stopped_on_entry_bar": stop_bar == first,
        "same_bar_sl_tp_ambiguity": ambiguous,
        "favorable_move_before_stopped_bar_in_initial_r": round(max(0., favorable) / risk_distance, 9),
        "entry_repricing_atr": round(side * (outcome.entry - plan.reference) / plan.atr, 9),
        "nearest_obstacle_net_room_at_fill_r": round(room_at_fill, 9) if room_at_fill is not None else None,
        "round_trip_commission_usd": round(2 * old.BASE.commission * plan.units, 9),
        "zero_friction_isolated_replay_state": (
            "CROSSES_CUTOFF" if counterfactual.state == "CLOSED" and not costless_closed_in_window
            else counterfactual.state
        ),
        "zero_friction_isolated_net_usd": (
            round(counterfactual.net, 9) if costless_closed_in_window else None
        ),
    }


def structural_path(plan, decision, bars):
    """Keep frozen geometry separate from the subsequent three-bar outcome label."""
    bar = bars[plan.bar]
    side = plan.side
    future = bars[plan.bar + 1:plan.bar + 4]
    if len(future) != 3 or any(
        later["time"] != earlier["time_close"] for earlier, later in zip([bar, *future], future)
    ):
        future = []
    reclaim = next(
        (item for item in future if side * (item["close"] - decision["level"]) <= 0), None
    )
    interval = bar["time_close"] - bar["time"]
    origin = decision["breakout_ms"]
    return {
        "known_at_confirmation": {
            "watch_utc": old.utc(decision["watch_ms"]),
            "source_level_known_utc": old.utc(decision["level_known_ms"]),
            "original_breakout_utc": old.utc(origin),
            "source_level": decision["level"],
            "risk_atr": round(decision["risk"] / plan.atr, 9),
            "net_room_r": decision["net_room_r"],
            "nearest_known_obstacle": decision["obstacle"],
            "extension_atr": decision["extension_atr"],
            "friction_r": decision["friction_r"],
            "bar_range_atr": round((bar["high"] - bar["low"]) / plan.atr, 9),
            "watch_to_confirmation_minutes": (bar["time_close"] - decision["watch_ms"]) / 60_000,
            "bars_from_original_breakout_to_retest": (bar["time_close"] - origin) // interval
            if plan.model == RETEST else None,
        },
        "later_diagnostic": {
            "three_contiguous_bars_observed": bool(future),
            "reclaimed_level_by_utc": old.utc(reclaim["time_close"]) if reclaim else None,
        },
    }


def gate_audit(observations, bars, start, end):
    """Evaluate old rejected geometry in isolation without changing the historical portfolio."""
    rows = []
    no_geometry = Counter()
    for item in observations:
        known = item["time_ms"]
        if item["state"] != "REJECTED" or not start <= known < end:
            continue
        reasons = item.get("reasons") or [item["reason"]]
        if not item.get("targets") or not item.get("plan_id"):
            no_geometry.update(reasons)
            continue
        plan = Plan(item["plan_id"], item["model"], item["side"], item["bar"], item["reference"],
                    item["stop"], item["targets"], item["prior_atr"], MAX_HOLD, UNITS)
        replay = old.simulate(plan, bars, old.BASE)
        closed = scored_closed(replay, bars, start, end)
        rows.append({
            "known_utc": old.utc(known),
            "reasons": reasons,
            "risk_atr": round(item["risk"] / item["prior_atr"], 9),
            "extension_atr": item["extension_atr"],
            "net_room_r": item["net_room_r"],
            "isolated_result_in_window": (
                "WIN" if replay.net > 0 else "LOSS" if replay.net < 0 else "FLAT"
            ) if closed else replay.state if replay.state != "CLOSED" else "CROSSES_CUTOFF",
            "isolated_closed_usd": round(replay.net, 9) if closed else None,
            "isolated_closed_r": round(replay.net / replay.risk_usd, 9) if closed else None,
            "hypothetical_plan": old.journal(replay, bars),
        })
    by_reason = {}
    for reason in sorted({reason for row in rows for reason in row["reasons"]} | no_geometry.keys()):
        all_rows = [row for row in rows if reason in row["reasons"]]
        sole = [row for row in all_rows if len(row["reasons"]) == 1]
        by_reason[reason] = {
            "all": dict(Counter(row["isolated_result_in_window"] for row in all_rows)),
            "sole_reason": dict(Counter(row["isolated_result_in_window"] for row in sole)),
            "sole_reason_closed_usd": round(sum(row["isolated_closed_usd"] or 0. for row in sole), 9),
            "sole_reason_closed_r": round(sum(row["isolated_closed_r"] or 0. for row in sole), 9),
            "without_frozen_brackets": no_geometry[reason],
        }
    return {
        "by_reason": by_reason,
        "rows": rows,
        "interpretation": "Isolated counterfactual with the original next-open checks and frozen brackets. "
                          "Reasons can overlap. Sole-reason counts are distinct from joint exclusions. "
                          "These sums ignore portfolio occupancy and do not establish gate efficacy on unseen data.",
    }


def frame_diagnostic(frame, bars, rc2_plans, parity, reference, event_rules):
    bounds = reference["windows_utc"]["full"]
    start, end = (epoch(bounds[key]) for key in ("start", "end"))
    observations, shadow_plans = scan(bars)
    candidates = {row["plan_id"]: row for row in observations if row["state"] == "ENTRY CANDIDATE"}
    rc2_plans = [plan for plan in rc2_plans if plan.bar >= old.WARMUP]
    shadow_plans = [plan for plan in shadow_plans if plan.bar >= old.WARMUP]
    full_set = [*rc2_plans, *shadow_plans]
    rc2, combined = (old.portfolio(plans, bars, old.BASE) for plans in (rc2_plans, full_set))
    for name, outcomes in (("RC2", rc2), ("RC2_PLUS_SHADOW", combined)):
        replay = json.loads(json.dumps([old.journal(item, bars) for item in outcomes]))
        if replay != reference["models"][name]["next_open"]["journal"]:
            raise ValueError(f"M{frame} does not reproduce the frozen {name} execution journal")

    baseline_closed = {item.plan.id: item for item in rc2 if scored_closed(item, bars, start, end)}
    combined_closed = {item.plan.id: item for item in combined if scored_closed(item, bars, start, end)}
    combined_by_id = {item.plan.id: item for item in combined}
    baseline_by_id = {item.plan.id: item for item in rc2}
    dropped = [baseline_closed[key] for key in baseline_closed if key not in combined_closed]
    added = [item for key, item in combined_closed.items() if item.plan.model == "RC2" and key not in baseline_closed]
    shadow = [item for item in combined_closed.values() if item.plan.model != "RC2"]
    base_net = sum(item.net for item in baseline_closed.values())
    combined_net = sum(item.net for item in combined_closed.values())
    effect = sum(item.net for item in [*shadow, *added]) - sum(item.net for item in dropped)
    if not isclose(combined_net - base_net, effect, abs_tol=1e-9):
        raise ValueError("Portfolio attribution does not reconcile")

    _, regimes, _ = old.independent_hours(bars, frame, event_rules)
    entries = []
    for item in shadow:
        plan = item.plan
        row = old.journal(item, bars)
        row.update(structural_path(plan, candidates[plan.id], bars))
        row["later_diagnostic"].update(stopped_path(item, candidates[plan.id], bars, end))
        row["later_diagnostic"]["realized_hour_regime"] = regimes.get(
            bars[item.entry_bar]["time"] // old.HOUR * old.HOUR, "OUTSIDE_REGISTERED_HOUR"
        )
        entries.append(row)
    losses = [row for row in entries if row["realized_usd"] < 0]
    wins = [row for row in entries if row["realized_usd"] > 0]
    geometry = {}
    for name, group in (("wins", wins), ("losses", losses)):
        geometry[name] = {
            "median_stop_atr_at_confirmation": median(
                row["known_at_confirmation"]["risk_atr"] for row in group
            ) if group else None,
            "median_extension_atr_at_confirmation": median(
                row["known_at_confirmation"]["extension_atr"] for row in group
            ) if group else None,
            "nearest_known_obstacle": sum(row["known_at_confirmation"]["nearest_known_obstacle"] is not None for row in group),
            "known_obstacle_room_below_original_gate_after_fill": sum(
                room is not None and room < MIN_NET_ROOM_R for room in
                (row["later_diagnostic"]["nearest_obstacle_net_room_at_fill_r"] for row in group)
            ),
            "exit_legs_by_reason": dict(Counter(leg["reason"] for row in group for leg in row["parts"])),
        }
    counterexamples = {}
    for name, predicate in (
        ("BUY", lambda row: row["side"] == 1),
        ("SELL", lambda row: row["side"] == -1),
        ("RANGING", lambda row: row["later_diagnostic"]["realized_hour_regime"] == "SIDEWAYS"),
        ("VOLATILE_AT_CONFIRMATION", lambda row: row["known_at_confirmation"]["bar_range_atr"] >= 1.5),
        ("FAILED_BREAKOUT", lambda row: row["later_diagnostic"]["reclaimed_level_by_utc"] is not None),
    ):
        candidates_in_group = [row for row in entries if predicate(row)]
        counterexamples[name] = {
            "closed": len(candidates_in_group),
            "winning_id": next((row["id"] for row in candidates_in_group if row["realized_usd"] > 0), None),
            "losing_id": next((row["id"] for row in candidates_in_group if row["realized_usd"] < 0), None),
        }

    return {
        "scored_window_utc": bounds,
        "loaded_bars": len(bars),
        "native_s5_rc2_signal_parity": parity,
        "prior_replay": "Exact frozen RC2 and combined order journals reproduced",
        "combined_effect_usd": round(effect, 9),
        "attribution": {
            "rc2_alone": cohort(baseline_closed.values()),
            "combined": cohort(combined_closed.values()),
            "admitted_shadow": cohort(shadow),
            "admitted_shadow_by_model": {
                model: cohort(item for item in shadow if item.plan.model == model)
                for model in sorted({item.plan.model for item in shadow})
            },
            "admitted_shadow_by_direction": {
                label: cohort(item for item in shadow if item.plan.side == side)
                for side, label in ((1, "BUY"), (-1, "SELL"))
            },
            "displaced_baseline": [
                {"standalone": old.journal(item, bars),
                 "combined": old.journal(combined_by_id[item.plan.id], bars)}
                for item in dropped
            ],
            "new_rc2": [
                {"standalone": old.journal(baseline_by_id[item.plan.id], bars),
                 "combined": old.journal(item, bars)}
                for item in added
            ],
        },
        "admitted_shadow_paths": entries,
        "admitted_shadow_geometry_by_outcome": geometry,
        "retest_lifecycle_counts": dict(Counter(
            row["state"] for row in observations if row["model"] == RETEST and start <= row["time_ms"] < end
        )),
        "shadow_loss_descriptions": {
            "closed_losses": len(losses),
            "entry_bar_stop": sum(row["later_diagnostic"]["stopped_on_entry_bar"] for row in losses),
            "same_bar_sl_tp_ambiguity": sum(row["later_diagnostic"]["same_bar_sl_tp_ambiguity"] for row in losses),
            "confirmed_level_reclaimed_in_three_bars": sum(
                row["later_diagnostic"]["reclaimed_level_by_utc"] is not None for row in losses
            ),
            "observable_three_bar_path": sum(row["later_diagnostic"]["three_contiguous_bars_observed"]
                                            for row in losses),
            "retest_losses": sum(row["model"] == RETEST for row in losses),
            "friction_counterfactual_wins": sum(
                (row["later_diagnostic"]["zero_friction_isolated_net_usd"] or 0.) > 0. for row in losses
            ),
            "note": "All accepted shadow plans passed the historical close-time risk and room gates. "
                    "The loss labels are observations after execution, not identified causal filters.",
        },
        "counterexamples": counterexamples,
        "old_shadow_rejected_geometry": gate_audit(observations, bars, start, end),
        "stress_same_frozen_plan_universe": {
            "rc2": old.stats(old.portfolio(rc2_plans, bars, old.STRESS), bars, start, end),
            "combined": old.stats(old.portfolio(full_set, bars, old.STRESS), bars, start, end),
        },
    }


def bearish_s5_buy_watches(archive, frame):
    """Keep BUY direction and use only the native known-at H1/H4 snapshot for classification."""
    path = f"indicator-default-M{frame}.csv"
    rows = csv.DictReader(StringIO(archive.read(path).decode("utf-8-sig")))
    trace, core, ready_ids = {}, [], set()
    for row in rows:
        tag, separator, body = row["Message"].partition("|")
        if not separator or tag not in {"DT_BASE", "RC1_CORE", "RC1_READY"}:
            continue
        data = json.loads(body)
        if tag == "DT_BASE":
            if data["frame"] != str(frame) or data["symbol"] != "OANDA:XAUUSD":
                raise ValueError("Wrong native trace context")
            trace[data["known_ms"]] = data
        elif tag == "RC1_CORE":
            if data["strategy_key"] == "S5" and data["side"] == 1 and data["book"] == "CORE":
                core.append(data)
        else:
            ready_ids.add(data["source_id"])
    buy_watches = [item for item in core if item["event"] == "WATCH"]
    bearish = []
    for watch in buy_watches:
        known = watch["known_time"]
        context = trace[known]
        if (context["h1_bias"], context["h4_bias"]) != (-1, -1):
            continue
        previous = trace.get(context["open_ms"])
        valid_level = (context["prior20_low"] == watch["price"] and context["close"] < watch["price"])
        htf_already_known = all(context[field] is not None and context[field] <= context["open_ms"]
                                for field in ("h1_known_ms", "h4_known_ms"))
        if not valid_level or not htf_already_known or (previous and previous["close"] < watch["price"]):
            raise ValueError("S5 BUY WATCH did not match the native downside probe geometry")
        follow = next(
            (item for item in core if item["known_time"] > known and item["event"] in TERMINALS
             and (item["plan_id"] == watch["plan_id"] or item["plan_id"].startswith(watch["plan_id"] + "|"))),
            None,
        )
        bearish.append({
            "direction": "BUY", "watch_utc": old.utc(known), "watch_close": context["close"],
            "prior_range_low": watch["price"], "h1_known_utc": old.utc(context["h1_known_ms"]),
            "h4_known_utc": old.utc(context["h4_known_ms"]),
            "watch_event_id": watch["event_id"], "terminal": follow["event"] if follow else "NOT_OBSERVED_BY_CUTOFF",
            "terminal_utc": old.utc(follow["known_time"]) if follow else None,
            "terminal_reason": follow["reason"] if follow else None,
            "ready_payload_observed": bool(follow and follow["event_id"] in ready_ids),
        })
    return {
        "trace_member": path, "native_buy_watches_total": len(buy_watches),
        "bearish_h1_h4_buy_watches": len(bearish),
        "terminal_counts": dict(Counter(item["terminal"] for item in bearish)), "rows": bearish,
        "interpretation": "The S5 BUY watch starts at a downside range breach and awaits a reclaim. "
                          "H1/H4 describe the observation. A watch does not constitute a BUY or SELL entry.",
    }


def run():
    archive = json.loads(decompress(pinned_local(REPLAY)))
    rules = json.loads(old.pinned(old.EVENT_CONFIG))["event"]
    return {
        "status": "RETROSPECTIVE_DIAGNOSTIC",
        "input_sha256": {**PINS, **old.PINS},
        "simulation": {
            "normal_costs": asdict(old.BASE),
            "stress_costs": asdict(old.STRESS),
            "native_warmup_bars": old.WARMUP,
            "max_open_move_atr": old.MAX_OPEN_MOVE_ATR,
            "cash_risk_cap": old.MAX_CASH_RISK,
            "initial_capital": old.CAPITAL,
            "source": "OANDA:XAUUSD exported OHLC and native archived READY events",
            "execution": "Next open, one-position occupancy, conservative stop-first handling of intrabar ambiguity",
        },
        "historical_matched_windows": {
            f"M{frame}": frame_diagnostic(frame, bars, plans, parity, archive["native_shared"][f"M{frame}"], rules)
            for frame, bars, plans, parity in matched_tapes()
        },
        "countertrend_buy_watch_november_11": countertrend_audit(),
    }


def countertrend_audit():
    with ZipFile(BytesIO(pinned_local(TRACE))) as archive:
        return {f"M{frame}": bearish_s5_buy_watches(archive, frame) for frame in (5, 15)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes((json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2, sort_keys=True)
                             + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
