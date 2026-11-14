"""Reconcile old Visual V2 and fresh EDA native archives without rerunning their market logic."""

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
import subprocess
import zipfile


VISUAL = "37b903f46a2d878d9d0757da134620c22f2d067d"
AUDIT = "4875918c17db967b706364c35c7cd074c5fbc14d"
OLD_PREFIX = "research/luminous_visual_v2/"
NEW_PREFIX = "research/entry_decision_audit/evidence/"
SOURCES = {
    "visual_v2_pine": (VISUAL, OLD_PREFIX + "QIRVANTA_GOLD_PRO_VISUAL_V2.pine",
                      "77feedcbef84e77b9270ec43840be5b823d838300d0a09d5ddb4fec7acc089f4"),
    "pinned_native": (VISUAL, OLD_PREFIX + "evidence/native-pairs.zip",
                      "fec83cd615923b210b6815852a69b74ed457f791ac41b3913a7c189f7d58f952"),
    "fresh_native": (AUDIT, NEW_PREFIX + "native-exports.zip",
                     "2b64267325b52bc33dd5ee5e427bf4e833bde11d1a035e8fd7cf4e856a5b550b"),
    "fresh_nov11": (AUDIT, NEW_PREFIX + "native-journal-nov11.json.gz",
                    "90fe32eab40d2f88ec7113a065194e8de378ba12af470d235f34d8d5353c003b"),
    "fresh_nov13_m5": (AUDIT, NEW_PREFIX + "native-journal-nov13-m5.json.gz",
                       "64f114f29b6e56b1dfba4b4b9b54af8c82a6bcc02bb5270dcf616cc1d275ebf1"),
    "fresh_nov11_parity": (AUDIT, NEW_PREFIX + "native-parity-nov11.json",
                           "208e5edd40ea3ad7822ae6bfd4fcf81a5ec6bb6c93328e16e4f3d1b67fa15877"),
    "fresh_nov13_m5_parity": (AUDIT, NEW_PREFIX + "native-parity-nov13-m5.json",
                              "525e68a00d07dc55b6931e6e3596cb3e52fb80bac4f95ac57b27068a511b38ed"),
    "fresh_settings": (AUDIT, NEW_PREFIX + "native-settings.json",
                       "19eba88a9489799ca423b559f63a5e16b5e74d8fd825435f3c6713726b112587"),
}
FRAMES = {"M5": "60", "M15": "240", "M30": "D"}
FIELDS = ("time_close", "open", "high", "low", "close")
SIDES = {1: "BUY", -1: "SELL"}
TAGS = ("RC1_CTX", "RC1_CORE", "RC1_READY", "RC2_EARLY", "DTV_STATE", "DTV_PAPER",
        "DTV_LEDGER", "DT_MARKET", "DT_END", "DT_SHADOW", "LZ_EVENT", "EDA_BAR", "EDA_SIDE")
WARMUP = "Недостатъчни данни или неподдържана рамка"
NO_TRIGGER = "Няма суров trigger"
WINDOWS = {
    "nov11_buy": ("2026-11-11", "02:45", "04:15", 1),
    "nov11_sell": ("2026-11-11", "11:30", "16:30", -1),
    "nov11_fixed_sell_hour": ("2026-11-11", "14:00", "15:00", -1),
    "nov13_m5_witness": ("2026-11-13", "17:50", "19:05", -1),
}


def utc(value):
    return datetime.fromtimestamp(value / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if value is not None else None


def stamp(day, time):
    return int(datetime.fromisoformat(f"{day}T{time}:00+00:00").timestamp()) * 1000


def require(condition, context):
    if not condition:
        raise ValueError(context)


def pinned(repo, specification):
    commit, path, expected = specification
    data = subprocess.check_output(["git", "show", f"{commit}:{path}"], cwd=repo)
    require(hashlib.sha256(data).hexdigest() == expected, f"Hash mismatch {commit}:{path}")
    return data


def tape(archive, path):
    out = {tag: [] for tag in TAGS}
    raw = archive.read(path)
    for row in csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))):
        tag, _, payload = row["Message"].partition("|")
        if tag in out:
            out[tag].append(payload)
    return out, hashlib.sha256(raw).hexdigest()


def objects(t, tag):
    return [json.loads(row, parse_float=Decimal) for row in t[tag]]


def parse_history(t):
    sentinel, = objects(t, "DT_END")
    full = objects(t, "DT_MARKET")
    valid = [r for r in full if r["time_close"] <= sentinel["time_close"]]
    require([r["index"] for r in valid] == list(range(sentinel["index"] + 1)), "DT_MARKET verified prefix is not contiguous by index")
    require(valid[-1]["time_close"] == sentinel["time_close"], "DT_MARKET does not reach DT_END")
    require(all(row["symbol"] == "OANDA:XAUUSD" for row in valid), "Wrong provider")
    by_open = {row["time"]: row for row in valid}
    require(len(by_open) == len(valid), "Duplicate chart open")
    result = {
        "bars_through_dt_end": len(valid), "first_loaded_open_utc": utc(valid[0]["time"]),
        "first_loaded_close_utc": utc(valid[0]["time_close"]), "last_loaded_open_utc": utc(valid[-1]["time"]),
        "last_loaded_close_utc": utc(sentinel["time_close"]),
        "rows_after_dt_end_excluded": len(full) - len(valid),
        "gaps_between_logged_bars": sum(a["time_close"] != b["time"] for a, b in zip(valid, valid[1:])),
    }
    return by_open, result


def context(t):
    rows = {}
    for p in t["RC1_CTX"]:
        ver, symbol, frame, origin, index, op, cl, o, h, l, c, h1, h4 = p.split("|")
        require(ver == "1" and symbol == "OANDA:XAUUSD", "Unexpected RC1 context source")
        key = int(cl)
        require(key not in rows, "Duplicate RC1_CTX history")
        rows[key] = {
            "origin": int(origin), "index": int(index), "frame": frame, "open_ms": int(op),
            "OHLC": [Decimal(x) for x in (o, h, l, c)], "h1_closed_ms": int(h1) if h1.isdigit() else None,
            "h4_closed_ms": int(h4) if h4.isdigit() else None,
        }
    return rows


def compare_ctx(old, new):
    require(tuple(old) == tuple(new), "Different protected close timestamps")
    fields = ("frame", "open_ms", "OHLC", "h1_closed_ms", "h4_closed_ms")
    different = [utc(key) for key in old if any(old[key][x] != new[key][x] for x in fields)]
    return {
        "protected_closes": len(old), "utc_first": utc(min(old)), "utc_last": utc(max(old)),
        "mismatched_close_utc": different,
        "old_first_bar_index_at_audit_start": old[min(old)]["index"],
        "fresh_first_bar_index_at_audit_start": new[min(new)]["index"],
        "exact_scored_window_clock_ohlc_and_advisory_source_timestamps": not different,
    }


def stream_comparison(old, new):
    result = {}
    for tag in ("RC1_CORE", "RC1_READY", "RC2_EARLY", "DTV_STATE", "DTV_PAPER", "DTV_LEDGER", "LZ_EVENT", "DT_SHADOW"):
        result[tag] = {"old_count": len(old[tag]), "fresh_count": len(new[tag]), "payload_bytes_equal": old[tag] == new[tag]}
    return result


def supplied_natives(t):
    return {
        "state": {row["known_ms"]: row for row in objects(t, "DTV_STATE")},
        "ready": {row["confirmed_ms"]: row for row in objects(t, "RC1_READY")},
        "paper": objects(t, "DTV_PAPER"),
        "core": objects(t, "RC1_CORE"),
        "ledger": objects(t, "DTV_LEDGER"),
        "shadow": objects(t, "DT_SHADOW"),
    }


def condense_paper(paper):
    keys = ("event", "plan_id", "side", "price", "reason", "reference", "initial_sl", "tp1",
            "tp2", "tp3", "gross_r", "cost_r", "net_r", "atr_at_signal")
    return {**{key: paper[key] for key in keys}, "known_utc": utc(paper["known_time"]),
            "effective_utc": utc(paper["effective_time"])}


def direction_label(value):
    return {None: "UNKNOWN_MISSING", 0: "MIXED", 1: "BUY", -1: "SELL"}[value]


def verify_fresh_eda(t, norm, expected_frame):
    ctx = context(t)
    bars = {row["close_ms"]: row for row in norm["bars"]}
    require(tuple(bars) == tuple(ctx), "Normalized bars do not match RC1 clocks")
    raw_bars = {int(row.split("|")[3]): row.split("|") for row in t["EDA_BAR"]}
    raw_sides = {(int(row.split("|")[3]), int(row.split("|")[4])): row.split("|") for row in t["EDA_SIDE"]}
    require(len(raw_bars) == len(t["EDA_BAR"]) == len(bars), "Unexpected raw EDA_BAR tape count")
    require(len(raw_sides) == len(t["EDA_SIDE"]) == len(bars) * 2, "Unexpected raw EDA_SIDE tape count")
    for key, row in bars.items():
        c = ctx[key]
        raw = raw_bars[key]
        require(row["frame"] == expected_frame[1:] and row["core_htf"] == FRAMES[expected_frame],
                "Unexpected selected core timeframe")
        require((row["index"], row["first_open_ms"], row["open_ms"]) == (c["index"], c["origin"], c["open_ms"]) and
                [Decimal(row[k]) for k in ("open", "high", "low", "close")] == c["OHLC"] and
                (row["h1_closed_ms"], row["h4_closed_ms"]) == (c["h1_closed_ms"], c["h4_closed_ms"]),
                "EDA and RC1 source differ")
        require((row["warmup"], row["readyHTF"], row["core_htf"]) == (raw[10] == "true", raw[11] == "true", raw[12]),
                "Normalized HTF ready state differs from raw")
    for row in norm["sides"]:
        raw = raw_sides[(row["close_ms"], row["side"])]
        require((row["before_phase"], row["after_phase"], row["raw"], row["reason"]) ==
                (int(raw[5]), int(raw[11]), raw[16] == "true", raw[17]),
                "Normalized S5 branch differs from raw")
    return bars


def phases(norm):
    return {(row["close_ms"], row["side"]): row for row in norm["sides"]}


def support_summary(bars, status):
    first = next((row["close_ms"] for row in bars.values() if row["readyHTF"]), None)
    return {
        "core_resolution": next(iter(bars.values()))["core_htf"],
        "core_htf_ready_count": sum(row["readyHTF"] for row in bars.values()),
        "warmup_ready_count": sum(row["warmup"] for row in bars.values()),
        "total_closes": len(bars), "first_ready_core_htf_close_utc": utc(first),
        "display_h1_direction": Counter(direction_label(row["h1_direction"]) for row in bars.values()),
        "display_h4_direction": Counter(direction_label(row["h4_direction"]) for row in bars.values()),
        "both_side_source_reasons": Counter(row["reason"] for row in status.values()),
    }


def decision(bar, side_row, native, at_close):
    side = side_row["side"]
    paper_events = {r["event"] for r in at_close if r["side"] == side}
    ready = native["ready"].get(bar["close_ms"])
    if "SIM_ENTRY" in paper_events:
        return "EXECUTED_IN_RAW_PAPER"
    if "CANCELLED" in paper_events:
        return "READY_PLAN_CANCELLED_AT_MODELED_OPEN"
    if "CAPACITY" in paper_events:
        return "READY_PLAN_BLOCKED_BY_LINKED_CAPACITY"
    if "QUEUED" in paper_events or (ready and ready["direction"] == SIDES[side]):
        return "READY_OR_QUEUED_NO_FILL_AT_THIS_CLOSE"
    if not bar["readyHTF"] and not bar["warmup"] and side_row["reason"] == WARMUP:
        return "CORE_HTF_NOT_READY_BEFORE_CANDIDATE"
    if side_row["raw"] and side_row["reason"] != "УСЛОВИЯТА СА ИЗПЪЛНЕНИ":
        return "RAW_S5_TRIGGER_REJECTED"
    if side_row["after_phase"] != 0:
        return "S5_OBSERVATION_WAITING_FOR_RECLAIM"
    if side_row["before_phase"] == 0 and side_row["reason"] == NO_TRIGGER:
        return "NO_S5_FORMATION_TRIGGER"
    return "NO_NEW_S5_ENTRY_THIS_CLOSE"


def chronology(window, frame, bars, side_rows, current, old=None):
    day, begin, end, focus = WINDOWS[window]
    start, stop = stamp(day, begin), stamp(day, end)
    rows = []
    for key, bar in bars.items():
        if not start <= key < stop:
            continue
        side = side_rows[key, focus]
        paper = [row for row in current["paper"] if row["known_time"] == key]
        old_paper = [row for row in old["paper"] if row["known_time"] == key] if old else None
        shadow = [row for row in current["shadow"] if row["time_ms"] == key and row["side"] == focus]
        state = current["state"][key]
        require(state["permission"] == bar["permission"], "EDA_BAR and DTV_STATE permission differ")
        rows.append({
            "window": window, "frame": frame, "confirmed_close_utc": utc(key), "focus": SIDES[focus],
            "fresh_decision": decision(bar, side, current, paper),
            "fresh_s5_reason": side["reason"], "fresh_s5_before_phase": side["before_phase"],
            "fresh_s5_after_phase": side["after_phase"], "fresh_s5_raw_trigger": side["raw"],
            "fresh_core_resolution": bar["core_htf"], "fresh_core_readiness": bar["readyHTF"], "fresh_warmup": bar["warmup"],
            "fresh_advisory_h1": direction_label(bar["h1_direction"]), "fresh_advisory_h4": direction_label(bar["h4_direction"]),
            "chart_previous_close": bar["previous_close"], "chart_current_close": bar["close"],
            "original_prior_20_opposite_edge": side["range20_edge"],
            "fresh_slot_before_phase": bar["before_paper_phase"], "fresh_slot_after_phase": bar["after_paper_phase"],
            "fresh_permission": state["permission"], "fresh_rc2_early_direction": bar["early_side"],
            "fresh_six_bar_structure_direction": bar["six_bar_side"],
            "fresh_directional_shadow_known": " | ".join(f'{r["model"]}/{r["state"]}/{r["reason"]}' for r in shadow),
            "fresh_paper_events_known": " | ".join(f'{r["event"]}/{SIDES[r["side"]]}' for r in paper),
            "old_permission": old["state"][key]["permission"] if old else None,
            "old_paper_events_known": " | ".join(f'{r["event"]}/{SIDES[r["side"]]}' for r in old_paper) if old else None,
            "old_exact_core_tuple_and_readiness": None,
        })
    return rows


def normalized(data):
    return json.loads(gzip.decompress(data) if data.startswith(b"\x1f\x8b") else data)


def compare_market(old, new):
    shared = sorted(old.keys() & new.keys())
    mismatches = []
    for key in shared:
        changes = {
            field: {"old": old[key][field], "fresh": new[key][field]} for field in FIELDS
            if old[key][field] != new[key][field]
        }
        if changes:
            mismatches.append({"bar_open_utc": utc(key), "old_index": old[key]["index"],
                               "fresh_index": new[key]["index"], "fields": changes})
    return {
        "common_bars": len(shared), "common_first_open_utc": utc(shared[0]),
        "common_last_open_utc": utc(shared[-1]), "common_last_close_utc": utc(old[shared[-1]]["time_close"]),
        "old_only_bars": len(old.keys() - new.keys()), "fresh_only_bars": len(new.keys() - old.keys()),
        "exact_clock_and_ohlc_common_bars": len(shared) - len(mismatches),
        "clock_or_ohlc_mismatches": mismatches,
    }


def tape_protected_summary(t, native):
    return {
        "permission_counts": Counter(row["permission"] for row in native["state"].values()),
        "raw_paper_counts": Counter(row["event"] for row in native["paper"]),
        "raw_paper_ready_plan_events": list(native["ready"].values()),
        "paper_entries_and_terminal_events": [condense_paper(r) for r in native["paper"]
                                            if r["event"] in ("SIM_ENTRY", "CLOSED", "CANCELLED")],
        "first_protected_ledger": native["ledger"][0], "last_protected_ledger": native["ledger"][-1],
        "exact_selected_core_tuple_logged": False,
        "aggregate_core_readiness_logged": bool(t["EDA_BAR"]),
    }


def assemble(files):
    fresh_norm = normalized(files["fresh_nov11"])
    recent = normalized(files["fresh_nov13_m5"])["M5"]
    parity = normalized(files["fresh_nov11_parity"])
    recent_parity = normalized(files["fresh_nov13_m5_parity"])
    require(parity["control_commit"] == recent_parity["control_commit"] == VISUAL, "Wrong within-run reference")
    report = {}
    rows = []
    with zipfile.ZipFile(io.BytesIO(files["pinned_native"])) as old_zip, zipfile.ZipFile(io.BytesIO(files["fresh_native"])) as new_zip:
        for frame in FRAMES:
            old, old_sha = tape(old_zip, f"candidate-{frame}.csv")
            new, new_sha = tape(new_zip, f"native/audit-on-{frame}.csv")
            match = parity["frames"][frame]
            require(match["status"] == "PROTECTED_NATIVE_PARITY" and
                    match["files"]["audit-on"]["sha256"] == new_sha, "Fresh tape differs from parity witness")
            a, old_history = parse_history(old)
            b, fresh_history = parse_history(new)
            ad, bd = supplied_natives(old), supplied_natives(new)
            bars = verify_fresh_eda(new, fresh_norm[frame], frame)
            side_rows = phases(fresh_norm[frame])
            for name in ("nov11_buy", "nov11_sell", "nov11_fixed_sell_hour"):
                rows.extend(chronology(name, frame, bars, side_rows, bd, ad))
            report[frame] = {
                "old_visual_v2_export_sha256": old_sha, "fresh_eda_on_export_sha256": new_sha,
                "fresh_same_input_on_off_vs_visual_v2_protected_parity": match["status"],
                "old_visual_v2_formation_and_execution_log_scope": tape_protected_summary(old, ad),
                "fresh_eda_formation_and_execution_log_scope": tape_protected_summary(new, bd),
                "old_visual_v2_history": old_history, "fresh_eda_history": fresh_history,
                "logged_history_overlap": compare_market(a, b),
                "nov11_protected_window_clock_comparison": compare_ctx(context(old), context(new)),
                "cross_archive_protected_payload_comparison": stream_comparison(old, new),
                "fresh_eda_readiness": support_summary(bars, side_rows),
            }
        recent_tape, recent_sha = tape(new_zip, "native-recent/audit-on-M5.csv")
        match = recent_parity["frames"]["M5"]
        require(match["status"] == "PROTECTED_NATIVE_PARITY" and
                match["files"]["audit-on"]["sha256"] == recent_sha, "Recent M5 tape differs from parity witness")
        native = supplied_natives(recent_tape)
        bars = verify_fresh_eda(recent_tape, recent, "M5")
        side_rows = phases(recent)
        rows.extend(chronology("nov13_m5_witness", "M5", bars, side_rows, native))
        recent_report = {
            "archive_sha256": recent_sha, "readiness": support_summary(bars, side_rows),
            "raw_paper_events": [condense_paper(row) for row in native["paper"]],
            "raw_ledger_final": native["ledger"][-1], "fresh_same_input_parity": match["status"],
        }

    counts = {}
    for name in WINDOWS:
        counts[name] = {}
        for frame in FRAMES:
            subset = [row for row in rows if row["window"] == name and row["frame"] == frame]
            if subset:
                counts[name][frame] = {"closed_bars": len(subset),
                                       "focus_side_code": Counter(r["fresh_decision"] for r in subset)}
    return {
        "schema": "qirvanta.core_htf.prior_archives.v1",
        "provenance": {name: dict(zip(("git_commit", "git_file", "sha256"), spec)) for name, spec in SOURCES.items()},
        "nov11_frames": report, "nov13_m5_execution_witness": recent_report,
        "case_window_counts": counts,
        "scope": {
            "history": "Confirmed DT_MARKET prefix ending at each archive's own DT_END. Cross-archive comparison is observational. Different loaded origins, calculation indices, and any pre-window OHLC mismatches break strict reproducibility.",
            "chart_and_htf": "Matching chart OHLC and advisory H1/H4 close timestamps establish the scored market bars, not identical HTF source EMA state. H1/H4 null is missing, 0 is a valid mixed reading.",
            "exact_selected_core_tuple": "Old and fresh source archives lack the exact selected core tuple field values. The newer EDA logs aggregate readyHTF and the selected timeframe; the old Visual V2 tape contains neither explicit field readiness nor aggregate readyHTF. No per-field availability is inferred from the old S5 actions or from advisory sampling.",
            "source_of_absent_candidate": "In fresh EDA, false selected core readiness with the explicit warmup reason means the original f_tvCandidate exits before forming S5. A warmed phase 0 to 0 and no raw trigger indicates an unstarted formation. Shadow geometry and an independently occupied slot are distinct from candidate risk evaluation.",
            "open_close_clock": "DTV_PAPER known_utc is its reporting close and effective_utc is the modeled execution timestamp where recorded. All trades here are RAW PAPER, not broker events.",
            "verification": "Protected ON/OFF versus Visual V2 comparisons apply within archived run groups. No new TradingView action, independent reload or external provider access is claimed here.",
        },
    }, rows


def decimal_as_text(value):
    if not isinstance(value, Decimal):
        raise TypeError(f"Unexpected archive type {type(value).__name__}")
    return str(value)


def pretty(data):
    return (json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, default=decimal_as_text) + "\n").encode("utf-8")


def run(args):
    files = {name: pinned(args.repo, spec) for name, spec in SOURCES.items()}
    report, rows = assemble(files)
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "prior-archive-comparison.json").write_bytes(pretty(report))
    with (args.output / "prior-archive-chronology.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=tuple(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    print(f"{len(rows)} archived decision rows; {len(report['nov11_frames'])} matched chart frames")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--repo", type=Path, default=Path.cwd())
    cli.add_argument("--output", type=Path, required=True)
    run(cli.parse_args())

