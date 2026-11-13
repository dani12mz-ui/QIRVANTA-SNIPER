"""Audit pinned OANDA chart exports and exact Visual V2/RC2 evidence."""

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from subprocess import check_output
from tempfile import TemporaryDirectory
from zipfile import ZipFile
from zoneinfo import ZoneInfo

from gold_pro.direct_breakout_research import EVENT_CONFIG, FORWARD_SEAL, RC2_ARCHIVE, independent_hours, pinned
from gold_pro.rc1_qa import compare_exports, read_export
from gold_pro.rc2_qa import MODES, extras, native_checks
from research.luminous_visual_v2.compare_native import verify_frame
from research.market_coverage_entry_quality.radar import HOUR, PRE_HOUR, clock as event_clock, lifecycle, warning_clocks
from research.professional_signal_selector.native_qa import log_messages


ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PINE = "research/luminous_visual_v2/QIRVANTA_GOLD_PRO_VISUAL_V2.pine"
VISUAL_DIR = ROOT / "research/luminous_visual_v2/evidence"
VISUAL_SHA = "fec83cd615923b210b6815852a69b74ed457f791ac41b3913a7c189f7d58f952"
FRAMES = (5, 15)
OHLC = ("open", "high", "low", "close")
NEW_YORK = ZoneInfo("America/New_York")


def digest(source):
    return sha256(source).hexdigest()


def clock(stamp, zone=timezone.utc):
    return datetime.fromtimestamp(stamp / 1000, zone)


def utc(stamp):
    return clock(stamp).isoformat().replace("+00:00", "Z")


def candle_times(row, step, cutoff):
    stamps = tuple(Decimal(str(row[k])) for k in ("time", "time_close"))
    if any(not value.is_finite() or value != value.to_integral_value() for value in stamps):
        raise ValueError("Expected exact UTC milliseconds")
    opening, closing = (int(v) for v in stamps)
    if opening % step or closing != opening + step or closing >= cutoff:
        raise ValueError(f"Misaligned candle or sealed time at {utc(opening)}")
    return opening


def put(bars, row, step, cutoff):
    opening = candle_times(row, step, cutoff)
    if bars and opening < next(reversed(bars)) + step:
        raise ValueError(f"Duplicate or out of order at {utc(opening)}")
    o, h, low, c = prices = tuple(Decimal(str(row[key])) for key in OHLC)
    if any(not value.is_finite() or value <= 0 for value in prices) or not low <= min(o, c) <= max(o, c) <= h:
        raise ValueError(f"Invalid OHLC at {utc(opening)}")
    bars[opening] = prices


def registered_bars(folder, item, frame, cutoff):
    path = folder / Path(item["local_csv"]).parent.name / "bars.csv"
    sha = digest(path.read_bytes())
    if sha != item["csv_sha256"]:
        raise ValueError(f"Registered M{frame} source hash changed")
    step = frame * 60_000
    rows = {}
    with path.open(newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream, strict=True):
            put(rows, row, step, cutoff)
    if (len(rows), next(iter(rows)), next(reversed(rows)) + step) != (
            item["bar_count"], item["first_open"], item["last_close"]):
        raise ValueError(f"Registered M{frame} range changed")
    return rows, sha


def native_bars(path, frame, cutoff):
    rows = {}
    end = None
    repeated = 0
    with path.open(newline="", encoding="utf-8-sig") as stream:
        for row in csv.DictReader(stream, strict=True):
            for line in row["Message"].splitlines():
                tag, _, payload = line.partition("|")
                if tag not in {"DT_MARKET", "DT_END"}:
                    continue
                value = json.loads(payload, parse_float=Decimal)
                if tag == "DT_END":
                    if end is not None or value["index"] != len(rows) - 1 or value["time_close"] != next(reversed(rows)) + frame * 60_000:
                        raise ValueError("Invalid historical end marker")
                    end = value
                    continue
                if value["symbol"] != "OANDA:XAUUSD" or value["timeframe"] != frame:
                    raise ValueError("TradingView market differs")
                if value["time"] in rows and value["index"] == len(rows) - 1 and value["time"] == next(reversed(rows)):
                    candle_times(value, frame * 60_000, cutoff)
                    if tuple(Decimal(str(value[k])) for k in OHLC) != rows[value["time"]]:
                        raise ValueError("Conflicting duplicate at native boundary")
                    repeated += 1
                else:
                    if value["index"] != len(rows):
                        raise ValueError("Native confirmed history discontinuity")
                    put(rows, value, frame * 60_000, cutoff)
    if end is None:
        raise ValueError("No native historical end marker")
    return rows, end, repeated


def splice(older, newer, frame):
    common = set(older) & set(newer)
    conflicts = [stamp for stamp in sorted(common) if older[stamp] != newer[stamp]]
    if not common or conflicts:
        raise ValueError(f"M{frame} missing calibration overlap or conflicts: {[utc(v) for v in conflicts[:6]]}")
    internal = [stamp for stamp in newer if stamp not in older and min(older) <= stamp <= max(older)]
    if internal:
        raise ValueError(f"M{frame} older source lacks {len(internal)} native candles inside registered span")
    return dict(sorted((older | newer).items())), len(common)


def describe(rows, frame):
    durations = Counter()
    details = []
    step = frame * 60_000
    for left, right in zip(rows, list(rows)[1:]):
        end = left + step
        if end == right:
            continue
        previous, next_open = clock(end, NEW_YORK), clock(right, NEW_YORK)
        delta = (right - end) // 60_000
        session_clock = (previous.hour, previous.minute, next_open.hour, next_open.minute)
        if (previous.weekday(), next_open.weekday()) == (4, 6) and session_clock == (17, 0, 18, 0) and delta in (2880, 2940, 3000):
            kind = "WEEKEND_SESSION_BOUNDARY"
        elif previous.date() == next_open.date() and session_clock == (17, 0, 18, 0) and delta == 60:
            kind = "REGULAR_SESSION_BOUNDARY"
        else:
            kind = "UNVERIFIED_OR_SPECIAL_SESSION_GAP"
        durations[f"{kind}:{delta}m"] += 1
        details.append({"previous_close_utc": utc(end), "next_open_utc": utc(right), "minutes": delta,
                        "previous_close_ny": previous.isoformat(), "next_open_ny": next_open.isoformat(), "classification": kind})
    return {"bars": len(rows), "first_open_utc": utc(min(rows)), "last_close_utc": utc(max(rows) + step),
            "days_elapsed": (max(rows) + step - min(rows)) / 86_400_000, "gap_counts": dict(durations),
            "gap_details": details}


def aligned(m5, m15):
    first = max(min(m5), min(m15))
    end = min(max(m5) + 300_000, max(m15) + 900_000)
    compared = partial = 0
    for stamp, actual in m15.items():
        if stamp < first or stamp + 900_000 > end:
            continue
        parts = [m5.get(stamp + offset * 300_000) for offset in range(3)]
        if None in parts:
            partial += 1
            continue
        observed = (parts[0][0], max(bar[1] for bar in parts), min(bar[2] for bar in parts), parts[2][3])
        if actual != observed:
            raise ValueError(f"M5 to M15 mismatch at {utc(stamp)}")
        compared += 1
    absent_m15 = [utc(stamp) for stamp in m5 if first <= stamp <= end - 900_000
                  and stamp % 900_000 == 0 and stamp + 300_000 in m5 and stamp + 600_000 in m5
                  and stamp not in m15]
    return {"first_open_utc": utc(first), "last_close_utc": utc(end),
            "days_elapsed": (end - first) / 86_400_000, "exact_m15_bars": compared,
            "m15_without_all_three_m5": partial, "complete_m5_triplets_without_native_m15": absent_m15}


def exact_current(work, source):
    frames = {}
    for frame in FRAMES:
        result = verify_frame(work, frame)
        if result["status"] != "MATCH":
            raise ValueError(f"Published Visual V2 M{frame} parity fails: {result['status']}")
        audit = result["indicator"]
        lane = audit["s5_raw_paper"]
        candidate = audit["visibility"]
        state = result["normalized_logs"]["streams"]
        frames[f"M{frame}"] = {
            "candidate_csv_sha256": digest((work / f"candidate-M{frame}.csv").read_bytes()),
            "audited_closes": candidate["closed_bars"],
            "first_audited_open_utc": candidate["first_exported_open_utc"],
            "last_audited_close_utc": candidate["last_exported_close_utc"],
            "core_ready_and_legacy_alerts": audit["core_ready_and_compatibility"]["rows"],
            "early_observations": state["RC2_EARLY"]["candidate"],
            "raw_paper_execution_events": lane["execution_events"], "raw_paper_final_r": lane["final_ledger_r"],
            "historical_market_and_shadow_bars": result["shadow_prefix"]["common_bars"],
            "luminous_events": result["luminous_zone_events"]["counts"][1],
        }
    return {"status": "EXACT_ARCHIVED_NATIVE_PARITY", "pine_sha256": digest(source), "frames": frames}


def earlier_rc2(parent, cutoff):
    frames = {}
    archive = pinned(RC2_ARCHIVE)
    with ZipFile(BytesIO(archive)) as refs:
        for frame in FRAMES:
            frame_results = {}
            for lane in ("parity", "fixed"):
                path = Path(refs.extract(f"{lane}_M{frame}.csv", parent))
                audit = read_export(path, frame, "audit")
                if audit.context[-1].closing >= cutoff:
                    raise ValueError("Sealed RC2 market rows are excluded")
                frame_results[lane] = audit, native_checks(path, audit, extras(path), frame, MODES[lane])
            parity, fixed = (frame_results[k][0] for k in ("parity", "fixed"))
            comparison = compare_exports(parity, fixed, ("core", "ready"))
            if comparison["status"] != "MATCH":
                raise ValueError("Archived RC2 strategy common CORE/READY changed")
            frames[f"M{frame}"] = {
                "first_open_utc": utc(parity.context[0].opening),
                "last_close_utc": utc(parity.context[-1].closing), "audited_closes": len(parity.context),
                "core_and_ready_match": True,
                "s5_raw_strategy": frame_results["parity"][1], "rc2_fixed_strategy": frame_results["fixed"][1],
            }
    return {"archive_sha256": digest(archive), "scope": "Earlier RC2 strategy build, distinct from current indicator RAW PAPER.", "frames": frames}


def native_extension(work, merged, original_end):
    """Audit every independently labelled new-tail episode against available native clocks."""
    bars = [{"time": at, "time_close": at + 300_000, **dict(zip(OHLC, map(float, prices)))}
            for at, prices in merged[5].items()]
    rules = json.loads(pinned(EVENT_CONFIG))["event"]
    catalog, _, _ = independent_hours(bars, 5, rules)
    hours = [
        {"start_utc": utc(row["start"]), "start_ms": row["start"], "end_ms": row["end"], "side": row["side"],
         "directional_extreme_distance": row["directional_extreme_distance"]}
        for row in catalog if row["start"] >= original_end
    ]
    for frame in FRAMES:
        messages = log_messages(work / f"candidate-M{frame}.csv")

        def payloads(tag):
            return [json.loads(line.split("|", 1)[1]) for line in messages if line.startswith(tag + "|")]

        state, ready, paper = (payloads(tag) for tag in ("DTV_STATE", "RC1_READY", "DTV_PAPER"))
        end, = payloads("DT_END")
        native_bars = [row for row in payloads("DT_MARKET") if row["index"] <= end["index"]]
        events = [row for row in payloads("DT_SHADOW") if row["bar"] <= end["index"]]
        _, continuation, _ = lifecycle(native_bars, events)
        early = []
        for line in messages:
            if line.startswith("RC2_EARLY|"):
                _, _, _, _, stamp, side, _ = line.split("|")
                early.append({"known_ms": int(stamp), "side": 1 if side == "BUY" else -1})
        start_qa, end_qa = min(row["known_ms"] for row in state), max(row["known_ms"] for row in state)
        clocks = warning_clocks(early, events, native_bars[0]["time"], end["time_close"])
        catalog_sides = {(row["start"], row["side"]) for row in catalog}
        for hour in hours:
            start, stop, side = (hour[k] for k in ("start_ms", "end_ms", "side"))
            first = next((w for w in clocks if w["side"] == side and start - PRE_HOUR <= w["known_ms"] < stop
                          and (w["known_ms"] >= start or (start - HOUR, side) not in catalog_sides)), None)
            geometry = [r for r in ready if start <= r["confirmed_ms"] < stop
                        and r["direction"] == ("BUY" if side == 1 else "SELL")]
            fills = [r for r in paper if r["event"] == "SIM_ENTRY"
                     and start <= r["effective_time"] < stop and r["side"] == side]
            closes = [r for r in state if start <= r["known_ms"] < stop]
            full_native_window = start_qa <= start and stop <= end_qa + frame * 60_000
            directions = [r for r in events if start - PRE_HOUR <= r["time_ms"] < stop and r["side"] == side]
            hour[f"M{frame}"] = {
                "combined_direction_scope": "COMPLETE_WINDOW" if start_qa <= start - PRE_HOUR and full_native_window
                else "RC2_EARLY_UNAVAILABLE_FOR_PART_OR_ALL_OF_WINDOW",
                "first_logged_directional_observation": first | {
                    "known_utc": utc(first["known_ms"]), "latency_minutes": (first["known_ms"] - start) / 60_000,
                } if first else None,
                "source_diagnostics": directions,
                "same_side_new_continuations": [r for r in directions if r["state"] == "BREAKOUT CONFIRMED"
                                                and start <= r["time_ms"] and event_clock(r) in continuation],
                "native_s5_decision_scope": "COMPLETE_WINDOW" if full_native_window else "PARTIAL_OR_UNAVAILABLE",
                "native_s5_permissions_by_close": [
                    {"known_ms": r["known_ms"], "known_utc": utc(r["known_ms"]),
                     "permission": r["permission"], "reason": r["permission_reason"]}
                    for r in closes
                ],
                "native_s5_same_side_ready": geometry, "native_raw_paper_same_side_sim_entries": fills,
                "absence_semantics": "Unknown outside protected QA logging; no approved entry inferred from Shadow.",
            }
    return {"hours": hours, "control_comparability": "Both frames use merged M5 market-hour labels. Current "
            "native Shadow sources cover their DT_END prefix; S5/RC2 protected state only covers the QA window."}


def audit(folder):
    protocol = json.loads((HERE / "DATA_PROTOCOL.json").read_text(encoding="utf8"))
    cutoff = json.loads(pinned(FORWARD_SEAL))["forward_start_ms"]
    pin = protocol["baseline"]
    source = check_output(["git", "show", f"{pin['commit']}:{PINE}"], cwd=ROOT)
    if digest(source) != pin["sha256"] or source != (ROOT / PINE).read_bytes():
        raise ValueError("Visual V2 source differs from the requested baseline")
    registration = json.loads((ROOT / "research/reports/actual_tf_registration.json").read_text())
    entries = {int(row["timeframe"]): row for row in registration["sources"]}
    archive = (VISUAL_DIR / "native-pairs.zip").read_bytes()
    if digest(archive) != VISUAL_SHA:
        raise ValueError("Visual V2 archive hash changed")
    result, merged = {}, {}
    with TemporaryDirectory() as temp, ZipFile(BytesIO(archive)) as pack:
        work = Path(temp)
        for frame in FRAMES:
            for role in ("baseline", "candidate"):
                pack.extract(f"{role}-M{frame}.csv", work)
            older, sha = registered_bars(folder, entries[frame], frame, cutoff)
            native_file = work / f"candidate-M{frame}.csv"
            newer, native_end, repeated = native_bars(native_file, frame, cutoff)
            bars, overlap = splice(older, newer, frame)
            result[f"M{frame}"] = {
                "registration_sha256": sha, "visual_archive_csv_sha256": digest(native_file.read_bytes()),
                "registered": describe(older, frame), "native_tail": describe(newer, frame),
                "native_historical_parity_end_utc": utc(native_end["time_close"]),
                "confirmed_native_repeat_rows_preserved_as_one": repeated,
                "exact_overlap": overlap, "native_additions": len(bars) - len(older),
                "merged": describe(bars, frame),
            }
            merged[frame] = bars
        current = exact_current(work, source)
        extension = native_extension(work, merged, entries[5]["last_close"])
        earlier = earlier_rc2(work, cutoff)
    return {"status": "MATCHED_DATASETS_VERIFIED_INSUFFICIENT_FOR_NEW_CANDIDATE",
            "visual_archive_sha256": VISUAL_SHA, "frames": result,
            "common_market": aligned(merged[5], merged[15]),
            "current_visual_v2_native": current, "earlier_rc2_strategy_native": earlier,
            "newly_joined_native_development_episodes": extension,
            "reserved_test": protocol["partition"]["untouched_test"], "sealed_start_utc": utc(cutoff)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = audit(args.data_root)
    args.output.write_text(json.dumps(result, sort_keys=True, indent=2, allow_nan=False, default=str) + "\n", encoding="utf-8")
    print(f"Verified {result['common_market']['exact_m15_bars']} matching M15 aggregates.")


if __name__ == "__main__":
    main()
