"""Reproduce the frozen professional selector's historical ablation and native evidence audit."""

import argparse
from collections import Counter, defaultdict
from dataclasses import replace
import csv
from hashlib import sha256
from io import BytesIO, StringIO
import json
from pathlib import Path
from statistics import mean
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from gold_pro.archive import native_outcomes
from gold_pro.direct_breakout_research import (
    BASE, EVENT_CONFIG, FORWARD_SEAL, PINS, RC2_ARCHIVE, STRESS, WARMUP, WSB_ARCHIVE,
    event_audit, independent_hours, journal, native_plans, pinned, portfolio, simulate, stats,
)
from gold_pro.direct_trend_breakout import CONSOLIDATION, RETEST, STRUCTURAL, scan
from gold_pro.rc1_qa import Context, compare_exports, read_export
from gold_pro.rc2_research import chart_bars, native_campaigns
from research.data.loader import load_bars
from research.direct_trend_breakout.forensic_timeline import ms, utc
from research.direct_trend_breakout.shadow_native import event_differences, read_native
from research.validation.statistics import block_bootstrap, quantile, wilson
from unified.tools.audit_premium_s5 import verify_bars
from unified.tools.audit_tv_observations import inspect


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PROTOCOL_SHA = "131608a1cf59d2ba48875662b1e39bc571253728326a081bc7141b9b123d1564"
DT_CONTEXT = ROOT / "research/direct_trend_breakout/evidence/base-native-logs.zip"
MODELS = {"P1_BREAKOUT": {STRUCTURAL, CONSOLIDATION}, "P2_RETEST": {RETEST}}
SIDES = {1: "BUY", -1: "SELL"}
HOUR = 3_600_000
COSTS = {"normal": BASE, "stress": STRESS}
NATIVE_PINS = {
    5: "350c89a24481b1947495049faa704a70501b3be73825601a675aea441616211d",
    15: "b766cf135ced65580a52b38e429ac61df1a2af780aa84104a06ae6a55aeb7e09",
    30: "78b46c01496da6b2d97145f09dede89b04223d338db6bc12851d227e9f154793",
}
PARENT_SOURCE = ROOT / "research/signal_quality_reliability/QIRVANTA_GOLD_PRO_SIGNAL_QUALITY.pine"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def checked(path, digest):
    raw = path.read_bytes()
    require(sha256(raw).hexdigest() == digest, f"Changed input: {path}")
    return raw


def extract(archive, name, directory):
    path = directory / name
    path.write_bytes(archive.read(name))
    return path


def old_s5(bars, archive, directory):
    path = extract(archive, "archival_s5_native.csv", directory)
    pinned(path.name, path.read_bytes())
    native = inspect(path)
    require(native["mode"] == "S5" and not native["unfilled"], "Invalid admitted S5 control")
    verify_bars(bars, native["run"])
    original = native_outcomes(native, BASE)
    native_slots = [(row.plan.bar, row.release, row.plan.id) for row in original]
    plans = [replace(row.plan, model="S5_RAW") for row in original]
    return plans, [], native_slots, {
        "provenance": "Matched immutable archived native S5; originally submitted plans only. RC2 unavailable.",
        "sha256": PINS[path.name], "ohlc_fingerprints": [native["run"]["checksum1"], native["run"]["checksum2"]],
        "native_campaigns": len(original), "native_closed": sum(row.state == "CLOSED" for row in original),
        "observed_native_closed_cash_before_incremental_spread_usd": sum(
            row.net + row.plan.units * BASE.spread * BASE.pointvalue for row in original if row.state == "CLOSED"),
    }


def native_rc2_control(bars, run, frame, lane):
    plans, decisions = native_plans(run, frame, lane)
    campaigns, disposition = native_campaigns(run)
    index = {bar["time_close"]: i for i, bar in enumerate(bars)}
    native = defaultdict(list)
    for row in run.native:
        native[row["plan_id"]].append(row)
    slots = []
    for pid, events in native.items():
        submission = next((row for row in events if row["event"] == "NATIVE_SUBMITTED"), None)
        terminal = next((row for row in events if row["event"] in {"NATIVE_CLOSED", "NATIVE_NOT_FILLED"}), None)
        if submission:
            slots.append((index[submission["known_time"]], index[terminal["known_time"]] if terminal else len(bars), pid))
    return plans, decisions, slots, {
        "provenance": "Pinned native matched RC2 audit, exact chart context, CORE and READY within both audit lanes.",
        "native_campaign_disposition": disposition,
        "native_closed_campaigns": sum(row.closed for row in campaigns),
        "observed_native_closed_cash_before_incremental_spread_usd": sum(
            part.net_before_spread for row in campaigns if row.closed for part in row.exits),
        "native_approved_ledger_slots": len(slots),
    }


def read_panels(protocol):
    windows = protocol["data"]["research_windows_utc"]
    with TemporaryDirectory() as scratch:
        directory = Path(scratch)
        with ZipFile(BytesIO(pinned(WSB_ARCHIVE))) as previous:
            path = extract(previous, "archival_m5_bars.csv", directory)
            pinned(path.name, path.read_bytes())
            bars = load_bars(path)
            yield "M5_long", 5, bars, {"S5_RAW": old_s5(bars, previous, directory)}, windows["M5_long"]
        with ZipFile(BytesIO(pinned(RC2_ARCHIVE))) as archive:
            for frame in (5, 15):
                fixed = read_export(extract(archive, f"fixed_M{frame}.csv", directory), frame, "audit")
                parity = read_export(extract(archive, f"parity_M{frame}.csv", directory), frame, "audit")
                match = compare_exports(fixed, parity, ("core", "ready"))
                require(match["status"] == "MATCH", f"M{frame} immutable control mismatch")
                bars = chart_bars(fixed)
                lanes = {lane: native_rc2_control(bars, run, frame, lane)
                         for lane, run in (("S5_RAW", parity), ("RC2", fixed))}
                name = f"M{frame}_matched"
                yield name, frame, bars, lanes, windows[name]


def admission(plan, event, bars, thresholds):
    bar = bars[plan.bar]
    require(event["time_ms"] == bar["time_close"] and event["bar"] == plan.bar and event["side"] == plan.side,
            f"Original event identity mismatch: {plan.id}")
    require(event["level_known_ms"] <= event["time_ms"], f"Future structural level: {plan.id}")
    if plan.model == RETEST:
        require(event["breakout_ms"] is not None and event["breakout_ms"] < event["time_ms"],
                f"Original retest before its originating breakout: {plan.id}")
    reasons = []
    if event["level_known_ms"] >= event["time_ms"] or plan.side * (bar["close"] - event["level"]) <= 0:
        reasons.append("FROZEN_EDGE_NOT_CONFIRMED")
    signed_body = plan.side * (bar["close"] - bar["open"]) / event["prior_atr"]
    if signed_body < thresholds["body_atr"]:
        reasons.append("INSUFFICIENT_SIGNED_BODY")
    if event["obstacle"] is None:
        reasons.append("OPPOSING_STRUCTURAL_BARRIER_UNKNOWN")
    if event["net_room_r"] is None or event["friction_r"] is None:
        reasons.append("ROOM_OR_MODELED_COST_UNKNOWN")
    elif event["net_room_r"] < thresholds["minimum_net_room_r"]:
        reasons.append("INSUFFICIENT_NET_ROOM")
    return tuple(reasons), signed_body


def reclaim(plan, event, bars):
    if plan.model == RETEST:
        return None
    i = plan.bar
    lookahead = bars[i + 1:i + 4]
    if len(lookahead) < 3 or any(bar["time"] != bars[i + j]["time_close"] for j, bar in enumerate(lookahead)):
        return None
    back = [bar["time_close"] for bar in lookahead if plan.side * (bar["close"] - event["level"]) < 0]
    return {"first_reclaim_utc": utc(back[0]) if back else None, "all_three": len(back) == 3}


def reclaim_counts(plans, labels):
    observed = [labels[plan.id] for plan in plans if labels[plan.id] is not None]
    false = sum(row["first_reclaim_utc"] is not None for row in observed)
    return {
        "eligible_original_breakout_edges": len(observed), "reclaimed_within_next_three": false,
        "reclaim_rate": false / len(observed) if observed else None, "wilson_95": wilson(false, len(observed)),
        "alternative_all_three_closes_back_through": sum(row["all_three"] for row in observed),
    }


def observed_native_slots(plans, slots):
    occupied = {}
    for plan in plans:
        overlapping = [pid for start, release, pid in slots if start <= plan.bar < release]
        if overlapping:
            occupied[plan.id] = overlapping
    return occupied


def expectancy(outcomes, bars, begin, end, side=0, bootstrap=False):
    group = [outcome for outcome in outcomes if outcome.state == "CLOSED"
             and begin <= bars[outcome.entry_bar]["time"] < end and bars[outcome.release]["time_close"] <= end
             and (not side or outcome.plan.side == side)]
    group.sort(key=lambda outcome: (outcome.release, outcome.plan.id))
    rs = [row.net / row.risk_usd for row in group]
    wins, losses = [row.net for row in group if row.net > 0], [row.net for row in group if row.net < 0]
    return {
        **stats(outcomes, bars, begin, end, side),
        "win_rate_wilson95": wilson(len(wins), len(group)),
        "average_win_usd": mean(wins) if wins else None,
        "average_loss_usd": mean(losses) if losses else None,
        "average_win_to_loss": -mean(wins) / mean(losses) if wins and losses else None,
        "worst_r": min(rs) if rs else None, "p05_r": quantile(rs, .05),
        "mean_worst_five_percent_r": mean(sorted(rs)[:max(1, (len(rs) + 19) // 20)]) if rs else None,
        "mean_r_block_bootstrap": block_bootstrap(rs) if bootstrap else None,
    }


def cohort_statistics(outcomes, bars, bounds):
    return {key: {label: expectancy(outcomes, bars, start, end, side, key == "full" and side == 0)
                  for side, label in ((0, "all"), *SIDES.items())}
            for key, (start, end) in bounds.items()}


def offers_status(outcomes):
    closed = [row for row in outcomes if row.state == "CLOSED"]
    return {"states": dict(Counter(row.state for row in outcomes)), "closed": len(closed),
            "winners": sum(row.net > 0 for row in closed), "losers": sum(row.net < 0 for row in closed),
            "closed_net_usd": sum(row.net for row in closed)}


def transition_difference(original, changed):
    orig = {row.plan.id: row for row in original}
    new = {row.plan.id: row for row in changed}
    removed = [row for pid, row in orig.items() if row.entry_bar is not None
               and (pid not in new or new[pid].entry_bar is None)]
    recovered = [row for pid, row in new.items() if row.entry_bar is not None
                 and (pid not in orig or orig[pid].entry_bar is None)]
    return {
        "original_fills_removed": offers_status(removed), "removed_ids": [row.plan.id for row in removed],
        "newly_filled_through_capacity": offers_status(recovered), "new_ids": [row.plan.id for row in recovered],
    }


def source_prefix_audit(bars, observations, plans):
    cuts = sorted({WARMUP, len(bars) // 2, len(bars) * 3 // 4, len(bars) - 1, len(bars),
                   *(plan.bar + 1 for plan in plans if plan.bar >= WARMUP)})
    for size in cuts:
        part_observations, part_plans = scan(bars[:size])
        require(part_observations == [row for row in observations if row["bar"] < size]
                and part_plans == [plan for plan in plans if plan.bar < size],
                f"Source leaked future bars at prefix {size}")
    return len(cuts)


def eval_panel(name, frame, bars, lanes, window, protocol, rules):
    begin, split, end = (ms(window[key]) for key in ("score_first", "descriptive_split", "last"))
    bounds = {"full": (begin, end), "early_60": (begin, split), "later_40": (split, end)}
    require((bars[0]["time"], bars[WARMUP]["time"], bars[-1]["time_close"])
            == (ms(window["load_first"]), begin, end), f"{name}: frozen data window changed")
    require(end < json.loads(pinned(FORWARD_SEAL))["forward_start_ms"], "Sealed forward data cannot be used")
    events, regimes, catalog = independent_hours(bars, frame, rules)
    events = [row for row in events if begin <= row["start"] and row["start"] + HOUR <= end]
    source, original = scan(bars)
    source_prefixes = source_prefix_audit(bars, source, original)
    plans = [plan for plan in original if plan.bar >= WARMUP]
    event_by_id = {row["plan_id"]: row for row in source if row["state"] == "ENTRY CANDIDATE"}
    labels = {plan.id: reclaim(plan, event_by_id[plan.id], bars) for plan in plans}
    verdicts = {plan.id: admission(plan, event_by_id[plan.id], bars, protocol["policies"]["shared"]) for plan in plans}
    parent = {cost: portfolio(plans, bars, model) for cost, model in COSTS.items()}
    cohorts = {"PARENT_SHADOW": plans}
    cohorts.update({key: [plan for plan in plans if plan.model in models and not verdicts[plan.id][0]]
                    for key, models in MODELS.items()})
    approved_replays = {
        lane: {cost: portfolio([plan for plan in original if plan.bar >= WARMUP], bars, model, use_s5_lock=lane == "S5_RAW")
               for cost, model in COSTS.items()} for lane, (original, _, _, _) in lanes.items()
    }
    controls = {}
    for lane, (_, decisions, _, meta) in lanes.items():
        outcomes = approved_replays[lane]
        controls[lane] = {
            **meta,
            "metrics": {cost: cohort_statistics(rows, bars, bounds) for cost, rows in outcomes.items()},
            "directional_opportunities_normal": event_audit(events, regimes, decisions, outcomes["normal"], bars),
            "ledger": {cost: [journal(row, bars) for row in rows] for cost, rows in outcomes.items()},
        }
    policies = {}
    for key, admitted in cohorts.items():
        ids = {plan.id for plan in admitted}
        in_scope = plans if key == "PARENT_SHADOW" else [plan for plan in plans if plan.model in MODELS[key]]
        excluded = [plan for plan in in_scope if plan.id not in ids]
        models = MODELS.get(key)
        model_observations = source if models is None else [row for row in source if row["model"] in models]
        occupied = {lane: observed_native_slots(admitted, value[2]) for lane, value in lanes.items()}
        results = {}
        for cost, model in COSTS.items():
            replayed = parent[cost] if key == "PARENT_SHADOW" else portfolio(admitted, bars, model)
            research_slots = [(row.plan.bar, row.release, row.plan.id) for row in replayed if row.entry_bar is not None]
            original_model = [row for row in parent[cost] if models is None or row.plan.model in models]
            results[cost] = {
                "metrics": cohort_statistics(replayed, bars, bounds),
                "directional_opportunities": event_audit(events, regimes, model_observations, replayed, bars),
                "parent_differences": transition_difference(parent[cost], replayed),
                "parent_same_model_fill_differences": transition_difference(original_model, replayed),
                "parent_exclusions_same_model": offers_status([simulate(plan, bars, model) for plan in excluded]),
                "ablation_after_blocking_observed_approved_occupancy": {
                    lane: {"offers_blocked_at_admission": len(blocked),
                           "metrics": expectancy(portfolio([plan for plan in admitted if plan.id not in blocked],
                                                           bars, model), bars, begin, end)}
                    for lane, blocked in occupied.items()} if key != "PARENT_SHADOW" else {},
                "control_would_be_occupied_by_standalone_shadow": {
                    lane: observed_native_slots([row.plan for row in approved_replays[lane][cost]], research_slots)
                    for lane in lanes},
                "ledger": [journal(row, bars) for row in replayed],
            }
        policies[key] = {
            "status": "UNCHANGED_PARENT" if key == "PARENT_SHADOW" else "PARTIAL_EVIDENCE_ABLATION",
            "in_scope_original_offers": len(in_scope), "admitted_in_ablation": len(admitted),
            "excluded_same_model_reasons": dict(Counter(reason for plan in excluded for reason in verdicts[plan.id][0])),
            "strict_native_htf_unknown_offers": len(in_scope), "full_selector_fills": None,
            "parent_same_model_reclaims": reclaim_counts(in_scope, labels),
            "selected_original_reclaims": reclaim_counts(admitted, labels),
            "approved_native_slot_occupied_at_offer": occupied,
            "economics": results,
        }
    return {
        "window": window, "source_bars": len(bars), "source_prefixes_exact": source_prefixes,
        "source_event_count": len(source), "source_states": dict(Counter(row["state"] for row in source if row["bar"] >= WARMUP)),
        "source_reasons_rejected": dict(Counter(reason for row in source if row["bar"] >= WARMUP and row["state"] == "REJECTED"
                                               for reason in row["reasons"])),
        "independent_hour_catalog": catalog,
        "complete_scored_directional_hours": dict(Counter(SIDES[row["side"]] for row in events)),
        "scored_price_regimes": dict(Counter(kind for stamp, kind in regimes.items() if begin <= stamp <= end - HOUR)),
        "model_policy": policies, "separate_immutable_controls": controls,
        "offer_evidence": [
            {"id": plan.id, "source": plan.model, "side": SIDES[plan.side], "known_at_utc": utc(event_by_id[plan.id]["time_ms"]),
             "level_known_at_utc": utc(event_by_id[plan.id]["level_known_ms"]),
             "source_level": event_by_id[plan.id]["level"], "known_opposing_barrier": event_by_id[plan.id]["obstacle"],
             "net_room_r": event_by_id[plan.id]["net_room_r"], "signed_body_prior_atr": verdicts[plan.id][1],
             "reasons": verdicts[plan.id][0], "reclaim": labels[plan.id], "htf": "UNKNOWN",
             "classification": "RESEARCH ONLY", "trade_approved": False}
            for plan in plans],
    }


def tagged(raw):
    rows = defaultdict(list)
    for row in csv.DictReader(StringIO(raw.decode("utf-8-sig"), newline="")):
        for line in row["Message"].splitlines():
            kind, sep, value = line.partition("|")
            if sep:
                rows[kind].append(value)
    return rows


def native_triage(signal, confirmed_sides):
    if "RESEARCH REJECTED:" in signal["reason"]:
        return "REJECT / EXPIRED"
    if len(confirmed_sides) > 1:
        return "CONFLICT"
    if signal["stage"] == "WATCH":
        return "OBSERVE"
    return "NO DATA"


def audit_native_context(protocol, folder):
    expected = protocol["data"]["direct_trend_context_zip_sha256"]
    with ZipFile(BytesIO(checked(DT_CONTEXT, expected))) as archive:
        traces = {frame: {row["known_ms"]: row for row in map(json.loads, tagged(
            archive.read(f"indicator-default-M{frame}.csv"))["DT_BASE"])} for frame in (5, 15)}
    common = traces[5].keys() & traces[15].keys()
    fields = ("h1_known_ms", "h4_known_ms", "h1_bias", "h4_bias")
    summary = {
        "pinned_archive_sha256": expected, "native_shared_case_closes": len(common),
        "native_agreement_same_utc": {key: sum(traces[5][at][key] == traces[15][at][key] for at in common)
                                     for key in fields},
        "use": "These prior-capture biases describe their own warmup. Missing exact event-time native biases in matched archival cohorts stay UNKNOWN.",
    }
    if folder is None:
        summary["fresh_parent"] = {"status": "OPTIONAL_LOCAL_TRADINGVIEW_EXPORTS_NOT_SUPPLIED"}
        return summary
    frames = {}
    for frame, digest in NATIVE_PINS.items():
        path = folder / f"baseline-M{frame}.csv"
        rows = tagged(checked(path, digest))
        tape = read_native(path)
        shadow, _ = scan(tape.bars)
        diff = event_differences(tape.events, shadow, tape.first_index)
        if diff is not None:
            # Native JSON can contain Decimal while the local replay contains float.
            diff = json.loads(json.dumps(diff, default=str))
        contexts = {ctx.closing: ctx for ctx in map(lambda row: Context.parse(row, frame), rows["RC1_CTX"])}
        ready = {row["confirmed_ms"]: row for row in map(json.loads, rows["RC1_READY"])}
        market = {row["time_close"]: row for row in tape.bars}
        sources = Counter()
        permissions = Counter()
        observed = {}
        for row in map(json.loads, rows["DTV_STATE"]):
            now = row["known_ms"]
            require(now in market and now in contexts, f"M{frame}: missing confirmed OHLC/HTF clock")
            require(all(t is None or t <= contexts[now].opening for t in contexts[now].htf_closes),
                    f"M{frame}: future HTF clock")
            approved = row["permission"] in {"BUY READY", "SELL READY"}
            if approved:
                require(now in ready and row["plan_visible"], f"M{frame}: approval without native READY")
            permissions[row["permission"]] += 1
            confirmed_sides = {item["side"] for item in row["signals"] if item["stage"] == "BREAKOUT CONFIRMED"}
            for signal in row["signals"]:
                at, expires = signal["known_ms"], signal["expires_ms"]
                require(at <= now < expires, f"M{frame}: stale/future direction {signal}")
                key = signal["source"], signal["model"], signal["side"], signal["stage"], at
                if key not in observed:
                    sources[signal["source"]] += 1
                    exact_prior = traces.get(frame, {}).get(at)
                    observed[key] = {
                        "event_id": f"OANDA:XAUUSD|{frame}|" + "|".join(map(str, key)), "symbol": "OANDA:XAUUSD",
                        "timeframe_minutes": frame, "source": key[0], "model": key[1], "direction": SIDES[key[2]],
                        "stage": key[3], "known_at_utc": utc(at), "expiry_utc": utc(expires),
                        "first_observed_panel_close_utc": utc(now),
                        "existing_permission_at_observation": row["permission"] if now == at else None,
                        "native_slot_phase_at_observation": row["paper_phase"] if now == at else None,
                        "parent_conflict_at_observation": row["direction"] == "CONFLICT" if now == at else None,
                        "display_reason": signal["reason"],
                        "triage_at_observation": native_triage(signal, confirmed_sides) if now == at else "NO DATA",
                        "observation_trade_approved": False,
                        "strict_native_htf_alignment": "UNKNOWN",
                        "prior_capture_h1_h4_sides": [exact_prior["h1_bias"], exact_prior["h4_bias"]] if exact_prior else None,
                    }
        case_begin, case_end = min(contexts), max(contexts)
        raw_states = [row for row in shadow if case_begin <= row["time_ms"] <= case_end]
        frames[f"M{frame}"] = {
            "sha256": digest, "bars": len(tape.bars), "closed_parent_panel": len(contexts), "observations": len(observed),
            "observations_by_source": dict(sources), "legacy_early_observations": len(rows["RC2_EARLY"]),
            "parent_permissions_at_confirmed_closes": dict(permissions),
            "parent_ready_only": [ready[when] for when in sorted(ready)],
            "parent_raw_paper_event_counts": dict(Counter(row["event"] for row in map(json.loads, rows["DTV_PAPER"]))),
            "native_shadow_exact_replay": diff is None, "first_native_shadow_difference": diff,
            "shadow_source_lifecycle": raw_states,
            "ledger": list(observed.values()),
        }
    summary["fresh_parent"] = frames
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-dir", type=Path, help="Optional pinned local TradingView parent captures")
    parser.add_argument("--output", type=Path, default=HERE / "evaluation.json")
    args = parser.parse_args()
    protocol = json.loads(checked(HERE / "PROTOCOL.json", PROTOCOL_SHA))
    checked(PARENT_SOURCE, protocol["baseline_pine_sha256"])
    pinned(FORWARD_SEAL)
    report = {
        "schema": "qirvanta.professional-selector.evaluation.v1", "status": "INCONCLUSIVE_RESEARCH_ONLY",
        "protocol_sha256": PROTOCOL_SHA,
        "reproducibility": "Run python -X utf8 -B -m research.professional_signal_selector.evaluate from the repository root. Non-shallow Git must include 2a3463db4c8595bc049681d9922d8a927044b8f2. Optional --native-dir is the local TradingView capture directory; hashes are verified.",
        "scope": "No full native HTF selector claim or executable policy. Historical ablation uses only causal source geometry, body and modeled costs, separate approved slot evidence, and ignores UNKNOWN HTF for the ablation only. Standalone one-slot Shadow hypotheses do not compete for or change approved S5/RC2 orders. Controls replay originally observed approvals at identical quote-side cost assumptions and keep native evidence separate.",
        "false_reclaim": "Strict return through a previously frozen breakout edge within the next three contiguous closes; require all three to be observed. Retest entries have no new originating breakout, so a P2 entry has no primary breakout denominator.",
        "statistical_scope": "Wilson 95% reclaim intervals, five-campaign block-bootstrap on full samples and descriptive chronological slices. No statistical test or Holm-adjusted claims on exposed history.",
        "execution_assumptions": protocol["execution"],
        "native_context": audit_native_context(protocol, args.native_dir),
        "untouched_selector": {**protocol["data"]["untouched_selector"], "status": "NOT_AVAILABLE_NOT_ACCESSED"},
        "windows": {},
    }
    rules = json.loads(pinned(EVENT_CONFIG))["event"]
    for name, frame, bars, lanes, window in read_panels(protocol):
        report["windows"][name] = eval_panel(name, frame, bars, lanes, window, protocol, rules)
        print(f"{name}: {len(bars)} pinned OANDA bars", flush=True)
    args.output.write_text(json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                           encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()

