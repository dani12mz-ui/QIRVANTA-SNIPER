#!/usr/bin/env python3
"""Archive actual Pine Logs OHLC/HTF exports without filling or reconstructing bars."""
from __future__ import annotations

import argparse
from collections import Counter
import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COLUMNS = ["time", "time_close", "open", "high", "low", "close", "volume", "htfClose", "htfEMA50", "htfEMA200", "htfEMA50prev", "priorDayHigh", "priorDayLow"]
BASELINE_SHA = "c6bfe1af0604c447eddaed1db5195bfe2c07d3826e7fafc70113235e9f25f1a5"
INTERVAL_MS = 15 * 60 * 1000


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc(milliseconds: int) -> str:
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def reject_nonfinite(value: str):
    raise ValueError(f"Non-finite JSON token {value}")


def read_json(line: str):
    return json.loads(line, parse_float=Decimal, parse_constant=reject_nonfinite)


def number(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError(f"Expected number/null, got {value!r}")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError("Non-finite decimal")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def json_ready(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [json_ready(v) for v in value]
    if isinstance(value, dict):
        return {k: json_ready(v) for k, v in value.items()}
    return value


def parse_export(raw: bytes) -> dict:
    """Require one complete bar block and one original run record in the actual CSV."""
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig"), newline=""))
    if reader.fieldnames != ["Date", "Message"]:
        raise ValueError("Expected actual TradingView CSV header Date,Message")
    state = "before"
    metadata = None
    rows = []
    run_records = []
    snapshots = []
    log_dates = set()
    messages = 0
    for log in reader:
        if None in log or log.get("Message") is None or log.get("Date") is None:
            raise ValueError("Malformed Date/Message CSV row")
        messages += 1
        log_dates.add(log["Date"])
        for text in log["Message"].splitlines():
            line = text.strip()
            if not line:
                continue
            if line == "QIRVANTA_BARS_BEGIN":
                if state != "before":
                    raise ValueError("Duplicate/nested bar block")
                state = "metadata"
                continue
            if line == "QIRVANTA_BARS_END":
                if state != "rows":
                    raise ValueError("Bar end marker without complete metadata")
                state = "after"
                continue
            if state == "metadata":
                metadata = read_json(line)
                if not isinstance(metadata, dict) or metadata.get("columns") != COLUMNS:
                    raise ValueError("Unexpected OHLC/HTF export columns")
                state = "rows"
                continue
            if state == "rows":
                row = read_json(line)
                if not isinstance(row, list) or len(row) != len(COLUMNS):
                    raise ValueError("Incorrect bar row width")
                for v in row:
                    number(v)
                if any(isinstance(row[i], bool) or not isinstance(row[i], int) for i in (0, 1)):
                    raise ValueError("Timestamps must be integer milliseconds")
                if any(v is None for v in row[2:6]):
                    raise ValueError("OHLC cannot be null")
                if row[1] <= row[0] or row[3] < max(row[2], row[4], row[5]) or row[4] > min(row[2], row[3], row[5]):
                    raise ValueError("Invalid bar interval or OHLC geometry")
                rows.append(row)
                continue
            if line.startswith("{"):
                event = read_json(line)
                if isinstance(event, dict) and event.get("schema") == "qirvanta.native.v1":
                    if event.get("kind") == "run":
                        run_records.append(event)
                    elif event.get("kind") == "snapshot":
                        snapshots.append(event)
    if state != "after" or not rows:
        raise ValueError("Missing/truncated QIRVANTA_BARS block or no bar rows")
    if len(run_records) != 1 or len(snapshots) != 1:
        raise ValueError("Need exactly one run and one cutoff snapshot from the same export")
    if run_records[0].get("run_id") != snapshots[0].get("run_id"):
        raise ValueError("Run/snapshot identity differs")
    return {"metadata": metadata, "rows": rows, "run": run_records[0], "snapshot": snapshots[0],
            "csv_messages": messages, "log_message_dates": sorted(log_dates)}


def gap_kind(start: int, end: int) -> str:
    day = datetime.fromtimestamp(start / 1000, timezone.utc).date()
    last = datetime.fromtimestamp((end - 1) / 1000, timezone.utc).date()
    weekend = False
    while day <= last:
        weekend |= day.weekday() >= 5
        day += timedelta(days=1)
    if weekend and end - start >= 24 * 60 * 60 * 1000:
        return "WEEKEND_SPANNING_BREAK_CANDIDATE"
    if end - start <= 4 * 60 * 60 * 1000:
        return "SHORT_SESSION_BREAK_CANDIDATE"
    return "UNCLASSIFIED_GAP"


def inspect(parsed: dict, expected_source_sha256: str = BASELINE_SHA) -> dict:
    rows = parsed["rows"]
    run = parsed["run"]
    duplicates, overlaps, backwards, gaps = [], [], [], []
    seen = {}
    for i, row in enumerate(rows):
        opening, closing = row[:2]
        if opening in seen:
            first = seen[opening]
            duplicates.append({"time": opening, "utc": utc(opening), "first_row": first, "repeated_row": i,
                               "identical_all_columns": row == rows[first]})
        else:
            seen[opening] = i
        if not i:
            continue
        previous = rows[i - 1]
        if opening < previous[0]:
            backwards.append({"previous_row": i - 1, "row": i})
        if opening < previous[1]:
            overlaps.append({"previous_row": i - 1, "row": i})
        elif opening > previous[1]:
            interval = opening - previous[1]
            gaps.append({"previous_row": i - 1, "next_row": i, "previous_close_time": previous[1], "next_open_time": opening,
                         "from_utc": utc(previous[1]), "to_utc": utc(opening), "duration_seconds": interval // 1000,
                         "m15_slots_without_observed_bars": interval // INTERVAL_MS,
                         "aligned_to_m15": interval % INTERVAL_MS == 0, "classification": gap_kind(previous[1], opening)})
    errors = []
    checks = {"symbol": run.get("symbol") == "OANDA:XAUUSD", "timeframe": str(run.get("timeframe")) == "15",
              "expected_strategy_source_hash": run.get("source_sha256") == expected_source_sha256,
              "export_count_matches_observed_counter": len(rows) == run.get("bar_count"),
              "first_open_matches_counter": rows[0][0] == run.get("first_bar_time"),
              "last_open_matches_counter": rows[-1][0] == run.get("last_bar_open"),
              "last_close_matches_cutoff": rows[-1][1] == run.get("cutoff"),
              "snapshot_matches_cutoff": parsed["snapshot"].get("at_time") == run.get("cutoff"),
              "no_bars_after_cutoff": all(row[1] <= run.get("cutoff", -1) for row in rows),
              "all_bars_m15_duration": all(row[1] - row[0] == INTERVAL_MS for row in rows),
              "no_duplicate_timestamps": not duplicates, "chronological_order": not backwards, "no_overlaps": not overlaps}
    errors.extend(name for name, passed in checks.items() if not passed)
    return {"status": "PASS_OBSERVED_SERIALIZED_EXPORT" if not errors else "INVALID_EXPORT",
            "validation_checks": checks, "failed_checks": errors, "bar_count": len(rows), "unique_open_timestamps": len(seen),
            "first_open_time": rows[0][0], "first_open_utc": utc(rows[0][0]),
            "last_open_time": rows[-1][0], "last_open_utc": utc(rows[-1][0]),
            "last_close_time": rows[-1][1], "last_close_utc": utc(rows[-1][1]),
            "null_counts": {column: sum(row[i] is None for row in rows) for i, column in enumerate(COLUMNS)},
            "duplicate_timestamps": duplicates, "backwards_rows": backwards, "overlaps": overlaps,
            "gaps": gaps, "gap_count": len(gaps), "gap_classification_counts": dict(Counter(row["classification"] for row in gaps)),
            "gap_semantics": "Observed time discontinuities, not automatically missing data. Candidate session/weekend classification is a UTC-duration heuristic; provider trading calendar and holidays not independently verified. No bars inserted."}


def canonical_csv(rows: list) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerow(COLUMNS)
    for row in rows:
        writer.writerow(["" if v is None else number(v) for v in row])
    return stream.getvalue().encode("utf-8")


def canonical_json(rows: list) -> bytes:
    head = '{"schema":"qirvanta.observed-bars.v1","columns":' + json.dumps(COLUMNS, separators=(",", ":")) + ',"rows":[\n'
    body = ",\n".join("[" + ",".join(number(v) for v in row) + "]" for row in rows)
    return (head + body + "\n]}\n").encode("utf-8")


def archive(source: Path, dest: Path, expected_source_sha256: str = BASELINE_SHA) -> dict:
    if dest.exists():
        raise ValueError("Refusing to overwrite an existing dataset archive")
    raw = source.read_bytes()
    parsed = parse_export(raw)
    report = inspect(parsed, expected_source_sha256)
    csv_bytes, json_bytes = canonical_csv(parsed["rows"]), canonical_json(parsed["rows"])
    manifest = {"created_at_utc": datetime.now(timezone.utc).isoformat(), "schema": "qirvanta.dataset-manifest.v1",
                "scope": "Actual observation-only diagnostic rerun export; not proof of exact original earlier TradingView dataset",
                "source_log_csv": source.relative_to(ROOT).as_posix() if source.is_relative_to(ROOT) else str(source),
                "source_log_sha256": sha(raw), "source_log_bytes": len(raw), "source_metadata": parsed["metadata"],
                "expected_strategy_source_sha256": expected_source_sha256,
                "run_metadata": parsed["run"], "cutoff_snapshot": parsed["snapshot"],
                "csv_log_messages": parsed["csv_messages"], "log_message_dates": parsed["log_message_dates"],
                "timestamp_semantics": "Date on Pine log messages is the logging bar, not each exported bar. Data timestamps come only from the row time/time_close columns.",
                "canonical_csv": {"file": "bars.csv", "sha256": sha(csv_bytes), "bytes": len(csv_bytes)},
                "canonical_json": {"file": "bars.json", "sha256": sha(json_bytes), "bytes": len(json_bytes)},
                "precision": "Pine f_num serializes prices/features to at most 8 decimal places. Decimal strings are preserved numerically; backend full precision is not claimed.",
                "checksum_scope": "checksum1/checksum2 are non-cryptographic Pine fingerprints. SHA-256 here authenticates exported serialized files only.",
                "no_reconstruction": "Source row order and all rows preserved. No filling gaps, merging providers, deduplication, or synthetic journal.",
                **report}
    dest.mkdir(parents=True)
    (dest / "bars.csv").write_bytes(csv_bytes)
    (dest / "bars.json").write_bytes(json_bytes)
    (dest / "dataset_manifest.json").write_text(json.dumps(json_ready(manifest), ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return json_ready(manifest)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--expected-source-sha256", default=BASELINE_SHA)
    args = parser.parse_args()
    if len(args.expected_source_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in args.expected_source_sha256):
        raise ValueError("Expected source SHA-256 must be 64 lowercase hexadecimal characters")
    result = archive(args.source.resolve(), args.destination.resolve(), args.expected_source_sha256)
    print(json.dumps({key: result[key] for key in ("status", "bar_count", "first_open_utc", "last_close_utc", "gap_count", "gap_classification_counts", "canonical_csv")}, ensure_ascii=True))
    return 0 if result["status"] == "PASS_OBSERVED_SERIALIZED_EXPORT" else 1


if __name__ == "__main__":
    raise SystemExit(main())
