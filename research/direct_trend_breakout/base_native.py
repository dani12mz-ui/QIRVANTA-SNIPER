"""Validate matched TradingView baseline and EARLY WATCH captures."""

import argparse
from collections import Counter
import csv
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

from gold_pro import rc1_qa, rc2_qa
from gold_pro.rc1_qa import require
from research.direct_trend_breakout.forensic_timeline import DIRECTIONS, PRICES, ms, utc, values


ROLES = ("indicator-default", "indicator-early", "audit-fixed", "audit-s5")
INDICATOR_MODES = ("indicator-default", "indicator-early")
CASE_TIMES = tuple(ms(f"2026-11-11T{time}:00Z") for time in
                   ("09:30", "11:30", "11:45", "12:00", "12:15", "12:30", "12:45",
                    "13:00", "13:30", "13:45", "14:00", "14:15", "14:30", "14:45",
                    "15:00", "15:15", "15:30", "15:45", "16:00", "16:15", "16:30"))


def read_trace(path, run, role, frame):
    rows = []
    with path.open(encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source, strict=True):
            tag, _, payload = row["Message"].partition("|")
            if tag == "DT_BASE":
                bar = rc1_qa._json_dict(payload)
                require(ms(row["Date"]) == bar["open_ms"], "DT_BASE: chart-open log time")
                rows.append(bar)
    require(len(rows) == len(run.context) > 0 and run.meta["end_marker_present"], "DT_BASE: incomplete native coverage")
    for row, ctx in zip(rows, run.context):
        require(row["symbol"] == rc1_qa.SYMBOL and row["frame"] == str(frame), "DT_BASE: instrument or timeframe")
        require(values(row, ("open_ms", "known_ms", "first_open_ms")) == (ctx.opening, ctx.closing, ctx.first_open)
                and values(row, PRICES) == ctx.ohlc
                and values(row, ("h1_known_ms", "h4_known_ms")) == ctx.htf_closes,
                "DT_BASE: inconsistent with native compatibility context")
        require(all(row[field] in (-1, 0, 1) for field in ("h1_bias", "h4_bias")), "DT_BASE: HTF direction")
        for field in ("buy_phase", "sell_phase"):
            require(row[field] in (0, 1) and row[field + "_before"] in (0, 1), "DT_BASE: S5 phase")
        if role in INDICATOR_MODES:
            require(row["kind"] == "indicator" and row["early_enabled"] == (role == "indicator-early")
                    and not row["early_alert_enabled"], "DT_BASE: indicator mode mismatch")
        else:
            expected = rc2_qa.MODES["fixed" if role == "audit-fixed" else "parity"]
            require(row["kind"] == "audit" and row["audit_lane"] == expected, "DT_BASE: audit mode mismatch")
    for prior, row in zip(rows, rows[1:]):
        for field in ("buy_phase", "sell_phase"):
            require(row[field + "_before"] == prior[field], "DT_BASE: phase chronology")
    return rows


def check_early(trace, events):
    by_time = {row["known_ms"]: row for row in trace}
    seen = {}
    previous = None
    for row in trace:
        o, h, low, c = values(row, PRICES)
        atr = row["atr_previous"]
        expanded = atr is not None and h - low >= Decimal("1.15") * atr
        bull = expanded and c > row["early_prior_high"] and c - o >= Decimal("0.50") * atr
        bear = expanded and c < row["early_prior_low"] and o - c >= Decimal("0.50") * atr
        require((bull, bear, expanded) == values(row, ("early_bull_geometry", "early_bear_geometry",
                                                       "early_previous_atr_expansion")),
                f"EARLY geometry disagreement at {utc(row['known_ms'])}")
        native_side = row["early_side"]
        if previous is not None:
            expected = 0
            if not row["gap"] and row["early_enabled"]:
                if bull and not previous["early_bull_geometry"]:
                    expected = 1
                if bear and not previous["early_bear_geometry"]:
                    expected = -1
            require(native_side == expected, f"EARLY WATCH edge disagreement at {utc(row['known_ms'])}")
        if native_side:
            seen[row["known_ms"]] = "BUY" if native_side == 1 else "SELL"
        previous = row
    require(seen == events, "DT_BASE: EARLY WATCH events differ from native logs")
    return [
        {"known_utc": utc(stamp), "direction": side, "close": by_time[stamp]["close"],
         "h1": DIRECTIONS[by_time[stamp]["h1_bias"]], "h4": DIRECTIONS[by_time[stamp]["h4_bias"]],
         "prior_range_boundary": by_time[stamp]["early_prior_high" if side == "BUY" else "early_prior_low"]}
        for stamp, side in events.items()
    ]


def frame_report(folder, frame):
    runs, traces, reports, journals = {}, {}, {}, {}
    for role in ROLES:
        path = folder / f"{role}-M{frame}.csv"
        run = runs[role] = rc1_qa.read_export(path, frame, "indicator" if role in INDICATOR_MODES else "audit")
        journal = journals[role] = rc2_qa.extras(path)
        trace = traces[role] = read_trace(path, run, role, frame)
        if role in INDICATOR_MODES:
            check = rc2_qa.indicator_checks(run, journal, frame, research=role == "indicator-early")
            check["native_early_timeline"] = check_early(trace, rc2_qa.early_events(journal["RC2_EARLY"], run, frame))
        else:
            mode = rc2_qa.MODES["fixed" if role == "audit-fixed" else "parity"]
            check = rc2_qa.native_checks(path, run, journal, frame, mode)
            props = journal["RC2_AUDIT_SUMMARY"][0]
            check["native_profit_factor"] = Decimal(props["grossprofit"]) / Decimal(props["grossloss"])
        reports[role] = {
            "file": path.name, "sha256": sha256(path.read_bytes()).hexdigest(), "bars": len(run.context),
            "first_loaded_open_utc": utc(run.context[0].first_open), "first_exported_open_utc": utc(run.context[0].opening),
            "last_exported_close_utc": utc(run.context[-1].closing), "counts": run.meta["events"], **check,
        }

    pairs = (("indicator-default", "indicator-early", ("core", "ready", "alerts")),
             ("audit-fixed", "audit-s5", ("core", "ready")),
             ("indicator-default", "audit-fixed", ("core", "ready")))
    comparisons = {}
    for left, right, streams in pairs:
        lhs, rhs = traces[left], traces[right]
        strict = rc1_qa.compare_exports(runs[left], runs[right], streams)
        same_warmup = lhs[0]["first_open_ms"] == rhs[0]["first_open_ms"]
        if same_warmup:
            require(strict["status"] == "MATCH", f"{left}/{right}: unexpected baseline divergence")
            trace_fields = (lhs[0].keys() & rhs[0].keys()) - {"kind", "early_enabled", "early_side"}
            if left.startswith("audit"):
                trace_fields -= {"audit_lane", "native_position", "native_pending_or_held", "native_admitted"}
            require(all(values(a, trace_fields) == values(b, trace_fields) for a, b in zip(lhs, rhs)),
                    f"{left}/{right}: trace decision divergence")
            comparison = strict
        else:
            comparison = rc2_qa.shared(runs[left], runs[right], streams)
            require(comparison["status"] == "MATCH", f"{left}/{right}: shared decision disagreement")
            comparison["status"] = "MATCH_ON_SHARED_BARS_WARMUP_DIFFERS"
        comparisons[f"{left}_vs_{right}"] = comparison
    require(journals["indicator-default"]["RC2_ALERT"] == journals["indicator-early"]["RC2_ALERT"],
            "EARLY WATCH toggle modified existing alert payloads")

    by_time = {row["known_ms"]: row for row in traces["indicator-default"]}
    audit = {row["known_ms"]: row for row in traces["audit-fixed"]}
    early = {row["known_ms"]: row for row in traces["indicator-early"]}
    case_rows = []
    for known in CASE_TIMES:
        row = by_time[known]
        case_rows.append({
            "open_utc": utc(row["open_ms"]), "known_utc": utc(known),
            **{key: row[key] for key in (*PRICES, "prior20_high", "prior20_low", "buy_phase_before",
                  "buy_phase", "sell_phase_before", "sell_phase", "buy_reason", "sell_reason", "decision",
                  "decision_side", "candidate_count", "ready_plan", "range_pass", "market_bias", "m15_trend")},
            "h1": DIRECTIONS[row["h1_bias"]], "h4": DIRECTIONS[row["h4_bias"]],
            "h1_known_utc": utc(row["h1_known_ms"]), "h4_known_utc": utc(row["h4_known_ms"]),
            "early_side_enabled_capture": early[known]["early_side"], "fixed_position_at_close": audit[known]["native_position"],
        })
    after = ms("2026-11-11T11:30:00Z")
    before = ms("2026-11-11T16:30:00Z")
    moves = [row for stamp, row in by_time.items() if after <= stamp <= before]
    return {"lanes": reports, "comparisons": comparisons, "case_rows": case_rows,
            "candidate_coverage_in_case": dict(Counter(row["decision"] for row in moves)),
            "ready_in_case": [{"known_utc": utc(row["known_ms"]), "side": row["decision_side"],
                              "plan": row["ready_plan"]} for row in moves if row["ready_plan"]]}


def shadow_switch_report(folder, frame):
    off, on = (folder / f"{prefix}-M{frame}.csv" for prefix in ("shadow-off", "shadow"))
    runs = [rc1_qa.read_export(path, frame, "indicator") for path in (off, on)]
    comparison = rc1_qa.compare_exports(*runs, ("core", "ready", "alerts"))
    require(comparison["status"] == "MATCH", "Shadow toggle altered the native RC2 decision stream")
    journals = [rc2_qa.extras(path) for path in (off, on)]
    for tag in ("RC2_ALERT", "RC2_EARLY", "RC2_FILTER"):
        require(journals[0][tag] == journals[1][tag], f"Shadow toggle altered {tag}")
        comparison["rows"][tag] = len(journals[0][tag])
    return {
        **comparison,
        "bars": len(runs[0].context),
        "first_loaded_open_utc": utc(runs[0].context[0].first_open),
        "files": {path.name: sha256(path.read_bytes()).hexdigest() for path in (off, on)},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "evidence" / "base-native.json")
    args = parser.parse_args()
    summary = {
        "scope": "Native OANDA:XAUUSD confirmed chart bars. Strategy Tester executions and alert payloads only.",
        "case_coverage_utc": ["2026-11-11T11:30:00Z", "2026-11-11T16:30:00Z"],
        "frames": {f"M{frame}": frame_report(args.native_dir, frame) for frame in (5, 15)},
        "shadow_off_vs_on": {f"M{frame}": shadow_switch_report(args.native_dir, frame) for frame in (5, 15)},
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str) + "\n",
                           encoding="utf-8", newline="\n")
    print("Verified eight native base captures and the EARLY WATCH edge sequence")


if __name__ == "__main__":
    main()
