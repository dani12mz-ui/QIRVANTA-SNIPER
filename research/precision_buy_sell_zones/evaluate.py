"""Evaluate preregistered BUY/SELL zones on exact pre-seal OANDA archives."""

import argparse
from collections import Counter, defaultdict
from dataclasses import replace
from gzip import compress
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from gold_pro.archive import native_outcomes
from gold_pro.direct_breakout_research import (
    BASE, EVENT_CONFIG, FORWARD_SEAL, HOUR, RC2_ARCHIVE, STRESS, WARMUP,
    independent_hours, journal, pinned, portfolio,
)
from gold_pro.direct_trend_breakout import MAX_HOLD, closed_context
from gold_pro.rc1_qa import compare_exports, read_export
from gold_pro.rc2_research import chart_bars
from gold_pro.replay import Plan, round_away
from research.data.loader import load_bars
from research.direct_trend_breakout.forensic_timeline import ms, utc
from research.direct_trend_breakout.shadow_native import read_native
from research.precision_buy_sell_zones.replay import replay
from research.professional_signal_selector.evaluate import (
    checked, expectancy, extract, native_rc2_control, observed_native_slots, require, tagged,
)
from research.validation.statistics import wilson
from unified.tools.audit_premium_s5 import verify_bars
from unified.tools.audit_tv_observations import inspect
from unified.tools.gold_market_data import context_rows


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PROTOCOL_SHA = "7406878761ffa1d6db918b72a2e283efde224f542723238a49f10cf80023ecc4"
S5_REGISTRATION = ROOT / "docs/premium_evidence/s5_screen_registration.json"
S5_REGISTRATION_SHA = "9ef45b538670332e797e5bcbb0660b912f910c54fdf94fab3eb899f5165e6d97"
CASE_ARCHIVE = ROOT / "research/final_htf_data_integrity/evidence/native-pairs.zip"
CASE_ARCHIVE_SHA = "33a526caaa71c401b21547f3dff9fa1080ac775bfb2b030e75beb91faae886d4"
CASE_START = ms("2026-11-11T00:00:00Z")
CASE_END = ms("2026-11-11T17:30:00Z")
DECLINE = ms("2026-11-11T11:30:00Z")
FRAMES = (5, 15, 30)
SIDES = {1: "BUY", -1: "SELL"}
COSTS = {"normal": BASE, "stress": STRESS}
TERMINAL = ("INVALIDATED", "EXPIRED", "REPLACED")
OHLC = ("open", "high", "low", "close")


def read_registered(data_root, native_dir):
    registration = json.loads(checked(S5_REGISTRATION, S5_REGISTRATION_SHA))
    loaded = {}
    for frame in FRAMES:
        entry = registration["frames"][str(frame)]
        path = data_root / Path(entry["local_bars"]).parent.name / "bars.csv"
        checked(path, entry["bars_sha256"])
        bars = load_bars(path)
        log = native_dir / entry["log_name"]
        checked(log, entry["log_sha256"])
        run = inspect(log)
        require(run["mode"] == "S5" and run["run"]["timeframe"] == str(frame) and not run["unfilled"],
                f"M{frame}: invalid immutable S5 journal")
        verify_bars(bars, run["run"])
        original = native_outcomes(run, BASE)
        lane = ([replace(row.plan, model="S5_RAW") for row in original],
                [(row.plan.bar, row.release, row.plan.id) for row in original], {
                    "control": "Original archived S5 native admitted campaigns",
                    "observed_native_campaigns": len(original),
                    "observed_native_closed": sum(row.state == "CLOSED" for row in original),
                    "native_log_sha256": entry["log_sha256"],
                    "ohlc_fingerprints": [run["run"]["checksum1"], run["run"]["checksum2"]],
                })
        loaded[frame] = bars, {"S5_RAW": lane}, {"bar_sha256": entry["bars_sha256"]}
    return loaded


def read_matched():
    with ZipFile(BytesIO(pinned(RC2_ARCHIVE))) as archive, TemporaryDirectory() as scratch:
        result = {}
        for frame in FRAMES:
            left, right = (read_export(extract(archive, f"{name}_M{frame}.csv", Path(scratch)), frame, "audit")
                           for name in ("parity", "fixed"))
            match = compare_exports(left, right, ("core", "ready"))
            require(match["status"] == "MATCH", f"M{frame}: immutable matched CORE/READY differs")
            bars = chart_bars(left)
            lanes = {}
            for name, run in (("S5_RAW", left), ("RC2", right)):
                approved, _, slots, meta = native_rc2_control(bars, run, frame, name)
                lanes[name] = approved, slots, meta
            result[frame] = bars, lanes, {"archive": RC2_ARCHIVE, "core_ready": match}
        return result


def assert_overlap(lower, higher):
    by_open = {bar["time"]: bar for bar in lower}
    step = lower[0]["time_close"] - lower[0]["time"]
    count = 0
    for bar in higher:
        parts = [by_open.get(stamp) for stamp in range(bar["time"], bar["time_close"], step)]
        if not parts or any(row is None for row in parts):
            continue
        aggregated = (parts[0]["open"], max(row["high"] for row in parts),
                      min(row["low"] for row in parts), parts[-1]["close"])
        require(aggregated == tuple(bar[key] for key in OHLC), f"Overlapping provider OHLC differs at {utc(bar['time'])}")
        count += 1
    require(count > 0, "No exact overlapping provider intervals")
    return count


def sample_events(run, warmup):
    zones = {zone.id: zone for zone in run.zones if zone.formed_index >= warmup}
    events = defaultdict(dict)
    for event in run.events:
        if event.zone_id in zones:
            require(event.state not in events[event.zone_id], f"Duplicate {event.state}: {event.zone_id}")
            events[event.zone_id][event.state] = event
    require(set(zones) == set(events), "Zone formation is missing from transition log")
    return zones, events


def completion(row):
    return next((row[state] for state in TERMINAL if state in row), None)


def wilson_rate(positive, total):
    return {"count": positive, "denominator": total, "rate": positive / total if total else None,
            "wilson_95": wilson(positive, total)}


def false_reversal(zone, rejection, bars, horizon, buffer_atr):
    index = rejection.bar_index
    next_bars = bars[index + 1:index + horizon + 1]
    if len(next_bars) != horizon:
        return "RIGHT_CENSORED"
    if any(row["time"] != bars[index + i]["time_close"] for i, row in enumerate(next_bars)):
        return "GAP_CENSORED"
    boundary = zone.distal - zone.side * buffer_atr * rejection.atr
    return "FALSE_REVERSAL" if any(zone.side * (row["close"] - boundary) <= 0 for row in next_bars) else "NO_RECLAIM"


def life_summary(zones, events, bars, thresholds, side):
    pairs = [(zone, events[zone.id]) for zone in zones.values() if not side or zone.side == side]
    state_counts = Counter(state for _, row in pairs for state in row)
    touched = [(zone, row) for zone, row in pairs if "FIRST_TOUCH" in row]
    rejected = [(zone, row) for zone, row in pairs if "REJECTION_CONFIRMED" in row]
    mature = [(zone, row) for zone, row in pairs if completion(row)]
    wait = [(row["FIRST_TOUCH"].known_ms - zone.known_ms) / 60_000 for zone, row in touched]
    confirmation = [(row["REJECTION_CONFIRMED"].known_ms - row["FIRST_TOUCH"].known_ms) / 60_000
                    for _, row in rejected]
    false = {}
    for horizon in (3, 6, 12):
        status = Counter(false_reversal(zone, row["REJECTION_CONFIRMED"], bars, horizon,
                                        thresholds["invalidation_close_atr"]) for zone, row in rejected)
        eligible = status["FALSE_REVERSAL"] + status["NO_RECLAIM"]
        false[str(horizon)] = {**wilson_rate(status["FALSE_REVERSAL"], eligible), "states": dict(status)}
    return {
        "zones": len(pairs), "lifecycle_counts": dict(state_counts), "right_censored_active": len(pairs) - len(mature),
        "touch_rate_observed": wilson_rate(len(touched), len(pairs)),
        "touch_rate_terminal_zones_only": wilson_rate(sum("FIRST_TOUCH" in row for _, row in mature), len(mature)),
        "rejection_rate_all_zones": wilson_rate(len(rejected), len(pairs)),
        "rejection_rate_touched_zones": wilson_rate(len(rejected), len(touched)),
        "formation_to_touch_minutes_median": median(wait) if wait else None,
        "touch_to_rejection_minutes_median": median(confirmation) if confirmation else None,
        "false_reversal_after_rejection": false,
    }


def freeze_offer(zone, rejection, bar):
    reference = bar["close"]
    side, tick = zone.side, BASE.tick
    stop = round_away(zone.distal - side * tick, tick, -side)
    risk = side * (reference - stop)
    require(risk > 0, f"Rejection has no valid distal stop: {zone.id}")
    targets = tuple(round_away(reference + side * ratio * risk, tick, side) for ratio in (1, 2, 3))
    return Plan(zone.id, "ZONE_REJECTION_RESEARCH", side, rejection.bar_index, reference, stop, targets,
                rejection.atr, MAX_HOLD)


def model_outcomes(plans, bars, cost, bounds):
    results = portfolio(plans, bars, cost)
    start, end = bounds
    metrics = {label: expectancy(results, bars, start, end, side, bootstrap=side == 0)
               for side, label in ((0, "all"), *SIDES.items())}
    positive_tp1 = sum(row.entry_bar is not None and row.plan.side * (row.plan.targets[0] - row.entry) * cost.pointvalue
                       > 2 * cost.commission for row in results)
    return results, {
        "admission_and_terminal_disposition": dict(Counter(row.state for row in results)),
        "filled_with_positive_modeled_tp1_after_cost": positive_tp1,
        "metrics": metrics,
        "filled_campaigns": [journal(row, bars) for row in results if row.entry_bar is not None],
        "unfilled_ids_by_reason": {state: [row.plan.id for row in results if row.state == state]
                                   for state in sorted({row.state for row in results if row.entry_bar is None})},
    }


def main_displacement(event, bars):
    side = event["side"]
    return next(bar["time_close"] for bar in bars[event["first_bar"]:event["last_bar"] + 1]
                if side * (bar["close"] - event["open"]) >= .75 * event["scale"])


def opportunity_coverage(independent, zones, event_rows, outcomes, bars):
    audit = []
    for episode in independent:
        start, stop, side = episode["start"], episode["end"], episode["side"]
        main = main_displacement(episode, bars)
        same = [zone for zone in zones.values() if zone.side == side]
        before = lambda zone: zone.known_ms < main and (
            (terminal := completion(event_rows[zone.id])) is None or terminal.known_ms > max(start, zone.known_ms))
        carried = [zone.id for zone in same if zone.known_ms < start and before(zone)]
        new = [zone.id for zone in same if start <= zone.known_ms and before(zone)]
        first_touches = [(row["FIRST_TOUCH"].known_ms, zone.id)
                         for zone in same for row in (event_rows[zone.id],)
                         if "FIRST_TOUCH" in row and start <= row["FIRST_TOUCH"].known_ms < main]
        rejections = [(row["REJECTION_CONFIRMED"].known_ms, zone.id)
                      for zone in same for row in (event_rows[zone.id],)
                      if "REJECTION_CONFIRMED" in row and start <= row["REJECTION_CONFIRMED"].known_ms < main]
        available = [item for item in outcomes if item.plan.side == side and item.entry_bar is not None]
        fills = [(bars[row.entry_bar]["time"], row.plan.id) for row in available
                 if start <= bars[row.entry_bar]["time"] < main]
        audit.append({
            "start_utc": utc(start), "hour_close_utc": utc(stop), "main_displacement_utc": utc(main),
            "side": SIDES[side], "active_carried_before_main": carried, "new_zone_before_main": new,
            "first_touch_before_main_minutes": (min(first_touches)[0] - start) / 60_000 if first_touches else None,
            "rejection_before_main_minutes": (min(rejections)[0] - start) / 60_000 if rejections else None,
            "first_fill_before_main_minutes": (min(fills)[0] - start) / 60_000 if fills else None,
            "fills_before_main": [pid for _, pid in fills],
        })
    aggregate = {}
    for side in ("all", *SIDES.values()):
        selected = [row for row in audit if side == "all" or row["side"] == side]
        aggregate[side] = {
            "directional_hours": len(selected),
            "zone_seen_before_main": sum(bool(row["active_carried_before_main"] or row["new_zone_before_main"]) for row in selected),
            "new_zone_before_main": sum(bool(row["new_zone_before_main"]) for row in selected),
            "rejection_before_main": sum(row["rejection_before_main_minutes"] is not None for row in selected),
            "next_open_fill_before_main": sum(bool(row["fills_before_main"]) for row in selected),
        }
    return {"coverage": aggregate, "episode_ledger": audit}


def price_story(zone, row, bars):
    return {
        "id": zone.id, "side": SIDES[zone.side], "known_at_utc": utc(zone.known_ms), "origin_open_utc": utc(zone.origin_open_ms),
        "proximal": zone.proximal, "distal": zone.distal, "fvg": zone.fvg, "break_level": zone.break_level,
        "pivot_known_at_utc": utc(zone.pivot_known_ms),
        "transitions": {state: {"known_at_utc": utc(event.known_ms), "price": bars[event.bar_index]["close"],
                                "atr": event.atr, "reason": event.reason}
                        for state, event in row.items()},
    }


def negative_examples(zones, rows, bars, prior_m30, regimes):
    prior = [bar for bar in prior_m30 if bar["time_close"] <= bars[0]["time"]]
    contexts = tuple(context_rows(bars, earlier=prior))
    prior_atr = [context.prior_atr for context in closed_context(bars)]
    broken = [zone for zone in zones.values() if "INVALIDATED" in rows[zone.id]]
    missing = [zone for zone in zones.values() if None in (contexts[zone.formed_index].h1.side, contexts[zone.formed_index].h4.side)]
    sideways = [zone for zone in zones.values() if regimes.get(zone.known_ms // HOUR * HOUR) == "SIDEWAYS"
                and completion(rows[zone.id]) is not None and "REJECTION_CONFIRMED" not in rows[zone.id]]
    in_trend = [zone for zone in broken if (context := contexts[rows[zone.id]["INVALIDATED"].bar_index])
                and context.h1.side == context.h4.side == -zone.side]
    invalidation_bars = [(zone, rows[zone.id]["INVALIDATED"].bar_index) for zone in broken]
    shocks = (((bars[i]["high"] - bars[i]["low"]) / prior_atr[i], i, zone)
              for zone, i in invalidation_bars if prior_atr[i] is not None and prior_atr[i] > 0)
    shock = max(shocks, key=lambda row: (row[0], row[1], row[2].id), default=None)
    flat = [i for i, bar in enumerate(bars) if bar["high"] == bar["low"]]
    unsafe_flat = {event.bar_index for row in rows.values() for state, event in row.items()
                   if state in {"FORMATION_DETECTED", "REJECTION_CONFIRMED"}} & set(flat)
    require(not unsafe_flat, "Flat bars created or confirmed a zone")
    return {
        "confirmed_breaks": len(broken), "htf_missing_formation_count": len(missing),
        "trend_continuation_against_zone": [price_story(zone, rows[zone.id], bars) for zone in in_trend[:2]],
        "sideways_expired_or_replaced_without_rejection": [price_story(zone, rows[zone.id], bars) for zone in sideways[:2]],
        "missing_confirmed_htf_examples": [price_story(zone, rows[zone.id], bars) for zone in missing[:2]],
        "largest_invalidating_range_shock": {
            "range_over_prior_atr": shock[0], "known_at_utc": utc(bars[shock[1]]["time_close"]),
            "zone_id": shock[2].id, "actual_news_event": "UNKNOWN",
        } if shock else None,
        "zero_range_bars": [{"open_utc": utc(bars[i]["time"]), "known_utc": utc(bars[i]["time_close"])} for i in flat],
    }


def evaluate_panel(frame, bars, lanes, protocol, event_config, prior_m30):
    run = replay(bars, frame, protocol)
    begin, end = bars[WARMUP]["time"], bars[-1]["time_close"]
    require(end < json.loads(pinned(FORWARD_SEAL))["forward_start_ms"], "Sealed data is outside scope")
    zones, rows = sample_events(run, WARMUP)
    plans = [freeze_offer(zone, rejected, bars[rejected.bar_index])
             for zone in zones.values() if (rejected := rows[zone.id].get("REJECTION_CONFIRMED")) is not None]
    independent, regimes, catalog = independent_hours(bars, frame, event_config)
    cost_results = {}
    for name, costs in COSTS.items():
        outcomes, evidence = model_outcomes(plans, bars, costs, (begin, end))
        reservations = [(item.plan.bar, item.release, item.plan.id) for item in outcomes if item.entry_bar is not None]
        evidence["independent_moves"] = opportunity_coverage(independent, zones, rows, outcomes, bars)
        evidence["observed_approved_slot_overlap"] = {}
        for lane, (approved, occupied_slots, meta) in lanes.items():
            occupied = observed_native_slots(plans, occupied_slots)
            free = [plan for plan in plans if plan.id not in occupied]
            masked_outcomes = portfolio(free, bars, costs)
            control_replay = portfolio([plan for plan in approved if plan.bar >= WARMUP], bars, costs,
                                       use_s5_lock=lane == "S5_RAW")
            evidence["observed_approved_slot_overlap"][lane] = {
                "control_provenance": meta,
                "zone_offers_blocked_by_observed_approved": occupied,
                "hypothetical_zone_reservations_overlapping_approved_submissions":
                    observed_native_slots([plan for plan in approved if plan.bar >= WARMUP], reservations),
                "zone_only_metrics_when_native_slot_was_free": expectancy(masked_outcomes, bars, begin, end, bootstrap=True),
                "immutable_approved_offer_replay_metrics": expectancy(control_replay, bars, begin, end, bootstrap=True),
            }
        cost_results[name] = evidence
    return {
        "coverage_utc": {"loaded_first": utc(bars[0]["time"]), "scored_first": utc(begin), "last_close": utc(end)},
        "bars": len(bars), "independent_event_catalog": catalog,
        "lifecycle": {label: life_summary(zones, rows, bars, protocol["lifecycle"], side)
                      for side, label in ((0, "all"), *SIDES.items())},
        "diagnostic_offers": len(plans), "quote_side_execution": cost_results,
        "negative_examples": negative_examples(zones, rows, bars, prior_m30, regimes),
    }


def native_case(protocol):
    archive_bytes = checked(CASE_ARCHIVE, CASE_ARCHIVE_SHA)
    result = {}
    with ZipFile(BytesIO(archive_bytes)) as archive, TemporaryDirectory() as directory:
        for frame in FRAMES:
            path = extract(archive, f"baseline-M{frame}.csv", Path(directory))
            native = read_native(path)
            bars = [bar for bar in native.bars if bar["time_close"] <= CASE_END]
            require(bars[-1]["time_close"] == CASE_END, f"M{frame}: missing exact case cutoff")
            run = replay(bars, frame, protocol)
            zones, rows = sample_events(run, WARMUP)
            events = [event for event in run.events if CASE_START <= event.known_ms <= CASE_END and event.zone_id in zones]
            streams = tagged(path.read_bytes())
            ready = [row for row in map(json.loads, streams["RC1_READY"])
                     if CASE_START <= row["confirmed_ms"] <= CASE_END]
            snapshots = {row["known_ms"]: row for row in map(json.loads, streams["DTV_STATE"])}
            conflict = []
            for approval in ready:
                at = approval["confirmed_ms"]
                opposite = -1 if approval["direction"] == "BUY" else 1
                for zone in zones.values():
                    terminal = completion(rows[zone.id])
                    if zone.side == opposite and zone.known_ms <= at and (terminal is None or terminal.known_ms > at):
                        conflict.append({"known_at_utc": utc(at), "approved_direction": approval["direction"], "zone_id": zone.id})
            case_ids = {event.zone_id for event in events}
            decline_formations = [event for event in events if event.known_ms >= DECLINE and event.state == "FORMATION_DETECTED"]
            offers = [freeze_offer(zones[event.zone_id], event, bars[event.bar_index]) for event in events
                      if event.state == "REJECTION_CONFIRMED"]
            slot = {}
            for plan in offers:
                at = bars[plan.bar]["time_close"]
                view = snapshots.get(at)
                slot[plan.id] = ("UNKNOWN" if view is None else "OCCUPIED" if view["paper_phase"] == 2 or view["paper_capacity"]
                                 else "RESERVED" if view["paper_phase"] == 1 else "FREE")
            scenario = {}
            for cost_name, costs in COSTS.items():
                outcomes = portfolio(offers, bars, costs)
                decline_outcomes = [item for item in outcomes if bars[item.plan.bar]["time_close"] >= DECLINE]
                scenario[cost_name] = [
                    {**journal(item, bars), "actual_native_s5_raw_slot_at_decision": slot[item.plan.id]}
                    for item in decline_outcomes]
            result[f"M{frame}"] = {
                "matched_archived_market_bars": len(bars), "first_loaded_open": utc(bars[0]["time"]),
                "last_case_close": utc(bars[-1]["time_close"]),
                "case_transitions_by_side": dict(Counter(f"{SIDES[zones[e.zone_id].side]} {e.state}" for e in events)),
                "first_decline_buy_zone_known_at_utc": next((utc(e.known_ms) for e in decline_formations if zones[e.zone_id].side > 0), None),
                "first_decline_sell_zone_known_at_utc": next((utc(e.known_ms) for e in decline_formations if zones[e.zone_id].side < 0), None),
                "native_approved_opposition": conflict,
                "decline_hypothetical_rejection_offers": scenario,
                "zone_lifecycles_intersecting_case": [price_story(zone, rows[zone.id], bars) for zone in zones.values() if zone.id in case_ids],
                "scope": "Offline replay of zone logic on exact prior native market candles. Does not establish the new Pine event or execution parity.",
            }
    return {"source_zip_sha256": CASE_ARCHIVE_SHA, "windows": result}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True, help="Existing exact registered M5/M15/M30 bars")
    parser.add_argument("--s5-native", type=Path, required=True, help="Existing exact immutable TradingView S5 archives")
    parser.add_argument("--output", type=Path, default=HERE / "evidence" / "evaluation.json.gz")
    args = parser.parse_args()
    protocol = json.loads(checked(HERE / "PROTOCOL.json", PROTOCOL_SHA))
    require(protocol["evaluation"]["warmup_chart_bars"] == WARMUP, "Registered warmup changed")
    for label, costs in COSTS.items():
        frozen = protocol["evaluation"]["base_costs" if label == "normal" else "stress_costs"]
        require((costs.spread, costs.ticks, costs.commission)
                == (frozen["spread"], frozen["slippage_ticks"], frozen["commission_per_unit_side"]),
                f"Registered {label} costs changed")
    event_rules = json.loads(pinned(EVENT_CONFIG))["event"]
    broad, recent = read_registered(args.data_root, args.s5_native), read_matched()
    require(max(bars[-1]["time_close"] for data in (broad, recent) for bars, _, _ in data.values())
            < json.loads(pinned(FORWARD_SEAL))["forward_start_ms"], "Sealed inputs outside scope")
    overlaps = {f"M{a}_to_M{b}": assert_overlap(broad[a][0], broad[b][0]) for a, b in ((5, 15), (5, 30), (15, 30))}
    report = {
        "schema": "qirvanta.precision-zones.research-evaluation.v1", "status": "EXPERIMENTAL_INCONCLUSIVE",
        "protocol_sha256": PROTOCOL_SHA, "zone_replay_sha256": sha256((HERE / "replay.py").read_bytes()).hexdigest(),
        "provider": protocol["symbol"], "exact_archival_ohlc_cross_frame_aggregates": overlaps,
        "untouched_forward": "NOT_STARTED_NOT_ACCESSED",
        "methodology": {
            "archive": "All source archives and sealed dates checked against existing registered hashes. First 250 chart bars warmup; observations defined on confirmed closes.",
            "directional_coverage": "Existing complete UTC-hour labels and .75x prior-scale main close. Active carried zones and newly formed zones scored separately. A watch, rejection and fill are distinct.",
            "shadow": "One tick distal stop and hypothetical 1R/2R/3R at rejection close only. Existing absolute .20 ATR next-open deviation guard, $250 cap, three equal legs, quote-side stop-first ambiguity and 48-bar position timeout. Zone observation expiry remains 24 chart bars.",
            "execution_controls": "Observed native approved occupancy is a separate mask. Standalone overlap IDs show where the model would compete for an original approval. No order replay modifies or claims to improve S5/RC2.",
            "costs": {"normal": protocol["evaluation"]["base_costs"], "stress": protocol["evaluation"]["stress_costs"]},
            "limitations": "Known archive only. Fixed horizons require all contiguous bars and report censored observations. No scheduled news, intrabar sequence, actual spread or order-book liquidity can be inferred.",
        },
        "archive_windows": {}, "matched_native_control_windows": {}, "november_11_case": native_case(protocol),
    }
    for destination, panels in (("archive_windows", broad), ("matched_native_control_windows", recent)):
        for frame, (bars, lanes, identity) in panels.items():
            report[destination][f"M{frame}"] = {
                "source": identity, **evaluate_panel(frame, bars, lanes, protocol, event_rules, broad[30][0])}
            print(f"{destination} M{frame}: {len(bars)} bars", flush=True)
    payload = (json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(compress(payload, mtime=0) if args.output.suffix == ".gz" else payload)


if __name__ == "__main__":
    main()

