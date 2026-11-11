"""Reconstruct archived FAST decisions and outcomes with pinned inputs."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
from statistics import mean, median

from gold_pro.archive import REGISTRATION, ROOT, _hash, _read, _read_json, _write, cash_campaign
from gold_pro.opportunity import event_audit, scan
from gold_pro.replay import Costs, portfolio
from research.data.loader import load_bars
from unified.tools.evaluate_v21_impulse import HOUR, hourly_events
from unified.tools.evaluate_v22_signals import feature_rows
from unified.tools.gold_market_data import context_rows

PROTOCOL = Path(__file__).with_name("diagnostic_protocol.json")


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _market(config, data_root, frame):
    source = _read_json(_read(ROOT / config["s5_pin"], config["s5_pin_sha256"]))
    item = source["frames"][str(frame)]
    path = data_root / Path(item["local_bars"]).parent.name / "bars.csv"
    return load_bars(_read(path, item["bars_sha256"])), item["bars_sha256"]


def _baseline(protocol, report_path, tape_path, data_root):
    report = _read_json(_read(report_path, protocol["source_report_sha256"]))
    if report["private_tape_sha256"] != protocol["source_tape_sha256"]:
        raise ValueError("Release report and diagnostic pins differ")
    _read(tape_path, protocol["source_tape_sha256"])
    recorded = json.loads(gzip.decompress(tape_path.read_bytes()))["5"]["FAST"]
    for name, digest in report["research_sources"].items():
        _read(ROOT / name, digest)
    _read(ROOT / "unified/tools/evaluate_v22_signals.py", protocol["feature_code_sha256"])
    _read(ROOT / "unified/tools/evaluate_v21_impulse.py", protocol["event_code_sha256"])
    settings = _read_json(_read(REGISTRATION, report["registration_sha256"]))
    bars, bars_sha = _market(settings, data_root, 5)
    old, prior_sha = _market(settings, data_root, 30)
    prior = old[:bisect_right([row["time_close"] for row in old], bars[0]["time"])]
    feature_config = _read_json(_read(
        ROOT / "docs/v22_evidence/historical_registration.json", protocol["feature_config_sha256"]))
    event_config = _read_json(_read(
        ROOT / settings["opportunity"]["event_reference"], protocol["event_config_sha256"]))["event"]
    c = settings["cost"]
    costs = Costs(c["tick"], c["pointvalue"], c["commission_per_unit_per_fill"],
                  c["adverse_ticks_market_and_stop"], c["round_trip_spread"],
                  c["stress_extra_round_trip_spread"], c["stress_extra_slip_each_side"], c["stress_commission_fraction"])
    equity = settings["study"]["starting_equity_usd"]
    features = feature_rows(bars, feature_config["signals"], earlier=prior)
    contexts = list(context_rows(bars, tick=costs.tick, earlier=prior))
    sides = [(row.h1.side, row.h4.side) for row in contexts]
    observations, plans = scan(bars, features, sides, settings["opportunity"], costs, equity, mode="FAST")
    outcomes, rejects = portfolio(plans, bars, costs, 5, equity)
    events, _, _ = event_audit(bars, 5, event_config, observations, plans, outcomes, rejects)
    replayed = {"events": events, "observations": observations, "outcomes": [asdict(o) for o in outcomes],
                "portfolio_rejections": [{"id": p.id, "bar": p.bar, "reason": r} for p, r in rejects]}
    if json.loads(json.dumps(replayed, allow_nan=False)) != recorded:
        raise ValueError("Reconstructed FAST tape differs from pinned original")
    identities = {"m5_sha256": bars_sha, "prior_m30_sha256": prior_sha, "m5_bars": len(bars),
                  "prior_m30_bars": len(prior), "m5_start_utc": utc(bars[0]["time"]),
                  "m5_end_utc": utc(bars[-1]["time_close"]),
                  "scan_warmup_bars": settings["opportunity"]["warmup_bars"], "matched_tape_sections": list(replayed)}
    return (report["frames"]["5"]["reports"]["opportunity"]["history"]["FAST"], bars, features,
            contexts, observations, outcomes, rejects, events, event_config, costs, identities)


def _anchors(bars, features, warmup):
    """Use levels known before each signal, with the original retest ordering."""
    recent_break, rows = {}, {}
    for i in range(warmup, len(bars)):
        f = features[i]
        if f.mss:
            recent_break.pop(-f.bos, None)
        for side in (1, -1):
            edge = "high" if side == 1 else "low"
            extreme = max if side == 1 else min
            swings = f.highs if side == 1 else f.lows
            prior_retest = recent_break.get(side)
            swing = swings[-1][1] if swings else None
            continuation = extreme(row[edge] for row in bars[i-3:i])
            if f.bos == side and swing is not None:
                recent_break[side] = swing
            rows[i, side] = {
                "BREAKOUT": swing if f.bos == side and swing is not None else continuation,
                "CONTINUATION": continuation,
                "COMPRESSION": extreme(row[edge] for row in bars[i-4:i]),
                "PULLBACK": features[i-1].ema21,
                "RETEST": prior_retest,
            }
    return rows


def _invalidation(outcome, anchor, bars, features, protocol):
    plan = outcome.plan
    first_stop = min((leg.bar for leg in outcome.exits if leg.reason in {"STOP", "STOP_GAP"}), default=None)
    first_failure = first_opposite = first_shock = None
    # A stop touch or the final exit can precede the bar close.
    limit = min(first_stop if first_stop is not None else len(bars), outcome.release)
    for i in range(outcome.entry_bar, limit):
        f, bar = features[i], bars[i]
        if first_failure is None and plan.side * (bar["close"] - anchor) < -protocol["anchor_failure_atr"] * plan.atr:
            first_failure = i
        if first_opposite is None and f.bos == -plan.side:
            first_opposite = i
        if first_shock is None and f.spike:
            first_shock = i
    changes = [i for i in (first_failure, first_opposite) if i is not None]
    invalidated = min(changes) if changes else None
    before_stop = invalidated is not None and first_stop is not None
    return {
        "anchor_failure_close_utc": utc(bars[first_failure]["time_close"]) if first_failure is not None else None,
        "opposite_bos_close_utc": utc(bars[first_opposite]["time_close"]) if first_opposite is not None else None,
        "shock_close_utc": utc(bars[first_shock]["time_close"]) if first_shock is not None else None,
        "first_thesis_invalidation_close_utc": utc(bars[invalidated]["time_close"]) if invalidated is not None else None,
        "original_stop_bar_open_utc": utc(bars[first_stop]["time"]) if first_stop is not None else None,
        "invalidation_before_original_stop": before_stop,
        "known_bars_before_original_stop": first_stop - invalidated if before_stop else None,
        "next_open_precedes_stop_bar": invalidated + 1 < first_stop if before_stop else None,
    }


def _campaign(outcome, available, confirmations, anchor, bars, features, contexts, costs, protocol, event_keys):
    plan = outcome.plan
    entry, feature, context = bars[outcome.entry_bar], features[plan.bar], contexts[plan.bar]
    side, atr = plan.side, plan.atr
    if plan.reference != bars[plan.bar]["close"] or available["time_ms"] != bars[plan.bar]["time_close"]:
        raise ValueError(f"Signal bar mismatch: {plan.id}")
    if outcome.entry_bar != plan.bar + 1 or abs(outcome.fill - (entry["open"] + side * costs.slip)) > 1e-8:
        raise ValueError(f"Market fill mismatch: {plan.id}")
    if (available["h1"], available["h4"]) != (context.h1.side, context.h4.side):
        raise ValueError(f"Canonical context mismatch: {plan.id}")
    complete = outcome.state == "CLOSED"
    price_risk = side * (outcome.fill - plan.stop)
    initial_risk_price = price_risk if price_risk > 0 else available["stop_distance"]
    completed = cash_campaign(outcome, bars, costs) if complete else None
    actual_loss = completed is not None and completed.net_cash < 0
    estimate = costs.spread + 2 * costs.slip + 2 * costs.commission / costs.pointvalue
    friction_fraction = estimate / available["stop_distance"]
    last_full = min(outcome.release, len(bars))
    known_prices = [outcome.fill, *(leg.price for leg in outcome.exits)]
    extrema = bars[outcome.entry_bar + 1:last_full]
    favorable = max([side * (price - outcome.fill) for price in known_prices]
                     + [side * (b["high" if side == 1 else "low"] - outcome.fill) for b in extrema])
    adverse = max([-side * (price - outcome.fill) for price in known_prices]
                   + [-side * (b["low" if side == 1 else "high"] - outcome.fill) for b in extrema])
    invalidation = _invalidation(outcome, anchor, bars, features, protocol)
    opposite = [item for i in range(max(0, plan.bar - protocol["lookback_opposing_bars"]), plan.bar + 1)
                for item in confirmations[i, -side] if item["time_ms"] <= entry["time"]]
    last_opposite = max(opposite, key=lambda item: (item["time_ms"], item["model"])) if opposite else None
    final_reasons = sorted({part.reason for part in outcome.exits if part.bar == outcome.release})
    flags = []
    if invalidation["anchor_failure_close_utc"] and plan.model != "PULLBACK":
        flags.append("FALSE_BREAKOUT")
    if (available["signal_age_bars"] >= protocol["late_signal_age_bars"]
            or available["extension_atr"] >= protocol["late_extension_atr"]
            or side * (outcome.fill - plan.reference) / atr >= protocol["late_adverse_fill_atr"]):
        flags.append("LATE_ENTRY")
    if -side in (context.h1.side, context.h4.side):
        flags.append("HTF_CONFLICT")
    if feature.spike or invalidation["shock_close_utc"]:
        flags.append("VOLATILITY_SHOCK")
    if invalidation["first_thesis_invalidation_close_utc"]:
        flags.append("STRUCTURAL_INVALIDATION")
    if friction_fraction >= protocol["unfavorable_friction_to_initial_risk"]:
        flags.append("UNFAVORABLE_COST")
    if available["net_room_r"] < protocol["thin_room_net_r"]:
        flags.append("INSUFFICIENT_ROOM")
    if available["stop_distance"] / atr >= protocol["wide_stop_atr"]:
        flags.append("STOP_TOO_WIDE")
    if "TIME_EXIT" in final_reasons:
        last = outcome.release - 1
        if (not invalidation["first_thesis_invalidation_close_utc"]
                and features[last].trend == side and side * (bars[last]["close"] - outcome.fill) > 0
                and side * (bars[last]["close"] - bars[last-3]["close"]) > 0):
            flags.append("PREMATURE_EXIT")
    if actual_loss and not flags:
        flags.append("UNCLASSIFIED")
    return {
        "campaign_id": plan.id, "state": outcome.state, "side": "BUY" if side == 1 else "SELL",
        "model": plan.model, "session": context.session, "second_chance": available["second_chance"],
        "watch_utc": utc(available["root_watch_ms"]), "confirmed_signal_utc": utc(available["time_ms"]),
        "first_structure_utc": utc(available["first_structure_ms"]) if available["first_structure_ms"] is not None else None,
        "fill_open_utc": utc(entry["time"]), "final_exit_bar_close_utc": utc(bars[outcome.release]["time_close"]) if complete else None,
        "fill_delay_bars": 1, "minutes_from_signal_close_to_fill_open": (entry["time"] - available["time_ms"]) / 60_000,
        "signal_age_bars": available["signal_age_bars"], "signal_age_minutes": available["signal_age_minutes"],
        "regime": plan.regime, "regime_reason": feature.regime_reason, "structural_bias": feature.structure,
        "bos_side": feature.bos, "mss": feature.mss, "htf_h1_side": context.h1.side, "htf_h4_side": context.h4.side,
        "htf_h1_close_utc": utc(context.h1.known_at) if context.h1.known_at is not None else None,
        "htf_h4_close_utc": utc(context.h4.known_at) if context.h4.known_at is not None else None,
        "directional_momentum_atr": available["momentum_atr"], "momentum_deteriorating": available["momentum_deteriorating"],
        "directional_displacement": feature.displacement == side, "atr": atr,
        "anchor_distance_at_signal_atr": side * (plan.reference - anchor) / atr,
        "anchor_distance_at_fill_atr": side * (outcome.fill - anchor) / atr,
        "extension_atr": available["extension_atr"], "original_stop_distance": available["stop_distance"],
        "original_stop_atr": available["stop_distance"] / atr, "net_room_r": available["net_room_r"],
        "friction_to_planned_price_risk": friction_fraction, "modeled_round_trip_spread_quote": costs.spread,
        "estimated_all_in_friction_usd": estimate * costs.pointvalue * plan.units,
        "priced_commission_usd": sum(leg.commission for leg in outcome.exits),
        "known_slippage_already_in_prices_usd": costs.slip * costs.pointvalue * (plan.units + sum(
            leg.units for leg in outcome.exits if not leg.reason.startswith("TP"))),
        "mfe_lower_bound_r": favorable / initial_risk_price, "mae_lower_bound_r": adverse / initial_risk_price,
        "net_usd": completed.net_cash if complete else None,
        "stress_net_usd": completed.stressed_net_cash if complete else None,
        "net_r": completed.net_cash / completed.risk_cash if complete else None, "closed_leg_net_usd": outcome.net,
        "exit_reasons": final_reasons, "exit_legs": [{"bar_close_utc": utc(bars[leg.bar]["time_close"]),
                      "reason": leg.reason, "units": leg.units, "net_usd": leg.net} for leg in outcome.exits],
        "invalidation": invalidation, "diagnostic_flags": flags,
        "opposing_signal_before_entry": {"close_utc": utc(last_opposite["time_ms"]), "model": last_opposite["model"]}
            if last_opposite else None,
        "same_side_large_event_at_fill": (entry["time"] // HOUR, side) in event_keys,
        "price_evidence": {
            "confirmed_prior_pivots_high": [{"bar_open_utc": utc(bars[i]["time"]), "price": price} for i, price in feature.highs],
            "confirmed_prior_pivots_low": [{"bar_open_utc": utc(bars[i]["time"]), "price": price} for i, price in feature.lows],
            "directional_anchor": anchor, "nearest_barrier": available["known_barrier"], "signal_close": plan.reference,
            "modeled_market_fill": outcome.fill, "initial_structural_stop": plan.stop, "targets": plan.targets,
            "actual_modeled_exits": [leg.price for leg in outcome.exits],
        },
    }


def _rejected_audit(observations, rejects, bars, features, events, recorded, protocol):
    event_by_key = {(e["start"] // HOUR, e["side"]): e for e in events}
    recorded_by_key = {(e["time_ms"] // HOUR, e["side"]): e for e in recorded}
    available = {item["plan_id"]: item for item in observations if item["state"] == "ENTRY_AVAILABLE"}
    risk_rows = [item for item in observations if item["state"] == "RISK_REJECTED"]
    portfolio_rows = [{**available[plan.id], "all_reasons": [reason], "reason": reason}
                      for plan, reason in rejects]
    summaries = {}
    event_rows = []
    for kind, rows in (("risk", risk_rows), ("portfolio", portfolio_rows)):
        primary, all_reasons = Counter(), Counter()
        matches = threshold = 0
        for item in rows:
            primary[item["reason"]] += 1
            all_reasons.update(item["all_reasons"])
            key = item["time_ms"] // HOUR, item["side"]
            event = event_by_key.get(key)
            if event is None:
                continue
            first = item["bar"] + 1
            if first > event["last_bar"]:
                continue
            matches += 1
            side = item["side"]
            anchor = bars[first]["open"]
            edge = "high" if side > 0 else "low"
            favorable = max(side * (b[edge] - anchor) for b in bars[first:event["last_bar"] + 1])
            favorable_atr = favorable / features[item["bar"]].atr
            if favorable_atr < protocol["rejected_event_excursion_atr"]:
                continue
            threshold += 1
            event_rows.append({"rejection_kind": kind, "candidate_id": item["plan_id"],
                               "confirmed_signal_utc": utc(item["time_ms"]), "side": "BUY" if side > 0 else "SELL",
                               "model": item["model"], "primary_reason": item["reason"],
                               "all_reasons": item["all_reasons"], "event_start_utc": utc(event["start"]),
                               "event_had_same_side_fast_fill": recorded_by_key[key]["filled"],
                               "observed_move_from_next_open_atr": favorable_atr})
        summaries[kind] = {"count": len(rows), "primary_reasons": dict(primary), "all_reasons": dict(all_reasons),
                           "in_same_side_large_event_with_later_bar": matches,
                           "later_event_excursion_at_least_1_atr": threshold}
    summaries["large_moves"] = {
        "events": len(events), "with_fast_fill": sum(item["filled"] for item in recorded),
        "missed_with_qualifying_rejection": len({item["event_start_utc"] for item in event_rows
                                                 if not item["event_had_same_side_fast_fill"]}),
        "rejection_examples": sorted(event_rows, key=lambda item: (item["confirmed_signal_utc"], item["candidate_id"])),
    }
    return summaries


def _summary(rows, original):
    closed = [row for row in rows if row["state"] == "CLOSED"]
    winners = [row for row in closed if row["net_usd"] > 0]
    losers = [row for row in closed if row["net_usd"] < 0]
    net = sum(row["net_usd"] for row in closed)
    stress = sum(row["stress_net_usd"] for row in closed)
    expectancy_r = mean(row["net_r"] for row in closed)
    if (len(winners), len(losers)) != (original["wins"], original["losses"]) or any(
        abs(a-b) > 1e-9 for a, b in [(net, original["net_cash"]), (stress, original["stressed_net_cash"]),
                                     (expectancy_r, original["expectancy_r"])]):
        raise ValueError("Reconstructed campaign outcomes differ from the archived FAST report")
    by_model = {}
    for name in sorted({row["model"] for row in closed}):
        subset = [row for row in closed if row["model"] == name]
        by_model[name] = {"closed": len(subset), "wins": sum(row["net_usd"] > 0 for row in subset),
                          "net_usd": sum(row["net_usd"] for row in subset)}
    leads = [row["invalidation"]["known_bars_before_original_stop"] for row in losers
             if row["invalidation"]["invalidation_before_original_stop"]]
    return {
        "closed": len(closed), "wins": len(winners), "losses": len(losers),
        "win_rate": len(winners) / len(closed), "net_usd": net, "stress_net_usd": stress,
        "expectancy_r": expectancy_r, "profit_factor": original["profit_factor"],
        "campaign_close_drawdown_usd": original["campaign_close_drawdown"],
        "avg_win_usd": mean(row["net_usd"] for row in winners), "avg_loss_usd": mean(row["net_usd"] for row in losers),
        "avg_loss_r": mean(row["net_r"] for row in losers), "median_loss_r": median(row["net_r"] for row in losers),
        "worst_r": min(row["net_r"] for row in closed), "fifth_percentile_r": original["p05_net_r"],
        "known_cost_usd": original["known_cost_cash"], "net_plus_known_cost_usd": net + original["known_cost_cash"],
        "net_loss_before_any_target": sum(not any(leg["reason"].startswith("TP") for leg in row["exit_legs"]) for row in losers),
        "loser_invalidation_before_original_stop": len(leads),
        "loser_median_thesis_invalidation_lead_bars": median(leads) if leads else None,
        "loser_next_open_precedes_stop_bar": sum(row["invalidation"]["next_open_precedes_stop_bar"] is True for row in losers),
        "loser_with_opposing_confirm": sum(row["opposing_signal_before_entry"] is not None for row in losers),
        "winner_with_opposing_confirm": sum(row["opposing_signal_before_entry"] is not None for row in winners),
        "loser_mfe_median_lower_bound_r": median(row["mfe_lower_bound_r"] for row in losers),
        "winner_mfe_median_lower_bound_r": median(row["mfe_lower_bound_r"] for row in winners),
        "flags_losses": dict(Counter(flag for row in losers for flag in row["diagnostic_flags"])),
        "flags_wins": dict(Counter(flag for row in winners for flag in row["diagnostic_flags"])),
        "final_reasons": dict(Counter(reason for row in closed for reason in row["exit_reasons"])),
        "loss_final_reasons": dict(Counter(reason for row in losers for reason in row["exit_reasons"])),
        "by_model": by_model,
    }


def diagnose(report_path, tape_path, data_root):
    protocol = _read_json(PROTOCOL)
    (original, bars, features, contexts, observations, outcomes, rejects, event_records, event_config,
     costs, identities) = _baseline(protocol, report_path, tape_path, data_root)
    states = Counter(outcome.state for outcome in outcomes)
    if states != {"CLOSED": protocol["closed_campaign_count"], "CENSORED_OPEN": protocol["censored_open_count"]}:
        raise ValueError(f"Unexpected FAST campaign states: {states}")
    market_events, _ = hourly_events(bars, 5, event_config)
    event_keys = {(e["start"] // HOUR, e["side"]) for e in market_events}
    confirmations = defaultdict(list)
    available = {}
    for row in observations:
        if row["state"] == "TREND_CONFIRMED":
            confirmations[row["bar"], row["side"]].append(row)
        elif row["state"] == "ENTRY_AVAILABLE":
            available[row["plan_id"]] = row
    anchors = _anchors(bars, features, identities["scan_warmup_bars"])
    rows = []
    for outcome in outcomes:
        plan = outcome.plan
        anchor = anchors[plan.bar, plan.side][plan.model]
        if anchor is None:
            raise ValueError(f"Missing confirmed anchor: {plan.id}")
        rows.append(_campaign(outcome, available[plan.id], confirmations, anchor, bars, features, contexts,
                              costs, protocol, event_keys))
    summary = _summary(rows, original)
    missed = _rejected_audit(observations, rejects, bars, features, market_events, event_records, protocol)
    return {"status": "VERIFIED_ARCHIVED_RECONSTRUCTION", "protocol": protocol,
            "protocol_sha256": _hash(PROTOCOL), "source_identities": identities,
            "costs": asdict(costs), "states": dict(states), "summary": summary, "rejected": missed,
            "campaigns": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--tape", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--private-out", type=Path)
    args = parser.parse_args()
    paths = [p.resolve() for p in (args.out, args.private_out) if p is not None]
    if len(set(paths)) != len(paths) or any(p.exists() for p in paths):
        parser.error("Choose distinct new output paths")
    result = diagnose(args.report, args.tape, args.data_root)
    if args.private_out:
        _write(args.private_out, result)
    for row in result["campaigns"]:
        del row["price_evidence"]
    _write(args.out, result)


if __name__ == "__main__":
    main()
