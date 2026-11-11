"""Reconcile archived S5 and evaluate V2.2 models on the same OANDA bars."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import asdict
from decimal import Decimal
import gzip
import json
from pathlib import Path
from statistics import mean, median

from diagnostics.accounting.audit import closed_drawdown, json_default, stats
from research.data.loader import load_bars
from research.validation.native_report import utc
from research.validation.statistics import holm, sign_flip_p
from unified.tools.audit_premium_s5 import verify_bars
from unified.tools.audit_premium_v2_quant import read_pinned
from unified.tools.audit_tv_observations import inspect
from unified.tools.build_tradingview_final import ROOT, sha
from unified.tools.evaluate_v21_impulse import HOUR, excursion, hourly_events, write_json
from unified.tools.evaluate_v22_execution import POLICIES, ZERO, make_plan, replay, s5_campaigns, simulate
from unified.tools.evaluate_v22_signals import MODELS, feature_rows, scan

REGISTRATION = ROOT / "docs/v22_evidence/historical_registration.json"
LANES = ("S5", *MODELS, "ABC", "S5+ABC")
DAY = 24 * HOUR
WEEK = 7 * DAY
MONDAY_EPOCH = 4 * DAY
SIDES = {"BUY": 1, "SELL": -1}


def dayslice(t):
    return t // DAY


def stress(trade, config, pointvalue):
    costs = config["stress"]
    extra_spread = Decimal(str(costs["incremental_round_trip_spread_price"]))
    extra_slip = Decimal(str(costs["incremental_adverse_slip_price_per_fill"]))
    extra_fee = Decimal(str(costs["incremental_commission_fraction"]))
    return ((extra_spread + 2 * extra_slip) * trade.units * pointvalue
            + extra_fee * sum((part.commission for part in trade.exits), ZERO))


def md(values):
    values = list(values)
    return median(values) if values else None


def ratio(a, b):
    return a / b if b else None


def longest_loss_sequence(values):
    longest = streak = 0
    for v in values:
        streak = streak + 1 if v < 0 else 0
        longest = max(longest, streak)
    return longest


def economics(trades, events, config, pointvalue, bar_count):
    closed = sorted((t for t in trades if t.complete), key=lambda t: (t.exits[-1].bar, t.id))
    observed_positions = [t for t in trades if t.entry_time is not None]
    equity = [t.cash for t in closed]
    stressed = [t.cash - stress(t, config, pointvalue) for t in closed]
    event_side = {e["start"] // HOUR: e["side"] for e in events}
    outside = [t.cash for t in closed if event_side.get(t.entry_time // HOUR) != t.side]
    risk = [t.initial_risk for t in closed if t.initial_risk is not None]
    net_r = [t.cash / t.initial_risk for t in closed if t.initial_risk is not None]
    stressed_r = [value / t.initial_risk for t, value in zip(closed, stressed) if t.initial_risk is not None]
    held = [t.exits[-1].bar - t.entry_bar + 1 for t in closed]
    # Campaigns can close and submit the next plan on the same bar. The next fill is later.
    market_bars = sum((min(t.release, bar_count - 1) - t.entry_bar + 1 for t in observed_positions), 0)
    realized_parts = [p for t in closed for p in t.exits]
    notional = [t.units * Decimal(str(t.entry_price)) * pointvalue for t in observed_positions]
    return {
        "closed": stats(equity), "stressed": stats(stressed),
        "campaign_close_drawdown_usd": closed_drawdown((t.exits[-1].time, i, t.cash)
            for i, t in enumerate(closed))["max_drawdown_cash"],
        "stressed_campaign_close_drawdown_usd": closed_drawdown((t.exits[-1].time, i, amount)
            for i, (t, amount) in enumerate(zip(closed, stressed)))["max_drawdown_cash"],
        "max_consecutive_losses": longest_loss_sequence(equity),
        "mean_net_r": mean(net_r) if net_r else None,
        "mean_stressed_net_r": mean(stressed_r) if stressed_r else None,
        "total_net_r": sum(net_r, ZERO) if net_r else None,
        "undefined_initial_risk": len(closed) - len(net_r),
        "mean_initial_risk_usd": mean(risk) if risk else None,
        "max_initial_risk_usd": max(risk, default=None),
        "max_entry_notional_usd": max(notional, default=None),
        "average_holding_bars": mean(held) if held else None,
        "average_holding_minutes_from_bar_timestamps": (mean(t.exits[-1].time - t.entry_time
                                                               for t in closed) / 60_000 if closed else None),
        "max_concurrent_campaigns": int(bool(observed_positions)),
        "exposed_chart_bars": market_bars,
        "fraction_of_chart_bars_exposed": ratio(market_bars, bar_count),
        "fee_usd": sum((p.commission for p in realized_parts), ZERO),
        "parts": len(realized_parts),
        "actual_market_or_stop_fill_part_count": sum(not p.reason.startswith("TP") for p in realized_parts),
        "nominal_market_slippage_usd_already_in_fill_prices": (
            Decimal(str(config["market_and_stop_adverse_slippage_ticks"])) *
            Decimal(".001") * pointvalue * (sum((t.units for t in closed), ZERO) +
            sum((p.units for p in realized_parts if not p.reason.startswith("TP")), ZERO))),
        "extra_stress_cost_usd": sum((stress(t, config, pointvalue) for t in closed), ZERO),
        "exit_part_reasons": dict(sorted(Counter(p.reason for p in realized_parts).items())),
        "gap_invalid_campaigns": sum(t.gap_invalid for t in trades),
        "moved_stops": sum(len(t.stop_changes) for t in trades),
        "open_or_pending": sum(not t.complete and not t.cancelled_at_entry for t in trades),
        "capital_failures_at_fill": sum(t.cancelled_at_entry for t in trades),
        "partial_cash_usd_excluded_from_complete_campaigns": sum((t.cash for t in trades if not t.complete), ZERO),
        "outside_event_closed": stats(outside),
        "opposite_event_closed": sum(event_side.get(t.entry_time // HOUR) == -t.side for t in closed),
        "by_side": {name: stats(t.cash for t in closed if t.side == side) for name, side in SIDES.items()},
    }


def event_lane(events, decisions, plans, trades, bars):
    logs_by_hour, plans_by_hour, fills_by_hour, admissions_by_hour = (defaultdict(list) for _ in range(4))
    for item in decisions:
        logs_by_hour[item["time"] // HOUR].append(item)
    for plan in plans:
        plans_by_hour[plan.time // HOUR].append(plan)
    for t in trades:
        admissions_by_hour[t.decision_time // HOUR].append(t)
        if t.entry_time is not None:
            fills_by_hour[t.entry_time // HOUR].append(t)
    result = {}
    for event in events:
        start, side = event["start"], event["side"]
        hour = start // HOUR
        obs = [d for d in logs_by_hour[hour] if d["side"] == side]
        candidates = [p for p in plans_by_hour[hour] if p.side == side]
        admits = [a for a in admissions_by_hour[hour] if a.side == side]
        fills = sorted((f for f in fills_by_hour[hour] if f.side == side), key=lambda f: f.entry_time)
        first = fills[0] if fills else None
        earliest = {
            "watch": min((d["time"] for d in obs if d["state"] == "WATCH"), default=None),
            "confirmed": min((d["time"] for d in obs if d["state"] == "CONFIRMED"), default=None),
            "geometry_ready": min((p.time for p in candidates), default=None),
            "admitted": min((a.decision_time for a in admits), default=None),
            "filled": first.entry_time if first else None,
        }
        present = any(t.side == side and t.entry_time is not None and t.entry_time < start
                      and t.release >= event["first_bar"] for t in trades)
        result[start] = {
            "time_utc_ms": earliest, "fill_id": first.id if first else None,
            "fill_model": first.engine if first else None,
            "submission_to_fill_minutes": ((first.entry_time - first.decision_time) / 60_000 if first else None),
            "submission_to_fill_bar_steps": first.entry_bar - first.decision_bar if first else None,
            "outside_bar_carry_in": present, "opposite_fills": len(fills_by_hour[hour]) - len(fills),
            **(excursion(first, event, bars) if first else {}),
        }
    return result


def event_summary(events, tape, decisions, models):
    summaries = {}
    for label, side in SIDES.items():
        matching = [e for e in events if e["side"] == side]
        rows = [tape[e["start"]] for e in matching]
        counts = {state: sum(r["time_utc_ms"][state] is not None for r in rows)
                  for state in ("watch", "confirmed", "geometry_ready", "admitted", "filled")}
        if not models:
            counts["watch"] = counts["geometry_ready"] = None
        eligible_start = {e["start"] // HOUR for e in matching}
        model_only = [d for d in decisions if d["model"] in models and d["side"] == side]
        summaries[label] = {
            "events": len(rows), "observed": counts,
            "event_detection_recall": ratio(counts["confirmed"], len(rows)),
            "actionable_signal_recall": ratio(counts["admitted"], len(rows)),
            "execution_recall": ratio(counts["filled"], len(rows)),
            "median_latency_minutes": {
                state: md((r["time_utc_ms"][state] - e["start"]) / 60_000
                          for r, e in zip(rows, matching) if r["time_utc_ms"][state] is not None)
                for state in counts
            },
            "median_execution_delay_minutes": md(r["submission_to_fill_minutes"] for r in rows if r["fill_id"]),
            "median_execution_delay_bars": md(r["submission_to_fill_bar_steps"] for r in rows if r["fill_id"]),
            "median_favorable_capture_fraction": md(r["capture_fraction"] for r in rows if r["fill_id"]),
            "median_favorable_excursion_price": md(r["favorable"] for r in rows if r["fill_id"]),
            "median_adverse_excursion_price": md(r["adverse"] for r in rows if r["fill_id"]),
            "same_side_carry_in": sum(r["outside_bar_carry_in"] for r in rows),
            "opposite_side_fills": sum(r["opposite_fills"] for r in rows),
            "off_event_confirmation_count": (sum(d["state"] == "CONFIRMED" and d["time"] // HOUR not in eligible_start
                                               for d in model_only) if models else None),
        }
    return summaries


def rejection_summary(blocked, risk, models):
    by_reason = Counter(r["reason"] for r in blocked)
    by_model = defaultdict(Counter)
    opportunity = defaultdict(lambda: ZERO)
    for r in blocked:
        by_model[r["model"]][r["reason"]] += 1
        if r["known_offer_outcome_usd"] is not None:
            opportunity[r["model"]] += r["known_offer_outcome_usd"]
    for item in risk:
        if item["model"] in models:
            by_model[item["model"]][item["reason"]] += 1
            by_reason[item["reason"]] += 1
    return {"primary_reason_counts": dict(sorted(by_reason.items())),
            "by_model": {model: dict(sorted(values.items())) for model, values in sorted(by_model.items())},
            "blocked_known_offer_cash_usd_if_taken": dict(sorted(opportunity.items()))}


def diagnostics_by_period(lanes, events, bars, config, pointvalue, split):
    first, cutoff = bars[0]["time"], bars[-1]["time_close"]
    weeks = range(MONDAY_EPOCH + ((first - MONDAY_EPOCH + WEEK - 1) // WEEK) * WEEK,
                  cutoff - WEEK + 1, WEEK)

    def period(start, end):
        use = {name: [t for t in tape if t.complete and start <= t.entry_time and t.exits[-1].time < end]
               for name, tape in lanes.items()}
        return {"from_utc": utc(start).isoformat(), "through_exclusive_utc": utc(end).isoformat(),
                "registered_hours": sum(start <= e["start"] and e["end"] <= end for e in events),
                "lanes": {name: {"count": len(tape), "net_usd": sum((t.cash for t in tape), ZERO),
                                 "stressed_usd": sum((t.cash - stress(t, config, pointvalue) for t in tape), ZERO)}
                          for name, tape in use.items()}}
    return {"earlier": period(first, split), "later": period(split, cutoff),
            "weekly_expanding_context_evaluation": [
                {"context_from_utc": utc(first).isoformat(), **period(start, start + WEEK)} for start in weeks
            ]}


def adjusted_inference(lanes, bars, execution, pointvalue):
    days = sorted({dayslice(b["time"]) for b in bars})

    def cash_by_day(trades):
        totals = defaultdict(lambda: ZERO)
        for t in trades:
            if t.complete:
                totals[dayslice(t.exits[-1].time)] += t.cash - stress(t, execution, pointvalue)
        return totals
    control = cash_by_day(lanes["S5"])
    models = [name for name in LANES if name != "S5"]
    raw, counts = [], []
    for model in models:
        candidate = cash_by_day(lanes[model])
        differences = [float(candidate[d] - control[d]) for d in days]
        blocks = [sum(differences[i:i + 5]) for i in range(0, len(days), 5)]
        counts.append(len(blocks))
        raw.append(sign_flip_p(blocks))
    adjusted = holm(raw)
    return {model: {"observed_utc_days": len(days), "adjacent_five_day_blocks": n,
                    "one_sided_sign_flip_p": p, "holm_adjusted_p": q}
            for model, n, p, q in zip(models, counts, raw, adjusted)}


def frame_result(tf, bars, earlier, original, settings, event_cfg):
    minutes = int(tf)
    execution = settings["execution"]
    run = original["run"]
    tick, pointvalue = float(run["mintick"]), run["pointvalue"]
    if tick != .001 or pointvalue != 1:
        raise ValueError("Historical execution contract changed")
    features = feature_rows(bars, settings["signals"], earlier)
    obs, candidates = scan(bars, features, settings["signals"])
    split_index = int(.60 * len(bars))
    for count in (split_index, len(bars) - 30):
        prefix = bars[:count]
        check = scan(prefix, feature_rows(prefix, settings["signals"], earlier), settings["signals"])
        if check != ([o for o in obs if o["bar"] < count], [c for c in candidates if c.index < count]):
            raise ValueError("Historical decisions changed under prefix replay")
    relevant = "h1_close" if tf == "5" else "h4_close" if tf == "15" else None
    match = sum(getattr(f, relevant) == b["htfClose"] for b, f in zip(bars, features)) if relevant else None
    if relevant and match != len(bars):
        raise ValueError(f"Reconstructed HTF close does not match archive {relevant}")
    candidate_plans = [make_plan(c, execution, tick, pointvalue) for c in candidates]
    risk = [{"id": p.id, "model": p.model, "side": p.side, "bar": p.bar, "time": p.time,
             "state": "ENTRY_REJECTED", "reason": reasons[0], "all_gate_failures": reasons}
            for p, reasons in candidate_plans if reasons]
    ready = [p for p, reasons in candidate_plans if not reasons]
    base = s5_campaigns(original["campaigns"])
    starting_capital = execution["starting_capital_usd"]
    s5, blocked_s5 = replay(base, bars, starting_capital, pointvalue)
    if blocked_s5 or {t.id for t in s5} != {t.id for t in base}:
        raise ValueError("The portfolio slot fails to reproduce original S5")
    events, event_calendar = hourly_events(bars, minutes, event_cfg)

    def model_replay(policy):
        return [simulate(p, bars, features, execution, tick, pointvalue, minutes, policy) for p in ready]

    fixed_offers = model_replay("FIXED")
    lanes, rejections = {"S5": s5}, {"S5": blocked_s5}
    for model in MODELS:
        lanes[model], rejections[model] = replay([o for o in fixed_offers if o.engine == model], bars,
                                                 starting_capital, pointvalue)
    lanes["ABC"], rejections["ABC"] = replay(fixed_offers, bars, starting_capital, pointvalue)
    lanes["S5+ABC"], rejections["S5+ABC"] = replay(base + fixed_offers, bars, starting_capital, pointvalue)
    reversed_tie, _ = replay(base + fixed_offers, bars, starting_capital, pointvalue, first="ABC")
    metrics = {}
    for name, orders in lanes.items():
        models = () if name == "S5" else (name,) if name in MODELS else MODELS
        histories = [o for o in obs if o["model"] in models]
        if name in ("S5", "S5+ABC"):
            # The S5 archive supplies admitted plans. It has no rejected candidates or watch states.
            histories.extend({"model": "S5", "side": t.side, "state": "CONFIRMED", "time": t.decision_time}
                             for t in base)
        tape = event_lane(events, histories, [p for p in ready if p.model in models], orders, bars)
        for event in events:
            event.setdefault("matches", {})[name] = tape[event["start"]]
        metrics[name] = {
            "economics": economics(orders, events, execution, pointvalue, len(bars)),
            "events_by_side": event_summary(events, tape, histories, models),
            "rejections": rejection_summary(rejections[name], risk, models),
        }
    baseline = metrics["S5"]["economics"]
    baseline_event_fills = sum(x["observed"]["filled"] for x in metrics["S5"]["events_by_side"].values())
    acceptance = {}
    for name in LANES[1:]:
        m = metrics[name]["economics"]
        off, control_off = m["outside_event_closed"], baseline["outside_event_closed"]
        observed_event_fills = sum(x["observed"]["filled"] for x in metrics[name]["events_by_side"].values())
        acceptance[name] = {
            "net_usd_delta": m["closed"]["net_cash"] - baseline["closed"]["net_cash"],
            "stressed_net_usd_delta": m["stressed"]["net_cash"] - baseline["stressed"]["net_cash"],
            "drawdown_usd_delta": m["campaign_close_drawdown_usd"] - baseline["campaign_close_drawdown_usd"],
            "mean_net_r_higher": bool(m["mean_net_r"] is not None and m["mean_net_r"] > baseline["mean_net_r"]),
            "event_fills_delta": observed_event_fills - baseline_event_fills,
            "off_event_loss_rate_at_most_control": bool(off["count"] and
                off["losses"] / off["count"] <= control_off["losses"] / control_off["count"]),
            "untouched_holdout": "UNAVAILABLE",
        }
    management, policies_orders, policies_blocked = {}, {}, {}
    fixed = metrics["ABC"]["economics"]
    for policy in POLICIES:
        if policy == "FIXED":
            evaluated, blocked = lanes["ABC"], rejections["ABC"]
        else:
            evaluated, blocked = replay(model_replay(policy), bars, starting_capital, pointvalue)
        value = economics(evaluated, events, execution, pointvalue, len(bars))
        net_delta = value["closed"]["net_cash"] - fixed["closed"]["net_cash"]
        stress_delta = value["stressed"]["net_cash"] - fixed["stressed"]["net_cash"]
        risk_delta = value["campaign_close_drawdown_usd"] - fixed["campaign_close_drawdown_usd"]
        management[policy] = {
            "label": POLICIES[policy], "economics": value, "net_delta_usd": net_delta,
            "stress_delta_usd": stress_delta, "drawdown_delta_usd": risk_delta,
            "status": ("REFERENCE" if policy == "FIXED" else "REJECTED_DRAWDOWN" if risk_delta > 0
                       else "INCONCLUSIVE_ARCHIVE" if net_delta > 0 and stress_delta > 0 else "REJECTED_PERFORMANCE"),
            "occupancy_rejections": len(blocked),
        }
        if policy != "FIXED":
            policies_orders[policy], policies_blocked[policy] = evaluated, blocked
    return ({
        "frame_minutes": minutes, "first_open_utc": utc(bars[0]["time"]).isoformat(),
        "last_close_utc": utc(bars[-1]["time_close"]).isoformat(), "bars": len(bars),
        "s5_market_fingerprints": [run["checksum1"], run["checksum2"]],
        "s5_native_equity_drawdown_usd": original["native_equity_drawdown"]["value"],
        "htf": {"prehistory_m30_bars": len(earlier), "archive_value_checked": relevant,
                "exact_archive_htf_close_matches": match,
                "eligible_bars_with_fewer_than_55_confirmed_h4_or_h1_bars":
                    sum(not f.htf_warm for f in features[settings["signals"]["warmup_bars"]:])},
        "event_calendar": event_calendar, "prefix_checks_at_bar_counts": [split_index, len(bars) - 30],
        "decision_counts": {model: dict(Counter(d["state"] for d in obs if d["model"] == model))
                            for model in (*MODELS, "REGIME")},
        "lanes": metrics, "acceptance_comparisons": acceptance, "management": management,
        "reversed_tie_policy_same_admissions": [o.id for o in reversed_tie] == [o.id for o in lanes["S5+ABC"]],
        "chronology": diagnostics_by_period(lanes, events, bars, execution, pointvalue, bars[split_index]["time"]),
        "descriptive_paired_stress_tests": adjusted_inference(lanes, bars, execution, pointvalue) if tf == "5" else None,
    }, {
        "events": events, "observations": obs,
        "candidates": [asdict(c) for c in candidates],
        "geometry": [asdict(p) for p, _ in candidate_plans], "risk_rejections": risk,
        "portfolio_rejections": rejections,
        "orders_by_lane": {name: [asdict(o) for o in orders] for name, orders in lanes.items()},
        "orders_by_alternative_management": {name: [asdict(o) for o in orders]
                                              for name, orders in policies_orders.items()},
        "rejections_by_alternative_management": policies_blocked,
    })


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-root", type=Path, required=True)
    p.add_argument("--logs-dir", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--tapes", type=Path, required=True)
    args = p.parse_args()
    if args.output.exists() or args.tapes.exists():
        p.error("Use new output filenames")
    settings = json.loads(REGISTRATION.read_bytes())
    source = json.loads((ROOT / settings["archive_registration"]).read_bytes())
    event_cfg = json.loads((ROOT / settings["event_registration"]).read_bytes())["event"]
    bars, original = {}, {}
    for tf, item in source["frames"].items():
        market = args.data_root / Path(item["local_bars"]).parent.name / "bars.csv"
        raw_s5 = args.logs_dir / item["log_name"]
        read_pinned(market, item["bars_sha256"])
        read_pinned(raw_s5, item["log_sha256"])
        bars[tf] = load_bars(market)
        original[tf] = inspect(raw_s5)
        if original[tf]["mode"] != "S5" or original[tf]["unfilled"]:
            raise ValueError("Historical S5 source has unrecorded proposals")
        verify_bars(bars[tf], original[tf]["run"])
    s = [b["time_close"] for b in bars["30"]]
    outputs, tapes = {}, {}
    for minutes in settings["frames_minutes"]:
        tf = str(minutes)
        context = bars["30"][:bisect_right(s, bars[tf][0]["time"])]
        outputs[tf], tapes[tf] = frame_result(tf, bars[tf], context, original[tf], settings, event_cfg)
    raw = json.dumps(tapes, default=json_default, allow_nan=False, ensure_ascii=False,
                     separators=(",", ":")).encode("utf-8")
    with args.tapes.open("xb") as stream:
        stream.write(gzip.compress(raw, mtime=0, compresslevel=9))
    write_json(args.output, {"schema": "qirvanta.v22.historical-results.v1",
                             "status": "EXPERIMENTAL_ARCHIVED_SAMPLE",
                             "registration_sha256": sha(REGISTRATION.read_bytes()),
                             "tape_sha256": sha(args.tapes.read_bytes()), "frames": outputs})
    print(f"Evaluated {len(outputs)} frames, 6 portfolio lanes and {len(POLICIES)} isolated management policies.")


if __name__ == "__main__":
    main()
