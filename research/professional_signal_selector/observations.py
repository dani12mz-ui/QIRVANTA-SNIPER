"""Normalize closed-bar native observations without changing their permissions."""

import argparse
from collections import Counter, defaultdict
import csv
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from math import isclose
import json
from pathlib import Path

from gold_pro import rc1_qa, rc2_qa
from gold_pro.direct_trend_breakout import CONSOLIDATION, RETEST, STRUCTURAL, closed_context
from research.direct_trend_breakout import shadow_native
from research.direct_trend_visibility import native_qa


ROOT = Path(__file__).resolve().parents[2]
BASELINE_QA = ROOT / "research/signal_quality_reliability/evidence/native-qa.json"
HTF_SOURCE = ROOT / "research/direct_trend_breakout/evidence/forensic-timeline-M15.csv"
HTF_SHA = "b5773bb4c69e265cf06043eb352abe2158ecf48a4353f4804574061ec68d264f"
PROTOCOL_SHA = "131608a1cf59d2ba48875662b1e39bc571253728326a081bc7141b9b123d1564"
PRIOR_FILES = {5: "repeat-M5.csv", 15: "fresh-candidate-M15.csv", 30: "fresh-candidate-M30.csv"}
SIDES = {1: "BUY", -1: "SELL"}
TERMINAL = {"CANCELLED", "EXPIRED", "INVALIDATED", "REJECTED"}
OHLC = ("open", "high", "low", "close")


@dataclass(frozen=True)
class Tape:
    frame: int
    market: tuple
    snapshots: tuple
    shadow: dict
    core: dict
    ready: dict
    early: dict

    def prefix(self, stamp):
        return replace(self, market=tuple(bar for bar in self.market if bar["time_close"] <= stamp),
                       snapshots=tuple(row for row in self.snapshots if row["known_ms"] <= stamp),
                       **{name: {time: rows for time, rows in getattr(self, name).items() if time <= stamp}
                          for name in ("shadow", "core", "ready", "early")})


def digest(raw):
    return sha256(raw).hexdigest()


def encode(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False, default=str) + "\n").encode()


def utc(stamp):
    return datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def epoch(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def groups(rows, field):
    result = defaultdict(list)
    for row in rows:
        result[row[field]].append(row)
    return dict(result)


def source_tape(path, frame):
    run = rc1_qa.read_export(path, frame, "indicator")
    shadow = shadow_native.read_native(path)
    early = rc2_qa.early_events(rc2_qa.extras(path)["RC2_EARLY"], run, frame)
    validation = native_qa.verify_visibility(path, frame)
    if validation["status"] != "VERIFIED" or not shadow.end:
        raise ValueError(f"Incomplete native observation tape: {path.name}")
    snapshots = tuple(native_qa.journal_rows(path)["DTV_STATE"])
    by_close = {bar["time_close"]: bar for bar in shadow.bars}
    for bar in run.context:
        known = by_close[bar.closing]
        if bar.opening != known["time"] or tuple(map(float, bar.ohlc)) != tuple(known[key] for key in OHLC):
            raise ValueError(f"Different native market inputs at {utc(bar.closing)}")
    return Tape(frame, shadow.bars, snapshots, groups(shadow.events, "time_ms"),
                groups(run.core, "known_time"), groups(run.ready, "confirmed_ms"), early)


def archived_htf():
    if digest(HTF_SOURCE.read_bytes()) != HTF_SHA:
        raise ValueError("Changed archived native HTF source")
    with HTF_SOURCE.open(encoding="utf-8", newline="") as stream:
        return {epoch(row["known_utc"]): row for row in csv.DictReader(stream)}


def htf_alignment(frame, stamp, bar, side, archive):
    row = archive.get(stamp) if frame == 15 else None
    empty = {"status": "DATA MISSING", "h1": None, "h4": None, "source": None}
    if not row or epoch(row["open_utc"]) != bar["time"] or any(float(row[key]) != bar[key] for key in OHLC):
        return empty
    frames = {}
    for timeframe in ("h1", "h4"):
        state = row[f"{timeframe}_native_bias"]
        closed = row[f"{timeframe}_last_closed_utc"]
        if state not in {"UP", "DOWN", "MIXED"} or not closed or epoch(closed) > stamp:
            return empty
        frames[timeframe] = {"bias": state, "closed_utc": closed}
    aligned = "UP" if side > 0 else "DOWN"
    opposite = "DOWN" if side > 0 else "UP"
    biases = {frames["h1"]["bias"], frames["h4"]["bias"]}
    status = "ALIGNED" if biases == {aligned} else "COUNTERTREND" if biases == {opposite} else "MIXED"
    # The older archive proves its own closed HTF values. Matching local OHLC
    # does not prove identical HTF warmup in the newer native run.
    return {"status": "DATA MISSING", "archived_status": status, **frames,
            "source": "archived native M15, exact local OHLC join, HTF warmup unverified"}


def snapshot_context(snapshot):
    signals = snapshot["signals"]
    seen = set()
    for signal in signals:
        identity = native_qa.signal_keys(signal, snapshot["known_ms"])
        model_key = (identity, signal["model"])
        if model_key in seen:
            raise ValueError("Duplicate identity in one native snapshot")
        seen.add(model_key)
    phase = snapshot["paper_phase"]
    if phase not in (0, 1, 2):
        raise ValueError("Unknown source occupancy phase")
    slot = "OCCUPIED" if snapshot["paper_capacity"] or phase == 2 else "RESERVED" if phase == 1 else "FREE"
    return {
        "directional_opportunity": snapshot["direction"],
        "permission": snapshot["permission"],
        "permission_source": snapshot["permission_source"],
        "permission_plan_visible": snapshot["plan_visible"],
        "slot": slot,
        "slot_source": "native S5 RAW PAPER at the same close",
        "slot_plan_id": snapshot["paper_plan_id"] or None,
        "active_observation_sources": [{key: signal[key] for key in ("source", "model", "side", "stage", "known_ms", "expires_ms")}
                                       for signal in signals],
    }


def plan(row, classification, native_cost=None):
    entry, stop, targets = row
    return {"classification": classification, "entry_reference": entry, "initial_sl": stop,
            "targets": targets, "modeled_cost": native_cost, "broker_cost_status": "BROKER COSTS UNKNOWN"}


def source_edge(level, level_known, stamp, close, side, kind):
    if level is None or level_known is None:
        return None
    if level_known > stamp:
        raise ValueError(f"Frozen level unavailable before {utc(stamp)}")
    if level_known == stamp:
        return None
    return {"level": level, "known_at_utc": utc(level_known), "source": kind,
            "close_beyond_in_direction": (close - float(level)) * side > 0}


def triage(row, confirmed_sides, rules):
    state, alignment = row["state"], row["htf"]["status"]
    geometry = row["plan"]
    if state in TERMINAL or row.get("source_gate_rejections") or (
            geometry and geometry.get("net_structural_room_r") is not None
            and float(geometry["net_structural_room_r"]) < rules["minimum_net_room_r"]):
        return "REJECT / EXPIRED", "SOURCE_TERMINAL_OR_KNOWN_RISK_FAILURE"
    if row["confirmed_observation"] and len(confirmed_sides) > 1:
        return "CONFLICT", "OPPOSING_CONFIRMED_OBSERVATIONS"
    if state in {"WATCH", "RETEST WATCH"}:
        return "OBSERVE", "WATCH_ONLY"
    if row["approval"] == "S5 APPROVED":
        return "OBSERVE", "SOURCE_APPROVAL_UNCHANGED"
    weak = []
    if alignment in {"MIXED", "COUNTERTREND"}:
        weak.append(alignment)
    if row["context"]["slot"] in {"RESERVED", "OCCUPIED"}:
        weak.append(row["context"]["slot"])
    if row["edge"] is not None and not row["edge"]["close_beyond_in_direction"]:
        weak.append("EDGE_NOT_CROSSED")
    if row["directional_body_prior_atr"] is not None and row["directional_body_prior_atr"] < rules["body_atr"]:
        weak.append("BODY_BELOW_THRESHOLD")
    if weak:
        return "OBSERVE", " + ".join(weak)
    complete = (row["policy"] is not None and state == "ENTRY CANDIDATE" and alignment == "ALIGNED"
                and row["closed_bar"] and row["confirmed_observation"] and row["context"]["slot"] == "FREE"
                and row["edge"] is not None and row["directional_body_prior_atr"] is not None
                and row["expires_at_utc"] is not None and geometry is not None
                and geometry.get("opposing_structure") is not None and geometry.get("net_structural_room_r") is not None
                and geometry["modeled_cost"] is not None and geometry["modeled_cost"].get("friction_r") is not None)
    if complete:
        return "RESEARCH PRIORITY", "ALL_FROZEN_GATES_PASS_RESEARCH_ONLY"
    return "NO DATA", "INCOMPLETE_OR_NO_VALID_CAUSAL_PLAN"


def source_rows(tape, snapshot, bar, previous, features, s5_edges):
    stamp = snapshot["known_ms"]
    interval = tape.frame * 60_000
    native = tape.shadow.get(stamp, ())
    close = bar["close"]
    for event in native:
        side, state, model = (event[key] for key in ("side", "state", "model"))
        times = [event[key] for key in ("source_ms", "level_known_ms", "watch_ms", "breakout_ms") if event[key] is not None]
        if any(value > stamp for value in times):
            raise ValueError("Future timestamp in native Shadow observation")
        key = (rc1_qa.SYMBOL, tape.frame, "DT", side, model, state, stamp, event["source_ms"],
               event["watch_ms"], event["breakout_ms"], event["origin_model"])
        uid = "|".join("-" if value is None else str(value) for value in key)
        linked = any(other["state"] == "RETEST WATCH" and other["side"] == side and other["origin_model"] == model
                     and other["breakout_ms"] == stamp and other["source_ms"] == event["source_ms"]
                     and other["level"] == event["level"] for other in native)
        bars = 0 if state in TERMINAL else 7 if linked or state in {"WATCH", "RETEST WATCH"} else 1
        geometry = None
        if state == "ENTRY CANDIDATE":
            geometry = plan((event["reference"], event["stop"], event["targets"]), "RESEARCH GEOMETRY",
                            {"mode": "PINNED MODELED COSTS", "friction_r": event["friction_r"]})
            prior_opposing = features.known_highs if side > 0 else features.known_lows
            obstacle = next((pivot for pivot in reversed(prior_opposing) if event["obstacle"] is not None
                             and isclose(pivot.price, float(event["obstacle"]), rel_tol=0, abs_tol=1e-8)), None)
            if event["obstacle"] is not None and (obstacle is None or obstacle.known_ms >= stamp):
                raise ValueError(f"Source opposing pivot unavailable before {utc(stamp)}")
            geometry.update(opposing_structure=event["obstacle"],
                            opposing_structure_known_at_utc=utc(obstacle.known_ms) if obstacle else None,
                            net_structural_room_r=event["net_room_r"], initial_all_in_risk_usd=event["risk_cash"],
                            earliest_eligible_open_utc=utc(stamp), next_open_execution_status="UNOBSERVED_AT_DECISION")
        event_policy = "P1_BREAKOUT" if model in (STRUCTURAL, CONSOLIDATION) else "P2_RETEST" if model == RETEST else None
        rejections = [other["reason"] for other in native if state == "BREAKOUT CONFIRMED"
                      and other["state"] == "REJECTED" and all(other[key] == event[key]
                      for key in ("side", "model", "source_ms", "watch_ms", "level"))]
        yield dict(event_id=uid, id_origin="DETERMINISTIC_DT_SOURCE_KEY", plan_id=event.get("plan_id"),
                   source="DIRECT TREND SHADOW", model=model, state=state, side=side, expires_ms=stamp + bars * interval,
                   confirmed_observation=state in {"BREAKOUT CONFIRMED", "ENTRY CANDIDATE"},
                   approval="RESEARCH ONLY", policy=event_policy, reason=event["reason"], plan=geometry,
                   source_gate_rejections=rejections,
                   edge=source_edge(event["level"], event["level_known_ms"], stamp, close, side, "NATIVE SHADOW"),
                   prior_atr=event["prior_atr"], origin_watch_utc=utc(event["watch_ms"]) if event["watch_ms"] else None)
    for event in tape.core.get(stamp, ()):
        side, state = event["side"], event["event"]
        edge = None
        if state == "WATCH" and len(previous) >= 20:
            level = (min(p["low"] for p in previous[-20:]) if side > 0 else max(p["high"] for p in previous[-20:]))
            if isclose(float(event["price"]), level, rel_tol=0, abs_tol=1e-8):
                s5_edges[side, stamp] = event["price"], previous[-1]["time_close"]
                edge = source_edge(*s5_edges[side, stamp], stamp, close, side, "S5 PRIOR RANGE EDGE")
        created = event["created_time"]
        if created is not None and created < stamp and (side, created) in s5_edges:
            edge = source_edge(*s5_edges[side, created], stamp, close, side, "S5 FROZEN WATCH EDGE")
        bars = 0 if state in TERMINAL else 4 if state == "WATCH" else 1
        yield dict(event_id=event["event_id"], id_origin="NATIVE", plan_id=event["plan_id"], source="S5/RC2",
                   model="S5_RECLAIM", state=state, side=side, expires_ms=stamp + bars * interval,
                   confirmed_observation=state == "CONFIRMED", approval="SOURCE OBSERVATION", policy=None,
                   reason=event["reason"], plan=None, edge=edge, prior_atr=features.prior_atr)
    for ready in tape.ready.get(stamp, ()):
        side = 1 if ready["direction"] == "BUY" else -1
        visible = snapshot["permission"] == f"{ready['direction']} READY" and snapshot["plan_visible"]
        core = next((event for event in tape.core.get(stamp, ()) if event["event_id"] == ready["source_id"]), None)
        modeled = {"mode": "SOURCE PLAN", "roundtrip_cost_price": core["roundtrip_cost_price"]} if core else None
        geometry = (plan((ready["entry"], ready["sl"], [ready[f"tp{i}"] for i in (1, 2, 3)]),
                         "S5 PLAN", modeled) if visible else None)
        if geometry is not None:
            geometry.update(initial_risk_price=side * (ready["entry"] - ready["sl"]),
                            opposing_structure=core["obstacle"] if core else None,
                            net_structural_room_r=None, earliest_eligible_open_utc=utc(stamp),
                            next_open_execution_status="UNOBSERVED_AT_DECISION")
        created = core["created_time"] if core else None
        edge = (source_edge(*s5_edges[side, created], stamp, close, side, "S5 FROZEN WATCH EDGE")
                if created is not None and created < stamp and (side, created) in s5_edges else None)
        yield dict(event_id=ready["event_id"], id_origin="NATIVE", parent_source_id=ready["source_id"],
                   source="S5/RC2", model="S5_RECLAIM", state="READY", side=side, expires_ms=stamp + interval,
                   confirmed_observation=True, approval="S5 APPROVED" if visible else "SOURCE OBSERVATION",
                   policy=None, reason=ready["reason"],
                   plan=geometry, edge=edge, prior_atr=features.prior_atr)
    if stamp in tape.early:
        direction = tape.early[stamp]
        side = 1 if direction == "BUY" else -1
        edge = None
        if len(previous) >= 12:
            level = max(p["high"] for p in previous[-12:]) if side > 0 else min(p["low"] for p in previous[-12:])
            edge = source_edge(level, previous[-1]["time_close"], stamp, close, side, "RC2 PRIOR 12 CLOSED BARS")
        yield dict(event_id=f"{rc1_qa.SYMBOL}|{tape.frame}|RC2_EARLY|{direction}|{stamp}|WATCH",
                   id_origin="DERIVED_FROM_PINNED_RC2_ALERT_KEY", source="RC2 EARLY WATCH", model="RC2_EARLY_WATCH",
                   state="WATCH", side=side, expires_ms=stamp + 2 * interval, confirmed_observation=False,
                   approval="RESEARCH ONLY", policy=None, reason="CLOSED BAR EARLY WATCH", plan=None, edge=edge,
                   prior_atr=features.prior_atr)


def normalize(tape, htf, rules):
    observations, ids = [], set()
    s5_edges = {}
    snapshots = {row["known_ms"]: row for row in tape.snapshots}
    for index, (bar, features) in enumerate(zip(tape.market, closed_context(tape.market))):
        stamp = bar["time_close"]
        snapshot = snapshots.get(stamp)
        if snapshot is None:
            continue
        context = snapshot_context(snapshot)
        active_confirmed = {signal["side"] for signal in snapshot["signals"] if signal["stage"] == "BREAKOUT CONFIRMED"}
        rows = list(source_rows(tape, snapshot, bar, tape.market[max(0, index - 20):index], features, s5_edges))
        active_confirmed.update(row["side"] for row in rows if row["confirmed_observation"])
        for row in rows:
            uid, side, expiry = (row.pop(key) for key in ("event_id", "side", "expires_ms"))
            if uid in ids:
                raise ValueError(f"Duplicate source event key: {uid}")
            ids.add(uid)
            if row["source"] == "DIRECT TREND SHADOW" and row["prior_atr"] is not None and not isclose(
                    float(row["prior_atr"]), features.prior_atr, rel_tol=0, abs_tol=2e-8):
                raise ValueError(f"Different local prior ATR at {utc(stamp)}")
            atr = float(row["prior_atr"]) if row["prior_atr"] is not None else None
            row.update(event_id=uid, symbol=rc1_qa.SYMBOL, timeframe_minutes=tape.frame, direction=SIDES[side],
                       known_at_ms=stamp, known_at_utc=utc(stamp), expires_at_utc=utc(expiry),
                       closed_bar=True, provisional_observation=row["state"] in {"WATCH", "RETEST WATCH"},
                       htf=htf_alignment(tape.frame, stamp, bar, side, htf), context=context,
                       directional_body_prior_atr=(bar["close"] - bar["open"]) * side / atr if atr else None,
                       range_prior_atr=(bar["high"] - bar["low"]) / atr if atr else None,
                       prior_ema20=features.prior_ema, prior_ema20_slope3=features.prior_ema_slope,
                       executable_plan_status=("S5 PLAN" if row["approval"] == "S5 APPROVED" and row["plan"] is not None
                                               else "NO EXECUTABLE PLAN"),
                       broker_cost_status="BROKER COSTS UNKNOWN")
            row["triage"], row["triage_reason"] = triage(row, active_confirmed, rules)
            observations.append(row)
    return observations


def evidence(tape, rows, htf, rules):
    observed = {tuple(signal[field] for field in ("source", "model", "side", "stage", "known_ms"))
                for snapshot in tape.snapshots for signal in snapshot["signals"]}
    for snapshot in tape.snapshots:
        stamp = snapshot["known_ms"]
        if normalize(tape.prefix(stamp), {time: value for time, value in htf.items() if time <= stamp}, rules) != [
            row for row in rows if row["known_at_ms"] <= stamp
        ]:
            raise ValueError(f"Observation prefix changed at {utc(stamp)}")
    observed_count = sum(len(snapshot["signals"]) for snapshot in tape.snapshots)
    return {
        "known_window_utc": [utc(tape.snapshots[0]["known_ms"]), utc(tape.snapshots[-1]["known_ms"])],
        "first_loaded_open_utc": utc(tape.market[0]["time"]),
        "confirmed_snapshot_bars": len(tape.snapshots),
        "prefix_invariance": {"status": "MATCH", "tested_prefixes": len(tape.snapshots)},
        "events": len(rows), "event_stream_sha256": digest(encode(rows)),
        "sources": Counter(row["source"] for row in rows), "states": Counter(row["state"] for row in rows),
        "triage": Counter(row["triage"] for row in rows), "event_time_htf": Counter(row["htf"]["status"] for row in rows),
        "archived_htf_context": Counter(row["htf"]["archived_status"] for row in rows if "archived_status" in row["htf"]),
        "approved_s5_events": sum(row["approval"] == "S5 APPROVED" for row in rows),
        "snapshot_deduplication": {"display_rows": observed_count, "unique_observations": len(observed),
                                   "repeat_displays": observed_count - len(observed)},
        "terminal_events": sum(row["state"] in TERMINAL for row in rows),
        "examples": [row for row in rows if row["known_at_utc"][11:16] in {"02:00", "02:15", "09:10", "11:45", "12:00", "12:15", "12:30", "13:30", "14:45"}],
    }


def paired_evidence(baseline, candidate, frame):
    pair = native_qa.verify_pair(baseline, candidate, frame, "indicator")
    shadow = native_qa.verify_shadow(baseline, candidate, frame)
    if pair["status"] != "MATCH" or shadow["status"] != "MATCH" or (
            native_qa.journal_rows(baseline) != native_qa.journal_rows(candidate)):
        raise ValueError(f"Source pair does not match on M{frame}")
    return {"status": "MATCH", "baseline_csv": baseline.name, "baseline_sha256": pair["baseline"]["sha256"],
            "raw_csv_identical": pair["baseline"]["sha256"] == pair["visibility"]["sha256"],
            "raw_paper": pair["s5_raw_paper"], "shadow": shadow,
            "core_ready_and_compatibility": pair["core_ready_and_compatibility"],
            "rc2_streams": pair["rc2_native_streams"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--events", type=Path, help="Optional complete normalized event stream")
    parser.add_argument("--previous-signal-quality", action="store_true", help="Use pinned older Signal Quality exports")
    args = parser.parse_args()
    protocol = Path(__file__).with_name("PROTOCOL.json").read_bytes()
    if digest(protocol) != PROTOCOL_SHA:
        raise ValueError("Changed preregistered protocol")
    specification = json.loads(protocol)
    rules = specification["policies"]["shared"]
    window = specification["data"]["research_windows_utc"]["native_qa"]
    source_manifest = json.loads(BASELINE_QA.read_bytes())
    htf = archived_htf()
    frames, events = {}, {}
    for frame in window["frames_minutes"]:
        label = f"M{frame}"
        filename = PRIOR_FILES[frame] if args.previous_signal_quality else f"candidate-{label}.csv"
        path = args.native_dir / filename
        actual = digest(path.read_bytes())
        prior = source_manifest["matched_pairs"][label]
        if args.previous_signal_quality and actual not in prior["sha256"]:
            raise ValueError(f"Wrong pinned source journal: {filename}")
        tape = source_tape(path, frame)
        if (tape.snapshots[0]["known_ms"], tape.snapshots[-1]["known_ms"]) != (
                epoch(window["close_first"]), epoch(window["close_last"])):
            raise ValueError(f"Wrong registered observation window: {filename}")
        rows = normalize(tape, htf, rules)
        events[label] = rows
        pairing = {} if args.previous_signal_quality else paired_evidence(args.native_dir / f"baseline-{label}.csv", path, frame)
        frames[label] = {"native_csv": filename, "sha256": actual, "same_window_source_pair": pairing,
                         "previous_source_first_loaded_open_utc": utc(prior["shadow"]["first_native_open"][1]),
                         **evidence(tape, rows, htf, rules)}
    result = {
        "scope": ("Prior pinned Signal Quality closed-bar observations" if args.previous_signal_quality else
                  "Native Professional Selector closed-bar journals paired with unchanged Signal Quality"),
        "protocol_sha256": PROTOCOL_SHA,
        "native_h1_h4": {"partial_M15_source_sha256": HTF_SHA, "same_warmup_equivalence": "NOT VERIFIED"},
        "availability": {"null": "UNKNOWN or inapplicable without a source value",
                         "archived_htf": "Context only. Different-run HTF warmup is not established.",
                         "research_geometry": "A native diagnostic plan only. No executable permission."},
        "missing_sources": {"wick": "No same-tape native Wick event rows. Separate Wick tape not merged.",
                            "pressure": "Context-only OHLC proxy, no directional event or order-flow evidence."},
        "model_aliases": {"PULLBACK_RETEST": RETEST},
        "frames": frames,
    }
    args.output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False, default=str, indent=2)
                           + "\n", encoding="utf-8", newline="\n")
    if args.events:
        args.events.write_bytes(encode(events))


if __name__ == "__main__":
    main()
