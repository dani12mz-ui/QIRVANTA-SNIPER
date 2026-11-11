"""Check the pressure calculation and compare native RC2 TradingView Pine Logs."""

from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import csv
from dataclasses import dataclass, replace
from decimal import Decimal
from itertools import zip_longest
import json
from pathlib import Path

from gold_pro import rc1_qa as rc1, rc2_qa as rc2
from gold_pro.rc1_qa import EvidenceError, require


BASELINE = "cb848e1e4d6d2686c524ac1a9136804748e9f577"
COMPARISON_FRAMES = (5, 15, 60)
TOLERANCE = Decimal("0.000001")
UNAVAILABLE = {"WARMUP", "UNAVAILABLE"}
MODES = {"OHLC", "TICK", "FALLBACK", *UNAVAILABLE}
EVIDENCE_ERRORS = (OSError, UnicodeError, csv.Error, ValueError, KeyError, TypeError)
MODE_REASONS = {
    "OHLC": {"OHLC_SELECTED"},
    "TICK": {"TICK_WEIGHTED"},
    "FALLBACK": {"NON_TICK_SOURCE", "INVALID_TICK_VOLUME", "ZERO_TICK_VOLUME"},
    "WARMUP": {"INSUFFICIENT_HISTORY"},
    "UNAVAILABLE": {"SOURCE_UNAVAILABLE", "INVALID_OHLC"},
}


class MissingPressureEvidence(EvidenceError):
    """The export was collected without the pressure journal."""


@dataclass(frozen=True)
class Bar:
    index: int
    first_open: int
    opening: int
    closing: int
    ohlc: tuple[Decimal, ...]
    volume: Decimal | None
    volume_type: str

    @classmethod
    def parse(cls, record, timeframe):
        require(record["symbol"] == rc1.SYMBOL and record["timeframe"] == str(timeframe),
                "PR_CTX: unexpected symbol or timeframe")
        index = record["index"]
        first, opening, closing = (rc1.epoch(record[key], key) for key in ("first_open_ms", "open_ms", "close_ms"))
        require(type(index) is int and index >= 0 and first <= opening < closing <= opening + timeframe * 60_000,
                "PR_CTX: invalid index or interval")
        o, high, low, close = (rc1.json_number(record[key], key) for key in ("open", "high", "low", "close"))
        require(0 < low <= min(o, close) <= max(o, close) <= high, "PR_CTX: invalid OHLC")
        raw_volume = record["volume"]
        volume = None if raw_volume is None else rc1.json_number(raw_volume, "volume")
        return cls(index, first, opening, closing, (o, high, low, close), volume,
                   rc1.nonempty(record["volume_type"], "volume type"))


def reference(window, lookback, weighted):
    """Independently calculate close location, choosing weights for the entire window."""
    if len(window) < lookback:
        return "WARMUP", None, None, "INSUFFICIENT_HISTORY"
    window = window[-lookback:]
    mode, reason = "OHLC", "OHLC_SELECTED"
    weights = [Decimal(1)] * lookback
    if weighted:
        mode = "FALLBACK"
        if any(bar.volume_type != "tick" for bar in window):
            reason = "NON_TICK_SOURCE"
        elif any(bar.volume is None or bar.volume < 0 for bar in window):
            reason = "INVALID_TICK_VOLUME"
        elif not sum(bar.volume for bar in window):
            reason = "ZERO_TICK_VOLUME"
        else:
            mode, reason, weights = "TICK", "TICK_WEIGHTED", [bar.volume for bar in window]
    locations = [Decimal("0.5") if high == low else (close - low) / (high - low)
                 for _, high, low, close in (bar.ohlc for bar in window)]
    buy = 100 * sum(location * weight for location, weight in zip(locations, weights)) / sum(weights)
    return mode, buy, 100 - buy, reason


@dataclass(frozen=True)
class PressureExport:
    path: Path
    timeframe: int
    bars: tuple[Bar, ...]
    rows: tuple[dict, ...]
    complete: bool
    sha256: str


def read_pressure(path, timeframe):
    path = Path(path)
    bars, samples = [], []
    end = None
    with path.open(encoding="utf-8-sig", newline="") as raw:
        reader = csv.DictReader(raw, strict=True)
        require(reader.fieldnames == ["Date", "Message"], "expected TradingView Pine Logs Date,Message CSV")
        for row in reader:
            require(set(row) == {"Date", "Message"} and row["Date"] and row["Message"] is not None,
                    "invalid Date,Message CSV row")
            for line in row["Message"].splitlines():
                tag, _, payload = line.partition("|")
                if tag not in {"PR_CTX", "PR_SAMPLE", "PR_END"}:
                    continue
                require(end is None, f"{tag}: record follows PR_END")
                record = rc1._json_dict(payload)
                if tag == "PR_CTX":
                    bar = Bar.parse(record, timeframe)
                    if bars:
                        prev = bars[-1]
                        require(bar.index == prev.index + 1 and bar.opening >= prev.closing
                                and bar.first_open == prev.first_open, "PR_CTX: missing or unordered chart context")
                    bars.append(bar)
                elif tag == "PR_SAMPLE":
                    samples.append(record)
                else:
                    end = record
    if not (bars or samples or end):
        raise MissingPressureEvidence("export has no pressure journal records")
    require(bars, "pressure journal has no PR_CTX")
    if end is not None:
        require(end == {"symbol": rc1.SYMBOL, "timeframe": str(timeframe),
                        "index": bars[-1].index, "close_ms": bars[-1].closing}, "PR_END: wrong terminal bar")
    return PressureExport(path, timeframe, tuple(bars), tuple(samples), end is not None,
                          rc1.digest(path.read_bytes()))


def sample_context(record, bars, timeframe):
    index = record["index"]
    require(type(index) is int and index in bars and record["chart"] == str(timeframe),
            "PR_SAMPLE: sample has no chart context")
    bar = bars[index]
    require(record["chart_open_ms"] == bar.opening and record["decision_ms"] == bar.closing,
            "PR_SAMPLE: decision does not match the confirmed chart close")
    frame = record["frame"]
    require(frame in {str(value) for value in (*COMPARISON_FRAMES, timeframe)},
            "PR_SAMPLE: unexpected source timeframe")
    frame = int(frame)
    relation = "HTF" if frame > timeframe else "LTF" if frame < timeframe else "LOCAL"
    require(record["relation"] == relation, "PR_SAMPLE: incorrect timeframe relation")
    require(type(record["weighted"]) is bool and type(record["comparison"]) is bool
            and type(record["lookback"]) is int and 1 <= record["lookback"] <= 100,
            "PR_SAMPLE: invalid weighting or lookback")
    mode, reason, source = record["mode"], record["reason"], record["source_ms"]
    require(mode in MODES and reason in MODE_REASONS[mode] and (source is None) == (reason == "SOURCE_UNAVAILABLE"),
            "PR_SAMPLE: invalid source, mode or reason")
    require((relation == "LOCAL" and source is not None) or (relation != "LOCAL" and record["comparison"]),
            "PR_SAMPLE: local context missing or comparison disabled")
    if mode in UNAVAILABLE:
        require(record["buy"] is None and record["sell"] is None, "PR_SAMPLE: unavailable result has percentages")
    else:
        buy, sell = (rc1.json_number(record[key], key) for key in ("buy", "sell"))
        require(0 <= buy <= 100 and 0 <= sell <= 100 and abs(buy + sell - 100) <= TOLERANCE,
                "PR_SAMPLE: percentages must sum to 100")
        require((mode in {"TICK", "FALLBACK"}) == record["weighted"], "PR_SAMPLE: inconsistent weighting mode")
    if source is not None:
        rc1.epoch(source, "pressure source close")
        if relation == "LOCAL":
            require(source == bar.closing, "PR_SAMPLE: local source was not the current closed bar")
        elif relation == "HTF":
            require(source <= bar.opening, "PR_SAMPLE: HTF source was unavailable at chart open")
        else:
            require(bar.opening < source <= bar.closing, "PR_SAMPLE: lower source falls outside the confirmed chart bar")
    return bar, frame, relation


def check_sample(record, bar, relation, source_bars, closes):
    boundary = bar.opening if relation == "HTF" else bar.closing
    position = bisect_right(closes, boundary) - 1
    source = record["source_ms"]
    left_covered = bool(source_bars and source_bars[0].closing <= boundary)
    right_covered = bool(source_bars and source_bars[-1].closing >= boundary)
    latest = closes[position] if position >= 0 else None
    if relation == "LTF" and latest is not None and latest <= bar.opening:
        latest = None
    require(source is None or latest is None or source >= latest,
            "PR_SAMPLE: source is not the latest independently observed confirmed bar")
    if left_covered and right_covered:
        if source is None:
            return "SOURCE_NOT_MAPPED" if latest is not None else "UNAVAILABLE_VERIFIED"
        require(source == latest, "PR_SAMPLE: source is not the latest independently observed confirmed bar")
    if source is None or position < 0 or closes[position] != source:
        return "SOURCE_NOT_OBSERVED"
    length = record["lookback"]
    window = source_bars[max(0, position + 1 - length):position + 1]
    if len(window) < length and (relation != "LOCAL" or window[0].index > 0):
        return "SOURCE_WARMUP_NOT_OBSERVED"
    mode, buy, sell, reason = reference(window, length, record["weighted"])
    require((record["mode"], record["reason"]) == (mode, reason),
            f"PR_SAMPLE: independently calculated mode and reason are {mode}/{reason}")
    if buy is not None:
        require(abs(record["buy"] - buy) <= TOLERANCE and abs(record["sell"] - sell) <= TOLERANCE,
                "PR_SAMPLE: percentages differ from independent OHLCV calculation")
    if buy is None:
        return "WARMUP_VERIFIED"
    return "CALCULATION_VERIFIED" if left_covered and right_covered else "CALCULATION_VERIFIED_SOURCE_EDGE"


def pressure_checks(run, references, *, expected_comparison=False, expected_weighted=False, rc1_context=None):
    bars = {bar.index: bar for bar in run.bars}
    if rc1_context is not None:
        reference_bars = {bar.index: bar for bar in rc1_context}
        for bar in run.bars:
            ctx = reference_bars.get(bar.index)
            require(ctx is not None and (bar.first_open, bar.opening, bar.closing, bar.ohlc) ==
                    (ctx.first_open, ctx.opening, ctx.closing, ctx.ohlc), "PR_CTX: differs from the native RC2 context")
    source_by_frame = {frame: (source.bars, [bar.closing for bar in source.bars])
                       for frame, source in {**references, run.timeframe: run}.items()}
    counts, modes, reasons = defaultdict(Counter), defaultdict(Counter), defaultdict(Counter)
    settings, seen = set(), set()
    last_index = -1
    chart_frames = {run.timeframe, *COMPARISON_FRAMES} if expected_comparison else {run.timeframe}
    for row in run.rows:
        bar, frame, relation = sample_context(row, bars, run.timeframe)
        require(row["comparison"] is expected_comparison and frame in chart_frames,
                "PR_SAMPLE: export did not exercise the expected comparison setting")
        require(row["weighted"] is expected_weighted, "PR_SAMPLE: export did not exercise the expected weighting setting")
        key = bar.index, frame
        require(key not in seen, "PR_SAMPLE: duplicate sample on a confirmed bar")
        require(bar.index >= last_index, "PR_SAMPLE: sample order moved backward in time")
        last_index = bar.index
        seen.add(key)
        settings.add(row["lookback"])
        counts[frame][check_sample(row, bar, relation, *source_by_frame.get(frame, ((), ())))] += 1
        modes[frame][row["mode"]] += 1
        reasons[frame][row["reason"]] += 1
    require(len(settings) <= 1, "PR_SAMPLE: settings changed within the same export")
    if not run.rows:
        return {"status": "MISSING_SAMPLES", "file": run.path.name, "sha256": run.sha256}
    require(seen == {(bar.index, frame) for bar in run.bars for frame in chart_frames},
            "PR_SAMPLE: missing per-bar observations")
    verified = {frame for frame, count in counts.items()
                if count["CALCULATION_VERIFIED"] > 0 and not count["SOURCE_NOT_MAPPED"]}
    return {
        "status": "VERIFIED" if run.complete and verified == chart_frames else "PARTIAL",
        "file": run.path.name, "sha256": run.sha256, "bars": len(bars), "end_marker_present": run.complete,
        "settings": {"lookback": next(iter(settings)), "weighted": expected_weighted, "comparison": expected_comparison},
        "frame_counts": {str(frame): {"verification": dict(counts[frame]), "modes": dict(modes[frame]),
                                      "reasons": dict(reasons[frame])}
                         for frame in sorted(chart_frames)},
    }


def load_native(path, timeframe, role, mode):
    try:
        run = rc1.read_export(path, timeframe, role)
        journal = rc2.extras(path)
        qa = rc2.indicator_checks(run, journal, timeframe) if role == "indicator" else (
            rc2.native_checks(path, run, journal, timeframe, mode)
        )
        return (run, journal), {"status": "PARSED", **run.meta, "rc2": qa}
    except FileNotFoundError:
        return None, {"status": "MISSING_EVIDENCE", "file": path.name}
    except EVIDENCE_ERRORS as exc:
        return None, {"status": "INVALID_EXPORT", "file": path.name, "reason": str(exc)}


def scoped_journal(run, journal, role, start, end):
    if role == "indicator":
        return {"RC2_ALERT": [row for row in journal["RC2_ALERT"] if start < row["events"][0]["confirmed_ms"] <= end]}
    exit_ids = {row["event_id"] for row in run.native if start < row["known_time"] <= end}
    return {
        "RC2_NATIVE_PART": [row for row in journal["RC2_NATIVE_PART"] if row["source_id"] in exit_ids],
        "RC2_NATIVE_ALERT": [row for row in journal["RC2_NATIVE_ALERT"] if start < row["events"][0]["known_ms"] <= end],
    }


def native_parity(baseline, feature, role):
    if baseline is None or feature is None:
        return {"status": "MISSING_EVIDENCE"}
    left, left_journal = baseline
    right, right_journal = feature
    streams = ("core", "ready", "alerts") if role == "indicator" else ("core", "ready", "native")
    comparison = rc1.compare_exports(left, right, streams)
    if comparison["status"] != "MATCH":
        if comparison.get("stream") == "context":
            overlap = comparison["shared_window_diagnostic"] = rc2.shared(left, right, streams)
            comparison["status"] = "NONIDENTICAL_CONTEXT"
            if overlap["status"] in {"MATCH", "MATCHED_EXCERPT"}:
                bounds = overlap["shared_first_open_ms"], overlap["shared_last_close_ms"]
                shared_left = scoped_journal(left, left_journal, role, *bounds)
                shared_right = scoped_journal(right, right_journal, role, *bounds)
                for tag, rows in shared_left.items():
                    if rows != shared_right[tag]:
                        overlap.update(status="MISMATCH", stream=tag, left_rows=len(rows), right_rows=len(shared_right[tag]))
                        break
                    overlap["rows"][tag] = len(rows)
                if role == "audit":
                    overlap["strategy_summary"] = "INCOMPARABLE_LOADED_CONTEXT"
        return comparison
    tags = ("RC2_ALERT", "RC2_EARLY", "RC2_FILTER") if role == "indicator" else (
        "RC2_NATIVE_PART", "RC2_NATIVE_ALERT", "RC2_AUDIT_SUMMARY"
    )
    for tag in tags:
        for index, (x, y) in enumerate(zip_longest(left_journal[tag], right_journal[tag]), 1):
            if x != y:
                return {"status": "MISMATCH", "stream": tag, "row": index, "left": x, "right": y}
        comparison["rows"][tag] = len(left_journal[tag])
    if role == "indicator":
        if not (left.alerts and left.ready and left_journal["RC2_ALERT"]):
            comparison["status"] = "NO_SIGNAL_OR_ALERT_EXAMPLES"
        return comparison
    if not left_journal["RC2_AUDIT_SUMMARY"]:
        comparison["status"] = "MISSING_NATIVE_STRATEGY_SUMMARY"
        return comparison
    summary, = left_journal["RC2_AUDIT_SUMMARY"]
    pnl = sum(row["cash_pnl"] for row in left.native if row["event"].removeprefix("NATIVE_EXIT_").isdigit())
    comparison["native_strategy_summary"] = summary
    comparison["exported_partial_cash_pnl_usd"] = pnl
    comparison["native_profit_factor"] = summary["grossprofit"] / abs(summary["grossloss"]) if summary["grossloss"] else None
    if not left_journal["RC2_NATIVE_PART"]:
        comparison["status"] = "NO_REALIZED_NATIVE_EXAMPLES"
    return comparison


def pressure_source(path, timeframe):
    try:
        return read_pressure(path, timeframe), None
    except FileNotFoundError:
        return None, {"status": "MISSING_EVIDENCE", "file": path.name}
    except MissingPressureEvidence as exc:
        return None, {"status": "MISSING_EVIDENCE", "file": path.name, "reason": str(exc)}
    except EVIDENCE_ERRORS as exc:
        return None, {"status": "INVALID_EXPORT", "file": path.name, "reason": str(exc)}


def pressure_repeat(original, repeat):
    if not original or not repeat:
        return {"status": "MISSING_EVIDENCE"}
    if not (original.complete and repeat.complete and original.rows and repeat.rows):
        return {"status": "INCOMPLETE_WINDOW"}
    same_origin = original.bars == repeat.bars
    if not same_origin:
        # A rolling chart can move its warmup origin without changing the exported
        # bars. Compare a complete identical input window without run-local indexes.
        normalize_bars = lambda bars: tuple(replace(bar, index=0, first_open=0) for bar in bars)
        if normalize_bars(original.bars) != normalize_bars(repeat.bars):
            return {"status": "NONIDENTICAL_CONTEXT"}
        original_rows = ({k: v for k, v in row.items() if k != "index"} for row in original.rows)
        repeat_rows = ({k: v for k, v in row.items() if k != "index"} for row in repeat.rows)
    else:
        original_rows, repeat_rows = original.rows, repeat.rows
    for index, (left, right) in enumerate(zip_longest(original_rows, repeat_rows), 1):
        if left != right:
            return {"status": "MISMATCH", "row": index, "original": left, "repeat": right}
    result = {"status": "MATCH" if same_origin else "MATCH_SHIFTED_WARMUP", "samples": len(original.rows),
              "original_sha256": original.sha256, "repeat_sha256": repeat.sha256}
    if not same_origin:
        result.update(original_first_open_ms=original.bars[0].first_open,
                      repeat_first_open_ms=repeat.bars[0].first_open)
    return result


def report(native_dir, audit_mode=rc2.MODES["fixed"]):
    native_dir = Path(native_dir)
    frames = {}
    default_runs = {}
    for timeframe in rc1.FRAMES:
        default_runs[timeframe] = pressure_source(native_dir / f"feature_indicator_M{timeframe}.csv", timeframe)
    hourly, hourly_issue = pressure_source(native_dir / "pressure_context_H1.csv", 60)
    references = {frame: run for frame, (run, _) in default_runs.items() if run}
    if hourly:
        references[60] = hourly

    for timeframe in rc1.FRAMES:
        frame = frames[f"M{timeframe}"] = {"exports": {}, "parity": {}, "pressure": {}}
        rc_context = None
        for role in ("indicator", "audit"):
            pair = []
            for name in ("baseline", "feature"):
                key = f"{name}_{role}"
                run, metadata = load_native(native_dir / f"{key}_M{timeframe}.csv", timeframe, role, audit_mode)
                pair.append(run)
                frame["exports"][key] = metadata
                if name == "feature" and role == "indicator" and run:
                    rc_context = run[0].context
            frame["parity"][role] = native_parity(*pair, role)
        runs = {}
        for kind in ("default", "compare", "tick"):
            source, issue = default_runs[timeframe] if kind == "default" else (
                pressure_source(native_dir / f"pressure_{kind}_M{timeframe}.csv", timeframe)
            )
            feature = default_runs[timeframe][0]
            if kind == "compare" and issue and issue["status"] == "MISSING_EVIDENCE" and (
                feature and feature.rows and feature.rows[0].get("comparison") is True
            ):
                source, issue = feature, None
            if issue:
                frame["pressure"][kind] = issue
                continue
            try:
                comparison = kind == "compare" or bool(source.rows and source.rows[0].get("comparison"))
                frame["pressure"][kind] = pressure_checks(
                    source, references, expected_comparison=comparison, expected_weighted=kind == "tick",
                    rc1_context=rc_context if source is feature else None,
                )
                runs[kind] = source
            except EVIDENCE_ERRORS as exc:
                frame["pressure"][kind] = {"status": "INVALID_EXPORT", "file": source.path.name, "reason": str(exc)}
        repeat, issue = pressure_source(native_dir / f"pressure_repeat_M{timeframe}.csv", timeframe)
        frame["repeat"] = issue or pressure_repeat(runs.get("default"), repeat)

    parity = [item["status"] for frame in frames.values() for item in frame["parity"].values()]
    pressure = [item["status"] for frame in frames.values() for item in frame["pressure"].values()]
    repeat = {frame["repeat"]["status"] for frame in frames.values()}
    invalid = any(item["status"] == "INVALID_EXPORT" for frame in frames.values() for item in frame["exports"].values())
    if invalid or "MISMATCH" in parity:
        status = "PARITY_FAILED"
    elif "INVALID_EXPORT" in pressure or repeat & {"MISMATCH", "INVALID_EXPORT"}:
        status = "PRESSURE_EVIDENCE_FAILED"
    elif all(item == "MATCH" for item in parity) and all(item == "VERIFIED" for item in pressure):
        status = "NATIVE_LOG_CHECKS_VERIFIED"
    else:
        status = "PARTIALLY_COMPLETE"
    return {
        "status": status, "baseline_source_commit": BASELINE, "audit_mode": audit_mode, "frames": frames,
        "independent_h1_context": hourly_issue or {"status": "PARSED", "file": hourly.path.name, "sha256": hourly.sha256},
        "scope": "Pine Logs verify exported history, source availability and emulator events. "
                 "Repeat compares separately supplied exports of the same bounded chart context. "
                 "Screenshots, alert delivery and intrabar display require separate observations.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-dir", required=True, type=Path,
                        help="directory with baseline/feature indicator/audit and optional pressure probe CSVs")
    parser.add_argument("--audit-mode", default=rc2.MODES["fixed"], choices=rc2.MODES.values())
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    evidence = report(args.native_dir, args.audit_mode)
    rendered = json.dumps(evidence, ensure_ascii=False, indent=2, default=str) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0 if evidence["status"] == "NATIVE_LOG_CHECKS_VERIFIED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
