"""Audit the same 33 directional hours and replay existing Shadow continuation offers."""

import argparse
from collections import Counter
import csv
from gzip import compress
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from statistics import median
from subprocess import check_output
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from gold_pro.direct_breakout_research import BASE, EVENT_CONFIG, FORWARD_SEAL, WARMUP, WSB_ARCHIVE, independent_hours, pinned
from gold_pro.direct_trend_breakout import RETEST, scan
from gold_pro.rc2_research import early_observations
from research.data.loader import load_bars
from research.direct_trend_breakout.shadow_native import event_differences, read_native, utc
from research.m5_lead_entry_window.evaluate import baseline_case_observations
from research.professional_signal_selector.evaluate import checked, old_s5, tagged
from unified.tools.audit_tv_observations import inspect

from .audit_baseline import audit, load_prior_evaluation
from .radar import MINUTE, PRE_HOUR, clock, detection, key, lifecycle, warning_clocks


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
PROTOCOL_SHA = "c9788f9bab6d4ccebf669542b9e378cae8ca2cf4b8e6819f855da9c4ece7d177"
VISUAL = "37b903f46a2d878d9d0757da134620c22f2d067d"
VISUAL_FILE = "research/luminous_visual_v2/QIRVANTA_GOLD_PRO_VISUAL_V2.pine"
INPUTS = {
    VISUAL_FILE: "77feedcbef84e77b9270ec43840be5b823d838300d0a09d5ddb4fec7acc089f4",
    "research/luminous_visual_v2/evidence/native-pairs.zip": "fec83cd615923b210b6815852a69b74ed457f791ac41b3913a7c189f7d58f952",
    "gold_pro/direct_trend_breakout.py": "569b5b8833c1d72094c3388955db6d4147a4fa296ba14ef476d35e017e01c0e5",
    "gold_pro/direct_breakout_research.py": "7ec3b77c924c72b7fecac3c0faf299f24c2aeca0e39f9068bf127d7e66537909",
    "gold_pro/rc2_research.py": "c443a4b28899e2cc4b7fb334aea095a390c7d068b5175ea967bf5355bb1eb4c6",
}
NATIVE_ARCHIVE = "research/luminous_visual_v2/evidence/native-pairs.zip"


def crosscheck_shadow(native, replayed):
    def signature(event):
        return (event["time_ms"], event["side"], event["model"], event["state"], event["reason"],
                event["source_ms"], float(event["level"]))

    def unique_rows(rows):
        counts = Counter(map(signature, rows))
        if any(n > 1 for n in counts.values()):
            raise ValueError("Shadow source has duplicate event identity")
        return {signature(row): row for row in rows}

    actual, expected = unique_rows(native), unique_rows(replayed)
    native_only = actual.keys() - expected.keys()
    python_only = expected.keys() - actual.keys()
    shared = actual.keys() & expected.keys()
    return {
        "status": "MATCH" if not native_only and not python_only and not event_differences(native, replayed)
        else "SOURCE_MISMATCH",
        "shared_events": len(shared),
        "native_only": [event_view(actual[k]) for k in sorted(native_only)],
        "python_only": [event_view(expected[k]) for k in sorted(python_only)],
        "shared_payload_first_difference": event_differences(
            [row for row in native if signature(row) in shared],
            [row for row in replayed if signature(row) in shared],
        ),
    }


def source_crosscheck(seal):
    """Use existing native Visual V2 logs solely to cross-check the reused sources."""
    archive = checked(ROOT / NATIVE_ARCHIVE, INPUTS[NATIVE_ARCHIVE])
    frames = {}
    with TemporaryDirectory() as temp, ZipFile(BytesIO(archive)) as package:
        for frame in (5, 15, 30):
            path = Path(package.extract(f"candidate-M{frame}.csv", temp))
            native = read_native(path)
            if native.end is None or native.first_index != 0 or native.bars[-1]["time_close"] >= seal:
                raise ValueError("Invalid native cross-check prefix")
            end_index = native.end["index"]
            bars = native.bars[:end_index + 1]
            observed = [e for e in native.events if e["bar"] <= end_index]
            expected, _ = scan(bars)
            comparison = crosscheck_shadow(observed, expected)
            tags = tagged(path.read_bytes())
            early = (baseline_case_observations(tags) if frame == 5 else [
                {"known_ms": int(v.split("|")[3]), "side": 1 if v.split("|")[4] == "BUY" else -1,
                 "price": float(v.split("|")[5])} for v in tags["RC2_EARLY"]])
            reconstructed = early_observations(bars)
            audit_times = {json.loads(v)["known_ms"] for v in tags["DTV_STATE"]}
            comparable = [v for v in reconstructed if v["known_ms"] in audit_times]
            frames[f"M{frame}"] = {
                "sha256": native.sha256, "native_first_open_utc": utc(bars[0]["time"]),
                "native_last_close_utc": utc(bars[-1]["time_close"]), "confirmed_bars": len(bars),
                "native_shadow_events": len(observed), "reconstructed_shadow_events": len(expected),
                "shadow_parity": comparison,
                "native_rc2_early_count": len(early), "reconstructed_rc2_early_count": len(comparable),
                "rc2_early_in_audit_window_exact": early == comparable,
                "raw_paper_records": len(tags["DTV_PAPER"]), "luminous_records": len(tags["LZ_EVENT"]),
            }
    return frames


def prefix_check(bars, events, plans, snapshots, continuations, seal):
    if bars[-1]["time_close"] >= seal:
        raise ValueError("Invalid archive bounds")
    cuts = sorted({1, 14, WARMUP, len(bars) // 2, len(bars) - 1, *range(480, len(bars), 480)})
    for length in cuts:
        prefix = bars[:length]
        partial_events, partial_plans = scan(prefix, BASE)
        partial_snapshots, partial_cont, _ = lifecycle(prefix, partial_events)
        last = prefix[-1]["time_close"]
        if (partial_events != [e for e in events if e["time_ms"] <= last]
                or partial_plans != [p for p in plans if p.bar < length]
                or partial_snapshots != {k: v for k, v in snapshots.items() if k <= last}
                or partial_cont != {k: v for k, v in continuations.items() if k[0] <= last}):
            raise ValueError(f"Source or radar changed when future OHLC was withheld at bar {length}")
    return cuts


def event_view(row):
    fields = ("side", "model", "state", "reason", "level", "level_known_ms", "source_ms",
              "breakout_ms", "origin_model", "reasons", "plan_id", "obstacle", "net_room_r", "friction_r")
    return {"known_ms": row["time_ms"], "known_utc": utc(row["time_ms"]),
            **{k: row[k] for k in fields if k in row}}


def event_exits(result, side, start, stop):
    cohort = {}
    for lane, data in result["portfolios"].items():
        rows = data["journal"]
        cohort[lane] = [
            r for r in rows if r["side"] == side and r.get("entry_ms") is not None and start <= r["entry_ms"] < stop
        ]
    return cohort


def movement_clock(bars, event, first_observation):
    """Describe prices at the archived clock. Future range is only an outcome scale."""
    first, last = event["first_bar"], event["last_bar"]
    block = bars[first:last + 1]
    stamp = first_observation["known_ms"] if first_observation else None
    opening, side = block[0]["open"], event["side"]
    close_slice = bars[max(0, first - 1):last + 1]
    price = next((bar["close"] for bar in close_slice if bar["time_close"] == stamp), None)
    favorable = event["directional_extreme_distance"]
    return {
        "hour_open": opening, "hour_close": block[-1]["close"],
        "directional_open_to_close": side * (block[-1]["close"] - opening),
        "ex_post_directional_extreme_distance": favorable,
        "first_in_hour_observation_close_price": price,
        "directional_progress_at_observation": (side * (price - opening) if price is not None else None),
    }


def hour_report(hours, early_only, existing, events, snapshots, continuations, traces, economics):
    output = []
    for hour in hours:
        at, stop, side = (hour[k] for k in ("start_ms", "end_ms", "side"))
        in_window = [row for row in events if at - PRE_HOUR <= row["time_ms"] < stop]
        signed = [row for row in in_window if row["side"] == side]
        matches = [row for row in signed if clock(row) in continuations and row["state"] == "BREAKOUT CONFIRMED"]
        state_before = snapshots.get(at - 5 * MINUTE, {})
        state_on_open = snapshots.get(at, {})
        filled_s5 = hour["s5"]["first_same_side_fill"]
        by_cost = {}
        for cost, result in economics["costs"].items():
            gates = [row for row in result["gate_rows"] if row["side"] == side and at <= row["time_ms"] < stop]
            fills = event_exits(result, side, at, stop)
            by_cost[cost] = {"quality_gates": gates, "same_side_fills": fills}
        confirmed = [r for r in signed if r["state"] == "BREAKOUT CONFIRMED" and at <= r["time_ms"]]
        geometry = [r for r in signed if r["state"] == "ENTRY CANDIDATE" and at <= r["time_ms"]]
        source_rejections = [r for r in signed if r["state"] == "REJECTED" and at <= r["time_ms"]]
        actual = by_cost["normal"]["same_side_fills"]["all_protected_priority"]
        if actual:
            outcome = "RESEARCH_FILL"
        elif not confirmed:
            outcome = "NO_CONFIRMED_SHADOW_BREAK"
        elif not geometry:
            outcome = "ALL_SHADOW_GEOMETRY_REJECTED"
        elif not any(r["passed"] for r in by_cost["normal"]["quality_gates"]):
            outcome = "NO_SHADOW_ENTRY_PASSES_NEXT_OPEN"
        else:
            outcome = "PASSED_GATE_WITHOUT_SAME_SIDE_FILL_IN_HOUR"
        normal = by_cost["normal"]
        sources = sorted({r["reason"] for r in source_rejections})
        gate_reasons = sorted({r["reason"] for r in normal["quality_gates"] if not r["passed"]})
        outcome_reasons = sorted({
            row["state"] for row in economics["costs"]["normal"]["portfolios"]["all_protected_priority"]["journal"]
            if row["side"] == side and at <= row["decision_ms"] < stop and row.get("entry_ms") is None
        })
        blockers = {
            model: dict(Counter(reason for b in traces[at] if at <= b["known_ms"]
                                for reason in b[model]["blockers"]))
            for model in ("structure", "consolidation")
        }
        output.append({**hour, "rc2_clock": early_only[at], "existing_radar": existing[at],
                       "shadow_events": [event_view(row) for row in in_window],
                       "shadow_same_side_predicates": traces[at],
                       "in_hour_shadow_blocker_counts": blockers,
                       "linked_before_hour": state_before.get(side), "linked_at_hour_start": state_on_open.get(side),
                       "confirmed_continuations": [event_view(row) | {"linked_origin": continuations[clock(row)]}
                                                   for row in matches],
                       "first_same_side_shadow_confirmation_in_hour": event_view(confirmed[0]) if confirmed else None,
                       "same_side_source_rejections": sources,
                       "same_side_next_open_rejection_reasons": gate_reasons,
                       "same_side_portfolio_rejection_reasons": outcome_reasons,
                       "entry_diagnosis": outcome, "protected_fill_observed": bool(filled_s5),
                       "execution": by_cost})
    return output


def after_terminal(events, bounds):
    """Describe later independent origins after the earlier retest ceases to be active."""
    terminals, results = {}, []
    for row in events:
        stamp, side, model, state = (row[k] for k in ("time_ms", "side", "model", "state"))
        if not bounds[0] <= stamp < bounds[1]:
            continue
        if model == RETEST and state in {"EXPIRED", "INVALIDATED"}:
            terminals[side] = row
        elif model != RETEST and state == "BREAKOUT CONFIRMED":
            terminal = terminals.get(side)
            if terminal is not None and key(row) != key(terminal):
                results.append({"terminal": event_view(terminal), "new_origin": event_view(row),
                                "elapsed_minutes": (stamp - terminal["time_ms"]) / MINUTE})
                del terminals[side]
    return results


def evaluate():
    from .entry_quality import evaluate_entries
    from .shadow_trace import trace

    checked(HERE / "PROTOCOL.json", PROTOCOL_SHA)
    for name, digest in INPUTS.items():
        checked(ROOT / name, digest)
    base_bytes = check_output(["git", "show", f"{VISUAL}:{VISUAL_FILE}"], cwd=ROOT)
    if sha256(base_bytes).hexdigest() != INPUTS[VISUAL_FILE]:
        raise ValueError("Pinned Visual V2 Git object differs")
    seal = json.loads(pinned(FORWARD_SEAL))["forward_start_ms"]
    with TemporaryDirectory() as temp, ZipFile(BytesIO(pinned(WSB_ARCHIVE))) as archive:
        m5 = Path(temp) / "archival_m5_bars.csv"
        tape = Path(temp) / "archival_s5_native.csv"
        for path in (m5, tape):
            path.write_bytes(pinned(path.name, archive.read(path.name)))
        bars = load_bars(m5)
        if bars[-1]["time_close"] >= seal:
            raise ValueError("Sealed data excluded")
        # Audit all known prior decisions before evaluating the continuation subset.
        baseline = audit(bars, load_prior_evaluation(), inspect(tape))
        controls, _, native_slots, control_provenance = old_s5(bars, archive, Path(temp))
    bounds = (bars[WARMUP]["time"], bars[-1]["time_close"])
    catalog, categories, eligible = independent_hours(bars, 5, json.loads(pinned(EVENT_CONFIG))["event"])
    if [(h["start_ms"], h["side"]) for h in baseline["hours"]] != [
            (h["start"], h["side"]) for h in catalog if bounds[0] <= h["start"] and h["end"] <= bounds[1]]:
        raise ValueError("Significant hour identities changed")
    events, plans = scan(bars, BASE)
    snapshots, continuations, transitions = lifecycle(bars, events)
    cuts = prefix_check(bars, events, plans, snapshots, continuations, seal)
    early = early_observations(bars)
    early_clocks = warning_clocks(early, (), *bounds)
    existing_clocks = warning_clocks(early, events, *bounds)
    early_only, early_stats = detection(early_clocks, baseline["hours"], categories)
    existing, existing_stats = detection(existing_clocks, baseline["hours"], categories)
    traces = trace(bars, events, [
        {"start": row["start_ms"], "end": row["end_ms"], "side": row["side"]}
        for row in baseline["hours"]
    ])
    economics = evaluate_entries(bars, events, plans, set(continuations), controls, native_slots, bounds)
    hours = hour_report(baseline["hours"], early_only, existing, events, snapshots, continuations, traces, economics)
    by_hour = {row["start"]: row for row in catalog}
    for hour in hours:
        hour["movement_clock"] = movement_clock(
            bars, by_hour[hour["start_ms"]], hour["existing_radar"]["first_in_hour"],
        )
    risk_rejected_followups = []
    by_origin = {}
    for row in events:
        if row["state"] == "BREAKOUT CONFIRMED" and clock(row) in continuations:
            identity = continuations[clock(row)]["origin"]
            by_origin.setdefault(identity, []).append(row)
    for item in events:
        stamp = item["time_ms"]
        if (not bounds[0] <= stamp < bounds[1] or item["state"] != "REJECTED" or item["model"] == RETEST
                or item["reason"] == "INSUFFICIENT_DISPLACEMENT"):
            continue
        for later in by_origin.get(key(item), ()):
            if stamp < later["time_ms"] <= bounds[1]:
                risk_rejected_followups.append({"rejection": event_view(item), "later_confirmation": event_view(later)})
    scored_continuations = [row for row in events if row["state"] == "BREAKOUT CONFIRMED"
                            and clock(row) in continuations and bounds[0] <= row["time_ms"] <= bounds[1]]
    return {
        "status": "INCONCLUSIVE_RESEARCH_ONLY",
        "protocol_sha256": PROTOCOL_SHA, "input_sha256": INPUTS,
        "baseline_scope": baseline["scope"], "baseline_summary": baseline["summary"],
        "s5_control_provenance": control_provenance,
        "significant_hour_catalog": eligible, "hours": hours,
        "radar": {
            "rc2_early": early_stats, "existing_union": existing_stats,
            "persistence_only": existing_stats,
            "incremental_new_warning_clocks": 0,
            "same_side_retest_already_active_before_hour": sum(row["linked_before_hour"] is not None for row in hours),
            "continuations_in_scored_window": dict(Counter(r["model"] for r in scored_continuations)),
            "significant_hours_with_same_side_continuation": sum(bool(row["confirmed_continuations"]) for row in hours),
            "confirmed_followups_after_original_geometry_rejection": risk_rejected_followups,
            "new_independent_origins_after_retest_expiry_or_invalidation": after_terminal(events, bounds),
            "prefix_lengths_verified": cuts, "raw_states": dict(Counter(r["state"] for r in transitions)),
        },
        "entry_quality": economics,
        "archived_native_source_crosscheck": source_crosscheck(seal),
        "native_candidate": "NOT_APPLICABLE_NO_EVIDENCE_FOR_PINE_CHANGE",
        "independent_holdout": "NOT_AVAILABLE",
    }


def write_hours(result, path):
    def compact(blockers):
        return " / ".join(f"{reason} ({count})" for reason, count in Counter(blockers).most_common(3))

    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream, lineterminator="\n")
        writer.writerow(["hour_start_utc", "direction", "first_utc", "latency_minutes", "classification", "source",
                         "first_shadow_break_utc", "continued", "native_s5_fill", "normal_entry_diagnosis",
                         "source_reasons", "open_gate_reasons", "portfolio_reasons",
                         "rc2_frequent_blockers", "structure_frequent_blockers", "consolidation_frequent_blockers"])
        for row in result["hours"]:
            radar = row["existing_radar"]
            first = radar["first"]
            confirmation = row["first_same_side_shadow_confirmation_in_hour"]
            writer.writerow([row["start_utc"], row["direction"], utc(first["known_ms"]) if first else None,
                             radar["lead_minutes"], radar["classification"], " / ".join(first["sources"]) if first else "",
                             confirmation["known_utc"] if confirmation else None, len(row["confirmed_continuations"]),
                             row["protected_fill_observed"], row["entry_diagnosis"],
                             " / ".join(row["same_side_source_rejections"]),
                             " / ".join(row["same_side_next_open_rejection_reasons"]),
                             " / ".join(row["same_side_portfolio_rejection_reasons"]),
                             compact(row["rc2_early"]["in_hour_gate_blocker_counts"]),
                             compact(row["in_hour_shadow_blocker_counts"]["structure"]),
                             compact(row["in_hour_shadow_blocker_counts"]["consolidation"])])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = evaluate()
    args.output.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode("utf-8")
    (args.output / "evidence.json.gz").write_bytes(compress(raw, compresslevel=9, mtime=0))
    write_hours(result, args.output / "33_hours.csv")
    print(f"Audited {len(result['hours'])} significant hours.")


if __name__ == "__main__":
    main()
