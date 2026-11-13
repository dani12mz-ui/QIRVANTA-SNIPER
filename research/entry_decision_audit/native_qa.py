"""Compare native Visual V2 control, entry audit ON, and entry audit OFF."""

import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
from decimal import Decimal
from itertools import zip_longest
import json
from pathlib import Path

from gold_pro import rc1_qa as rc1, rc2_qa as rc2
from research.direct_trend_breakout import shadow_native
from research.direct_trend_visibility.native_qa import journal_rows, paper_evidence
from research.luminous_visual_v2.compare_native import confirmed_history
from research.professional_signal_selector.native_qa import FRAMES, SHADOW_TAGS, log_messages, pressure_pair


CONTROL = "37b903f46a2d878d9d0757da134620c22f2d067d"
CONTROL_SHA = "77feedcbef84e77b9270ec43840be5b823d838300d0a09d5ddb4fec7acc089f4"
SEALED_START = 1794787200000
AUDIT_TAGS = {"EDA_BAR", "EDA_SIDE"}
ROLES = ("baseline", "audit-on", "audit-off")
BAR_FIELDS = (
    "version symbol frame close_ms first_open_ms index open high low close warmup readyHTF core_htf "
    "h1_closed_ms h1_direction h4_closed_ms h4_direction gap atr_mean atr_pass bar_range max_entry_range_atr "
    "date_pass news_paused session_pass before_paper_phase before_paper_plan_id after_paper_phase "
    "after_paper_plan_id selected_id qty_ok rc2_filter_active rc2_blocked permission permission_reason mintick "
    "min_atr_ratio max_atr_ratio min_stop_atr max_stop_atr max_cost_risk max_open_dev_atr min_cooldown "
    "rc2_range_atr_limit open_ms previous_close early_side six_bar_side"
).split()
SIDE_FIELDS = (
    "version symbol frame close_ms side before_phase before_born_ms before_born_index before_anchor "
    "before_level last_accepted_index after_phase after_born_ms after_born_index after_anchor after_level "
    "raw reason signed_body atr directional_close_fraction cooldown_pass range20_edge"
).split()
BAR_FLAGS = set(
    "warmup readyHTF gap atr_pass date_pass news_paused session_pass qty_ok rc2_filter_active rc2_blocked".split()
)
SIDE_FLAGS = {"raw", "cooldown_pass"}


def timestamp(value):
    clock = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if clock.utcoffset() is None or clock.utcoffset().total_seconds() != 0:
        raise argparse.ArgumentTypeError("the audit clock must be explicit UTC")
    return int(clock.timestamp() * 1000)


def parse_numbers(row, fields, parser):
    for field in fields:
        if row[field] != "null":
            row[field] = parser(row[field])
        else:
            row[field] = None


def require_flags(row, fields, allowed_na=()):
    for field in fields:
        value = row[field]
        rc1.require(value in ("true", "false") or field in allowed_na and value == "NA",
                    f"Invalid {field} flag {value!r}")
        row[field] = None if value == "NA" else value == "true"


def parse_row(line, fields, frame):
    parts = line.split("|")
    rc1.require(len(parts) == len(fields) + 1, f"{parts[0]} field count {len(parts) - 1}, expected {len(fields)}")
    row = dict(zip(fields, parts[1:]))
    rc1.require((row["version"], row["symbol"], row["frame"]) == ("1", rc1.SYMBOL, str(frame)),
                f"{parts[0]} schema, symbol or timeframe differs")
    row["close_ms"] = rc1.epoch(int(row["close_ms"]), "EDA close")
    return row


def bar_row(line, frame, ctx, states, ledger):
    row = parse_row(line, BAR_FIELDS, frame)
    stamp = row["close_ms"]
    bar = ctx.get(stamp)
    rc1.require(bar is not None, f"EDA_BAR lacks RC1_CTX at {stamp}")
    parse_numbers(row, "first_open_ms index h1_closed_ms h1_direction h4_closed_ms h4_direction "
                  "before_paper_phase after_paper_phase min_cooldown open_ms early_side six_bar_side".split(), int)
    parse_numbers(row, ("open high low close atr_mean bar_range max_entry_range_atr mintick min_atr_ratio "
                        "max_atr_ratio min_stop_atr max_stop_atr max_cost_risk max_open_dev_atr "
                        "rc2_range_atr_limit previous_close").split(),
                  lambda value: rc1.number(value, "EDA_BAR numeric"))
    require_flags(row, BAR_FLAGS, {"qty_ok"})
    ohlc = tuple(row[name] for name in ("open", "high", "low", "close"))
    rc1.require((row["first_open_ms"], row["index"], row["open_ms"], ohlc) ==
                (bar.first_open, bar.index, bar.opening, bar.ohlc),
                f"EDA_BAR chart clock or OHLC differs from RC1_CTX at {stamp}")
    rc1.require((row["h1_closed_ms"], row["h4_closed_ms"]) == bar.htf_closes,
                f"EDA_BAR confirmed H1/H4 clock differs from RC1_CTX at {stamp}")
    for key in ("h1", "h4"):
        direction = row[f"{key}_direction"]
        rc1.require(direction in (-1, 0, 1, None) and
                    (row[f"{key}_closed_ms"] is not None or direction is None),
                    f"EDA_BAR {key} direction suggests data with unavailable source at {stamp}")
    rc1.require(bool(row["core_htf"]) and all(row[k] in (-1, 0, 1) for k in ("early_side", "six_bar_side")),
                f"EDA_BAR invalid HTF resolution or observation side at {stamp}")
    rc1.require(0 < row["min_stop_atr"] < row["max_stop_atr"] and 0 < row["min_atr_ratio"] < row["max_atr_ratio"]
                and row["min_cooldown"] >= 0, f"EDA_BAR invalid original gate thresholds at {stamp}")
    row["permission_reason"] = row["permission_reason"].replace("~", "|")
    rc1.require(row["bar_range"] == max(ohlc[1] - ohlc[2], row["mintick"]) and row["mintick"] == Decimal("0.001"),
                f"EDA_BAR invalid range or OANDA tick size at {stamp}")
    rc1.require((row["permission"], row["permission_reason"]) ==
                (states[stamp]["permission"], states[stamp]["permission_reason"]),
                f"EDA_BAR permission differs from DTV_STATE at {stamp}")
    for key in ("before_paper_plan_id", "after_paper_plan_id", "selected_id"):
        row[key] = row[key].replace("~", "|")
    rc1.require(row["before_paper_phase"] in (0, 1, 2) and
                bool(row["before_paper_plan_id"]) == (row["before_paper_phase"] != 0),
                f"EDA_BAR invalid prior RAW phase or associated plan at {stamp}")
    rc1.require((row["qty_ok"] is not None) == bool(row["selected_id"]),
                f"EDA_BAR quantity-grid check does not match selected plan availability at {stamp}")
    rc1.require((row["after_paper_phase"], row["after_paper_plan_id"]) ==
                (ledger[stamp]["phase"], ledger[stamp]["plan_id"]),
                f"EDA_BAR closing paper state differs from DTV_LEDGER at {stamp}")
    return row


def side_row(line, frame, ctx):
    row = parse_row(line, SIDE_FIELDS, frame)
    stamp = row["close_ms"]
    bar = ctx.get(stamp)
    rc1.require(bar is not None, f"EDA_SIDE lacks RC1_CTX at {stamp}")
    parse_numbers(row, "side before_phase before_born_ms before_born_index last_accepted_index "
                  "after_phase after_born_ms after_born_index".split(), int)
    parse_numbers(row, "before_anchor before_level after_anchor after_level signed_body atr "
                  "directional_close_fraction range20_edge".split(), lambda value: rc1.number(value, "EDA_SIDE numeric"))
    require_flags(row, SIDE_FLAGS, {"cooldown_pass"})
    rc1.require(row["side"] in (-1, 1) and all(row[field] in (0, 1) for field in ("before_phase", "after_phase")),
                f"EDA_SIDE invalid side or formation phase at {stamp}")
    rc1.require(bool(row["reason"]), f"EDA_SIDE missing actual reason at {stamp}")
    for label in ("before", "after"):
        born = row[f"{label}_born_ms"]
        index = row[f"{label}_born_index"]
        rc1.require(born is None or born <= stamp, f"EDA_SIDE formation born in the future at {stamp}")
        rc1.require(index is None or index <= bar.index, f"EDA_SIDE future formation index at {stamp}")
    rc1.require(row["signed_body"] == (bar.ohlc[3] - bar.ohlc[0]) * row["side"],
                f"EDA_SIDE signed body differs from native OHLC at {stamp}")
    rc1.require(row["raw"] == (row["cooldown_pass"] is not None),
                f"EDA_SIDE cooldown must only be evaluated for a raw trigger at {stamp}")
    fraction = row["directional_close_fraction"]
    rc1.require(fraction is None or Decimal(0) <= fraction <= Decimal(1),
                f"EDA_SIDE invalid close location at {stamp}")
    return row


def diagnostics(messages, frame, run, legacy, path):
    ctx = {bar.closing: bar for bar in run.context}
    states = {row["known_ms"]: row for row in legacy["DTV_STATE"]}
    ledgers = {row["known_ms"]: row for row in legacy["DTV_LEDGER"]}
    rc1.require(ctx.keys() == states.keys() == ledgers.keys(), "EDA needs complete protected state and ledger context")
    early_rows = rc2.early_events(rc2.extras(path)["RC2_EARLY"], run, frame)
    early = {"BUY": 1, "SELL": -1}
    bars = {}
    sides = {}
    for line in messages:
        tag = line.partition("|")[0]
        if tag == "EDA_BAR":
            row = bar_row(line, frame, ctx, states, ledgers)
            key = row["close_ms"]
            rc1.require(key not in bars and (not bars or key > next(reversed(bars))), "EDA_BAR order or duplicate")
            rc1.require(row["early_side"] == early.get(early_rows.get(key), 0),
                        f"EDA_BAR EARLY WATCH differs from RC2_EARLY at {key}")
            bars[key] = row
        elif tag == "EDA_SIDE":
            row = side_row(line, frame, ctx)
            key = row["close_ms"], row["side"]
            rc1.require(key not in sides and (not sides or key[0] >= next(reversed(sides))[0]),
                        "EDA_SIDE order or duplicate")
            sides[key] = row
    rc1.require(bars.keys() == ctx.keys(), "EDA_BAR closes do not cover the complete protected audit range")
    rc1.require(sides.keys() == {(stamp, side) for stamp in ctx for side in (1, -1)},
                "EDA_SIDE must include exactly two opposing rows per audited close")
    for prev, curr in zip(bars, tuple(bars)[1:]):
        if ctx[curr].index == ctx[prev].index + 1:
            rc1.require((bars[prev]["after_paper_phase"], bars[prev]["after_paper_plan_id"]) ==
                        (bars[curr]["before_paper_phase"], bars[curr]["before_paper_plan_id"]),
                        f"EDA_BAR broken RAW PAPER transition at {curr}")
            for side in (1, -1):
                fields = ("phase", "born_ms", "born_index", "anchor", "level")
                rc1.require(tuple(sides[prev, side][f"after_{field}"] for field in fields) ==
                            tuple(sides[curr, side][f"before_{field}"] for field in fields),
                            f"EDA_SIDE broken formation transition at {curr} for {side}")
    return {"bars": list(bars.values()), "sides": list(sides.values())}


def first_difference(reference, comparison):
    for index, (base, candidate) in enumerate(zip_longest(reference, comparison), 1):
        if base != candidate:
            return {"line": index, "baseline": base, "comparison": candidate}
    return None


def verify_frame(directory, frame, begin, end):
    paths = {role: directory / f"{role}-M{frame}.csv" for role in ROLES}
    if missing := [path.name for path in paths.values() if not path.is_file()]:
        return {"status": "MISSING_EXPORTS", "files": missing}, None
    logs = {role: log_messages(path) for role, path in paths.items()}
    histories = {role: confirmed_history(rows) for role, rows in logs.items()}
    runs = {role: rc1.read_export(path, frame, "indicator") for role, path in paths.items()}
    legacy = {role: journal_rows(path) for role, path in paths.items()}
    shadow = {role: shadow_native.read_native(path) for role, path in paths.items()}
    meters = {}
    alerts = {}
    paper = {}
    for role, run in runs.items():
        rc1.require(run.meta["end_marker_present"], f"{role}: RC1_END missing, possibly truncated")
        origin, final = shadow[role].first_index, shadow[role].end
        rc1.require(origin == 0 and final and final["index"] >= 1198, f"{role}: full historical warmup unavailable")
        rc1.require(shadow[role].timeframe == frame, f"{role}: wrong historical timeframe")
        rc1.require(shadow[role].bars[0]["time"] == run.context[0].first_open,
                    f"{role}: Shadow warmup origin differs from protected RC1 history")
        rc1.require(final["time_close"] < SEALED_START, f"{role}: warmup reaches a protected period")
        rc1.require(shadow[role].bars[0]["time"] <= begin <= end <= final["time_close"],
                    f"{role}: requested diagnostic window falls outside witnessed loaded history")
        by_close = {bar["time_close"]: bar for bar in shadow[role].bars}
        target = tuple(stamp for stamp in by_close if begin <= stamp <= end)
        rc1.require(target and tuple(bar.closing for bar in run.context) == target,
                    f"{role}: protected logs missing closed candles in the requested window")
        for bar in run.context:
            dt = by_close[bar.closing]
            rc1.require((dt["time"], tuple(Decimal(str(dt[key])) for key in shadow_native.OHLC)) ==
                        (bar.opening, bar.ohlc), f"{role}: protected OHLC differs from historical DT_MARKET")
        rc1.require(tuple(row["known_ms"] for row in legacy[role]["DTV_STATE"]) == target,
                    f"{role}: incomplete DTV_STATE coverage")
        paper[role] = paper_evidence(legacy[role], run.context, frame)
        rc1.require(paper[role] is not None, f"{role}: missing RAW PAPER ledger")
        alerts[role] = rc2.indicator_checks(run, rc2.extras(paths[role]), frame, research=True)
        if role != "audit-on":
            rc1.require(not any(line.partition("|")[0] in AUDIT_TAGS for line in logs[role]),
                        f"{role}: unexpected diagnostic rows")
    for role in ROLES[1:]:
        meters[role] = pressure_pair(paths["baseline"], paths[role], frame, (runs["baseline"], runs[role]))
    differences = {}
    protected = {role: tuple(line for line in logs[role]
                             if line.partition("|")[0] not in SHADOW_TAGS | AUDIT_TAGS) for role in ROLES}
    for role in ROLES[1:]:
        changes = {}
        if difference := first_difference(histories["baseline"], histories[role]):
            changes["historical_ohlc_shadow"] = difference
        if difference := first_difference(protected["baseline"], protected[role]):
            changes["all_existing_unmodified_streams"] = difference
        if meters[role]["status"] != "MATCH":
            changes["pressure"] = meters[role]
        if changes:
            differences[role] = changes
    report = {
        "status": "MISMATCH" if differences else "PROTECTED_NATIVE_PARITY",
        "files": {role: {"name": path.name, "sha256": rc1.digest(path.read_bytes()),
                         "messages": len(logs[role])} for role, path in paths.items()},
        "coverage": {
            "first_loaded_open_ms": runs["baseline"].context[0].first_open,
            "last_historical_close_ms": shadow["baseline"].end["time_close"],
            "audited_closes": len(runs["baseline"].context),
            "first_audited_close_ms": runs["baseline"].context[0].closing,
            "last_audited_close_ms": runs["baseline"].context[-1].closing,
        },
        "protected_streams": dict(Counter(line.partition("|")[0] for line in protected["baseline"])),
        "legacy_alert_validation": alerts,
        "raw_paper_examples": dict(Counter(row["event"] for row in paper["baseline"][1])),
        "raw_paper_final_ledger": paper["baseline"][0][-1],
        "observational_native_events": len(shadow["baseline"].events),
        "first_differences": differences,
    }
    if not differences:
        report["execution_coverage"] = (
            "SIM_ENTRY_EXERCISED" if any(row["event"] == "SIM_ENTRY" for row in paper["baseline"][1])
            else "NO_SIMULATED_ENTRY_EXAMPLES"
        )
        report["zone_coverage"] = (
            "LZ_EVENT_EXERCISED" if report["protected_streams"].get("LZ_EVENT") else "NO_ZONE_EXAMPLES"
        )
    if differences:
        return report, None
    try:
        rows = diagnostics(logs["audit-on"], frame, runs["audit-on"], legacy["audit-on"], paths["audit-on"])
    except ValueError as exc:
        return {**report, "status": "INCOMPLETE_DIAGNOSTICS", "diagnostic_error": str(exc)}, None
    report["diagnostic_rows"] = {"bar": len(rows["bars"]), "side": len(rows["sides"])}
    return report, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Fresh baseline/audit-on/audit-off-M*.csv")
    parser.add_argument("--frames", nargs="+", choices=FRAMES, default=FRAMES, type=int)
    parser.add_argument("--start", type=timestamp, default="2026-11-11T00:00:00Z")
    parser.add_argument("--end", type=timestamp, default="2026-11-11T17:30:00Z")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--journal", type=Path, help="Where to write validated EDA rows, separate from parity")
    parser.add_argument("--candidate-source", type=Path, help="Local exact source supplied for native compilation")
    parser.add_argument("--settings", type=Path, help="Operator-provided run settings and screenshot references")
    args = parser.parse_args()
    if not args.start < args.end < SEALED_START:
        parser.error("expected a chronological, pre-seal UTC close window")
    source = None if args.candidate_source is None else rc1.digest(args.candidate_source.read_bytes())
    settings = None if args.settings is None else rc1.digest(args.settings.read_bytes())
    frames = {}
    ledger = {}
    for frame in args.frames:
        try:
            result, rows = verify_frame(args.directory, frame, args.start, args.end)
            frames[f"M{frame}"] = result
            if rows is not None:
                ledger[f"M{frame}"] = rows
        except (OSError, ValueError, KeyError, IndexError, TypeError, csv.Error) as exc:
            frames[f"M{frame}"] = {"status": "INVALID_EXPORT", "reason": str(exc)}
    passed = all(row["status"] == "PROTECTED_NATIVE_PARITY" for row in frames.values())
    report = {
        "status": "PROTECTED_NATIVE_PARITY" if passed else "INCOMPLETE_OR_MISMATCH",
        "control_commit": CONTROL, "control_pine_sha256": CONTROL_SHA,
        "candidate_source_sha256_supplied_by_operator": source,
        "settings_manifest_sha256_supplied_by_operator": settings,
        "audited_close_window_ms": {"start_inclusive": args.start, "end_inclusive": args.end},
        "evidence_scope": "Pine Logs prove bounded protected stream parity. Compile, exact configuration and UI captures require separate native witnesses.",
        "alert_scope": "Alert payload and logged intent, without server delivery verification.",
        "frames": frames,
    }
    for path, payload in ((args.output, report), (args.journal, ledger)):
        if path:
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
                            encoding="utf-8", newline="\n")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

