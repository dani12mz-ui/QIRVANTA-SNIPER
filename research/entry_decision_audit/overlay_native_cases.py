"""Join parity-checked protected Pine events to the fresh EDA branch diagnostics."""

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import hashlib
import io
import json
from pathlib import Path
from zipfile import ZipFile


BASE = "37b903f46a2d878d9d0757da134620c22f2d067d"
FRAMES = {"M5": 5, "M15": 15, "M30": 30}
TAGS = ("DTV_STATE", "DTV_PAPER", "RC1_CORE", "RC1_READY", "DTV_LEDGER", "DT_SHADOW")
CASE_SELECTION = {
    "NOV11_BUY_03": ("2026-11-11", 1, ("02:45", "03:00", "03:05", "03:15", "03:30", "03:35", "03:40", "04:00")),
    "NOV11_SELL_DAY": (
        "2026-11-11", -1, ("11:30", "11:45", "12:00", "12:15", "12:30", "12:50", "12:55",
                          "13:00", "13:30", "13:45", "14:00", "14:05", "14:35", "15:25", "15:30"),
    ),
    "NOV11_FIXED_SELL_14": ("2026-11-11", -1, ("13:45", "14:00", "14:05", "14:15", "14:30", "14:35", "14:45")),
    "NOV13_M5_SELL_EXECUTION_WITNESS": ("2026-11-13", -1, ("17:50", "18:00", "18:05", "18:40", "18:45", "19:00")),
}
FULL_WINDOWS = {
    "NOV11_BUY_03": ("02:45", "04:15"),
    "NOV11_SELL_DAY": ("11:30", "16:30"),
    "NOV11_FIXED_SELL_14": ("14:00", "15:00"),
}
WARMUP_REASON = "Недостатъчни данни или неподдържана рамка"
NO_TRIGGER = "Няма суров trigger"


def utc(clock):
    if clock is None:
        return None
    return datetime.fromtimestamp(clock / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ms(day, time):
    return int(datetime.fromisoformat(f"{day}T{time}:00+00:00").timestamp()) * 1000


def json_file(path):
    source = path.read_bytes()
    return json.loads(gzip.decompress(source) if source.startswith(b"\x1f\x8b") else source)


def get_raw(root, archive, batch, frame):
    name = f"{batch}/audit-on-{frame}.csv"
    return archive.read(name) if archive else (root / name).read_bytes()


def check(condition, message):
    if not condition:
        raise ValueError(message)


def protected_messages(raw):
    result = {tag: [] for tag in TAGS}
    for row in csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))):
        tag, _, payload = row["Message"].partition("|")
        if tag in result:
            result[tag].append(json.loads(payload))
    return result


def effective_in(event, clocks):
    return event["known_time"] in clocks or event["effective_time"] in clocks


def direction_label(direction):
    return {None: "UNKNOWN_MISSING", 0: "MIXED", -1: "SELL", 1: "BUY"}[direction]


def source_watch_gate(bar, side):
    if not bar["warmup"] or side["before_phase"] != 0:
        return None
    if side["range20_edge"] is None or bar["previous_close"] is None:
        return None
    edge = Decimal(side["range20_edge"])
    close_offset = side["side"] * (Decimal(bar["close"]) - edge)
    previous_offset = side["side"] * (Decimal(bar["previous_close"]) - edge)
    return {
        "source": "Visual V2 f_tvCandidate idle opposite-break branch",
        "previous_close": bar["previous_close"], "close": bar["close"],
        "preceding_20_bar_opposite_edge": side["range20_edge"],
        "edge_kind": "LOW" if side["side"] == 1 else "HIGH",
        "gap": bar["gap"], "previous_signed_offset": str(previous_offset),
        "close_signed_offset": str(close_offset),
        "passes": not bar["gap"] and close_offset < 0 <= previous_offset,
    }


def focus_class(bar, side, protected):
    if any(row["side"] == side["side"] and row["event"] == "SIM_ENTRY" for row in protected["paper"]):
        return "EXECUTED_NATIVE_RAW_PAPER"
    if any(row["side"] == side["side"] and row["event"] == "CANCELLED" for row in protected["paper"]):
        return "QUEUED_THEN_CANCELLED"
    if bar["permission"] == ("BUY READY" if side["side"] == 1 else "SELL READY"):
        return "READY_AT_CONFIRMED_CLOSE"
    if not bar["warmup"] and not bar["readyHTF"] and side["reason"] == WARMUP_REASON:
        return "NO_CANDIDATE_SELECTED_CORE_HTF_NOT_READY"
    if side["raw"]:
        return "S5_TRIGGER_WITHOUT_READY_AT_THIS_CLOSE"
    if side["before_phase"] == 0 == side["after_phase"] and side["reason"] == NO_TRIGGER:
        return "NO_IDLE_S5_WATCH_TRIGGER"
    if side["after_phase"] != 0:
        return "S5_WATCH_PENDING"
    return "NO_NEW_S5_CANDIDATE_AT_THIS_CLOSE"


def case_rows(label, frame, normalized, protected):
    day, focus, times = CASE_SELECTION[label]
    wanted = {ms(day, value) for value in times}
    sides = {(row["close_ms"], row["side"]): row for row in normalized["sides"]}
    states = {row["known_ms"]: row for row in protected["DTV_STATE"]}
    ledgers = {row["known_ms"]: row for row in protected["DTV_LEDGER"]}
    output = []
    for bar in normalized["bars"]:
        clock = bar["close_ms"]
        if clock not in wanted:
            continue
        s5 = {dir_: sides[(clock, dir_)] for dir_ in (1, -1)}
        native = {
            "display": states[clock], "raw_ledger": ledgers[clock],
            "core": [row for row in protected["RC1_CORE"] if row["known_time"] == clock],
            "ready": [row for row in protected["RC1_READY"] if row["confirmed_ms"] == clock],
            "paper": [row for row in protected["DTV_PAPER"] if row["known_time"] == clock],
            "shadow": [row for row in protected["DT_SHADOW"] if row["time_ms"] == clock],
        }
        check(native["display"]["permission"] == bar["permission"], f"{label}/{frame}/{clock} protected decision mismatch")
        checks = {dir_: source_watch_gate(bar, values) for dir_, values in s5.items()}
        for dir_, gate in checks.items():
            if gate is not None:
                check(gate["passes"] == (s5[dir_]["after_phase"] == 1), f"{label}/{frame}/{clock} source watch mismatch")
        output.append({
            "case_id": label, "frame": frame, "focus_side": focus, "confirmed_close_utc": utc(clock),
            "known_at_evidence_class": focus_class(bar, s5[focus], native),
            "fresh_eda_bar": bar, "fresh_eda_sides": s5,
            "fresh_pinned_s5_watch_start_derived_from_previous_and_current_closes": checks,
            "fresh_protected_at_close": native,
        })
    return output


def full_summary(normalized, protected):
    bars = normalized["bars"]
    sides = normalized["sides"]
    by_close = {row["close_ms"]: row for row in bars}
    check(len(by_close) == len(bars) and len(sides) == len(bars) * 2, "Incomplete normalized EDA")
    for row in protected["DTV_STATE"]:
        check(by_close[row["known_ms"]]["permission"] == row["permission"], "Normalized/RAW permission mismatch")
    entries = [row for row in protected["DTV_PAPER"] if row["event"] == "SIM_ENTRY"]
    first_ready = next((row["close_ms"] for row in bars if row["readyHTF"]), None)
    ready_counts = Counter(str(row["readyHTF"]).lower() for row in bars)
    warmup_counts = Counter(str(row["warmup"]).lower() for row in bars)
    return {
        "bar_count": len(bars), "side_count": len(sides),
        "first_audited_close_utc": utc(bars[0]["close_ms"]), "last_audited_close_utc": utc(bars[-1]["close_ms"]),
        "loaded_history_first_open_utc": utc(bars[0]["first_open_ms"]),
        "selected_core_htf": sorted({row["core_htf"] for row in bars}),
        "selected_core_htf_ready": ready_counts, "full_warmup_ready": warmup_counts,
        "first_ready_core_htf_close_utc": utc(first_ready),
        "h1_direction_counts": Counter(direction_label(row["h1_direction"]) for row in bars),
        "h4_direction_counts": Counter(direction_label(row["h4_direction"]) for row in bars),
        "dashboard_permission_counts": Counter(row["permission"] for row in bars),
        "both_side_reason_counts": Counter(row["reason"] for row in sides),
        "raw_paper_event_counts": Counter(row["event"] for row in protected["DTV_PAPER"]),
        "raw_paper_entries": entries,
        "final_raw_ledger_from_protected_stream": protected["DTV_LEDGER"][-1],
    }


def focused_window_summary(label, normalized, protected):
    day, focus, _ = CASE_SELECTION[label]
    first, last = (ms(day, time) for time in FULL_WINDOWS[label])
    bars = [row for row in normalized["bars"] if first <= row["close_ms"] < last]
    sides = [row for row in normalized["sides"] if first <= row["close_ms"] < last and row["side"] == focus]
    check(len(bars) == len(sides), f"{label} missing focus side")
    paper = [row for row in protected["DTV_PAPER"] if first <= row["known_time"] < last]
    return {
        "focus_side": focus, "first_utc_inclusive": utc(first), "last_utc_exclusive": utc(last),
        "assessed_closes": len(sides), "warmup_ready_close_count": sum(row["warmup"] for row in bars),
        "visible_permission_counts": Counter(row["permission"] for row in bars),
        "focus_side_original_reasons": Counter(row["reason"] for row in sides),
        "focus_side_before_after_phases": Counter(f'{row["before_phase"]}->{row["after_phase"]}' for row in sides),
        "focus_side_raw_trigger_count": sum(row["raw"] for row in sides),
        "focus_side_raw_sim_entries_known_in_window": sum(row["side"] == focus and row["event"] == "SIM_ENTRY" for row in paper),
        "opposing_paper_events": [row for row in paper if row["side"] != focus and row["event"] in ("QUEUED", "CANCELLED")],
    }


def compare_origins(archive_rows, current, main_sets):
    summary = {}
    reps = {}
    for row in archive_rows:
        if row["id"] in ("NOV11_BUY_03", "NOV11_SELL_DAY"):
            reps[(row["id"], row["frame"])] = row
    for frame, norm in current.items():
        summary[frame] = {}
        for case in ("NOV11_BUY_03", "NOV11_SELL_DAY"):
            old = reps[(case, frame)]["current_visual_v2_native_s5"]
            by_close = {row["close_ms"]: row for row in norm["bars"]}
            old_contexts = [row["context"] for row in old["state_by_close"]]
            check(bool(old_contexts), f"{case}/{frame} missing archived native context")
            for archived in old_contexts:
                fresh = by_close[archived["known_ms"]]
                check(archived["open_ms"] == fresh["open_ms"]
                      and archived["ohlc"] == [float(fresh[key]) for key in ("open", "high", "low", "close")]
                      and archived["h1_closed_ms"] == fresh["h1_closed_ms"]
                      and archived["h4_closed_ms"] == fresh["h4_closed_ms"],
                      f"{case}/{frame} historical and fresh chart inputs diverge")
            sample = old_contexts[0]
            current_first = by_close[sample["known_ms"]]["first_open_ms"]
            check(sample["first_chart_open_ms"] != current_first, "Old and fresh history origins unexpectedly identical")
            clocks = set(row["known_ms"] for row in old_contexts)
            current_paper = [event for event in main_sets[frame]["DTV_PAPER"] if effective_in(event, clocks)]
            summary[frame][case] = {
                "matched_close_open_ohlc_and_sampled_htf_close_timestamps": len(old_contexts),
                "old_native_first_loaded_open_utc": utc(sample["first_chart_open_ms"]),
                "fresh_native_first_loaded_open_utc": utc(current_first),
                "old_native_paper_event_counts": Counter(row["event"] for row in old["paper_events"]),
                "fresh_native_paper_event_counts": Counter(row["event"] for row in current_paper),
                "strict_cross_run_input_parity": False,
            }
    return summary


def csv_row(row):
    bar = row["fresh_eda_bar"]
    sides = row["fresh_eda_sides"]
    protected = row["fresh_protected_at_close"]
    checks = row["fresh_pinned_s5_watch_start_derived_from_previous_and_current_closes"]
    return {
        **{key: row[key] for key in ("case_id", "confirmed_close_utc", "frame", "focus_side", "known_at_evidence_class")},
        "symbol": bar["symbol"], "s5_permission": bar["permission"],
        "s5_permission_reason": bar["permission_reason"], "core_htf": bar["core_htf"],
        "core_htf_ready": bar["readyHTF"], "warmup": bar["warmup"],
        "h1_direction": direction_label(bar["h1_direction"]), "h4_direction": direction_label(bar["h4_direction"]),
        "h1_closed_utc": utc(bar["h1_closed_ms"]), "h4_closed_utc": utc(bar["h4_closed_ms"]),
        "previous_close": bar["previous_close"], "close": bar["close"],
        "early_observation_side": bar["early_side"], "six_bar_structure_observation_side": bar["six_bar_side"],
        "buy_s5_phases": f'{sides[1]["before_phase"]}->{sides[1]["after_phase"]}',
        "buy_s5_raw": sides[1]["raw"], "buy_s5_reason": sides[1]["reason"],
        "buy_range20_low": sides[1]["range20_edge"],
        "buy_watch_cross_pass": checks[1]["passes"] if checks[1] else "",
        "sell_s5_phases": f'{sides[-1]["before_phase"]}->{sides[-1]["after_phase"]}',
        "sell_s5_raw": sides[-1]["raw"], "sell_s5_reason": sides[-1]["reason"],
        "sell_range20_high": sides[-1]["range20_edge"],
        "sell_watch_cross_pass": checks[-1]["passes"] if checks[-1] else "",
        "paper_phases": f'{bar["before_paper_phase"]}->{bar["after_paper_phase"]}',
        "fresh_paper_events": " | ".join(f'{event["event"]} {event["side"]}' for event in protected["paper"]),
        "fresh_shadow_events": " | ".join(
            f'{event["model"]} {event["side"]} {event["state"]} {event["reason"]}' for event in protected["shadow"]),
    }


def run(args):
    main = json_file(args.main_journal)
    recent = json_file(args.recent_journal)
    old = json_file(args.older_movement_journal)
    check(set(main) == set(FRAMES) and set(recent) == {"M5"}, "Unexpected diagnostic windows")
    archive = ZipFile(args.native_input) if args.native_input.is_file() else None
    fresh = {}
    recent_native = {}
    raw_hashes = {}
    try:
        for batch, frames, target in (("native", FRAMES, fresh), ("native-recent", ["M5"], recent_native)):
            for frame in frames:
                raw = get_raw(args.native_input, archive, batch, frame)
                target[frame] = protected_messages(raw)
                raw_hashes[f"{batch}/audit-on-{frame}.csv"] = hashlib.sha256(raw).hexdigest()
    finally:
        if archive:
            archive.close()

    rows = []
    for case in CASE_SELECTION:
        for frame, normalized in (recent if case.startswith("NOV13") else main).items():
            protected = recent_native[frame] if case.startswith("NOV13") else fresh[frame]
            rows.extend(case_rows(case, frame, normalized, protected))
    check(len({(row["case_id"], row["frame"], row["confirmed_close_utc"]) for row in rows}) == len(rows),
          "Duplicate selected close")
    data = {
        "schema": "qirvanta.entry_decision_native_case_overlay.v1", "baseline_commit": BASE,
        "native_on_export_sha256": raw_hashes,
        "fresh_nov11": {frame: full_summary(main[frame], protected) for frame, protected in fresh.items()},
        "fresh_nov13_m5_execution_witness": full_summary(recent["M5"], recent_native["M5"]),
        "cross_run_archive_comparison_screen_fixtures_only": compare_origins(old, main, fresh),
        "nov11_full_case_windows": {case: {frame: focused_window_summary(case, normalized, fresh[frame])
                                            for frame, normalized in main.items()}
                                    for case in FULL_WINDOWS},
        "selected_close_rows": rows,
        "notes": {
            "h1_h4": "null means UNKNOWN/MISSING for the sampled display context; 0 is MIXED after known data. EDA readyHTF reports the independently selected original core resolution H1/H4/D, rather than guaranteeing both advisory samples are ready.",
            "no_instrumentation_effect": "Input main/recent EDA journals are parsed from test copies whose protected streams were independently compared with baseline and audit-off at identical same-run origin and settings.",
            "m5_old_vs_fresh": "Pinned archived tape has six SIM_ENTRY events in the full Nov 11 audit, including opposing BUY fills at 12:50/14:00. Today's trace has zero, selected H1 is unready through 17:00, and calculation origins differ. The cause of the change in provided history is outside these logs.",
            "m30_core": "Current selected core resolution D is not ready in the main M30 audit while H1 and H4 sampled directions are available. The original aggregate readyHTF flag cannot identify the missing individual Daily tuple field or platform cause.",
            "observation": "Observation directions, Shadow geometry and Luminous events remain separate from S5; no trade-price inference from them.",
            "edge_rule": "Source watch crossing is derived at a known close only when original warmup passed and the prior per-side S5 phase was idle. An unevaluated branch remains null.",
            "audit_window": "The Nov11 results are captured anew on the same dates as the older tape, with a different calculation origin between those two runs. This overlay supplements and does not replace the archived movement classifications.",
            "recent": "Nov13 M5 is a separate in-indicator simulation witness with TP1/stop adjustment and an open remainder at 19:00. No broker execution or server alert transport is inferred.",
        },
    }
    flat = [csv_row(row) for row in rows]
    args.output.mkdir(parents=True, exist_ok=True)
    payload = (json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    (args.output / "native-movement-cases.json.gz").write_bytes(gzip.compress(payload, mtime=0))
    with (args.output / "native-movement-cases.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=tuple(flat[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(flat)
    print(f"{len(rows)} selected confirmed closes written")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--main-journal", type=Path, required=True, help="Parity-validated Nov11 normalized JSON or JSON.GZ")
    cli.add_argument("--recent-journal", type=Path, required=True, help="Parity-validated Nov13 M5 JSON or JSON.GZ")
    cli.add_argument("--older-movement-journal", type=Path, required=True)
    cli.add_argument("--native-input", type=Path, required=True, help="Evidence directory with native/ and native-recent/ or equivalent ZIP")
    cli.add_argument("--output", type=Path, required=True)
    run(cli.parse_args())

