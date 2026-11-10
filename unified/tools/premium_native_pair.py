"""Prepare and reconcile fresh matched TradingView Premium observations."""

import argparse
from collections import Counter
import csv
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory

from diagnostics.accounting.audit import audit, json_default, read_jsonl
from unified.tools.audit_premium_parity import GRID, compact, comparison
from unified.tools.audit_tv_observations import ALIGNMENT_FIELDS, validate_execution
from unified.tools.build_sniper_premium import BASELINE_SHA, OUTPUT, ROOT, sha, verify_source


SCHEMA = "qirvanta.premium.native_pair.v1"
FRAMES = ("5", "15", "30")
BUILDERS = {
    "baseline": "observe_tradingview_final.py",
    "premium": "make_premium_observer.py",
}
CUTOFF_LINE = re.compile(rb"^int dgCutoff = [^\r\n]+$", re.MULTILINE)


def write_report(path, report):
    with path.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, default=json_default, allow_nan=False)
        stream.write("\n")


def cutoff_epoch(value, timeframe):
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() != timedelta(0) or stamp.second or stamp.microsecond:
        raise ValueError("Use a UTC cutoff on an exact completed bar close, e.g. 2026-11-10T12:00:00Z")
    period = 30 if timeframe == "all" else int(timeframe)
    if stamp.minute % period:
        raise ValueError(f"Cutoff must align to {period}-minute bars")
    return int(stamp.timestamp()) * 1000


def observer(builder, source_sha, timeframe, cutoff):
    with TemporaryDirectory() as scratch:
        path = Path(scratch) / "observer.txt"
        subprocess.run(
            [sys.executable, "-X", "utf8", "-B", str(ROOT / "unified/tools" / builder),
             str(path), "--timeframe", timeframe],
            cwd=ROOT, check=True, capture_output=True,
        )
        data = path.read_bytes()
    if data.count(source_sha.encode("ascii")) != 1:
        raise ValueError(f"Unexpected source identity in {builder}")
    if len(CUTOFF_LINE.findall(data)) != 1:
        raise ValueError(f"Expected a single observer cutoff in {builder}")
    return CUTOFF_LINE.sub(f"int dgCutoff = {cutoff}".encode("ascii"), data, count=1)


def prepare(args):
    cutoff = cutoff_epoch(args.cutoff, args.timeframe)
    source_shas = {"baseline": BASELINE_SHA,
                   "premium": verify_source(OUTPUT.read_bytes())["source_sha256"]}
    buffers = {arm: observer(builder, source_shas[arm], args.timeframe, cutoff)
               for arm, builder in BUILDERS.items()}
    args.output_dir.mkdir()
    arms = {}
    for arm, data in buffers.items():
        name = f"{arm}-observer.txt"
        (args.output_dir / name).write_bytes(data)
        arms[arm] = {"observer_file": name, "observer_sha256": sha(data),
                     "source_sha256": source_shas[arm]}
    write_report(args.output_dir / "manifest.json", {
        "schema": SCHEMA, "cutoff_utc": args.cutoff, "cutoff_ms": cutoff,
        "timeframes": list(FRAMES if args.timeframe == "all" else (args.timeframe,)),
        "symbol": "OANDA:XAUUSD", "arms": arms,
        "required_manual_record": "Matching chart type, loaded history, Inputs and Properties. "
        "Capture each raw export and identify the compiled observer by SHA-256.",
        "scope": "Observer strips visualization. Compilation of the full Pine is separate.",
    })
    print(args.output_dir / "manifest.json")


def read_observed(paths, source_sha, cutoff, frames):
    runs = {}
    for path in paths:
        records, _ = read_jsonl(path, allow_pine_prefix=True, origin_override="REAL_TRADINGVIEW")
        data = audit(records)
        run = data["run"]
        prefix, mode, model = run["run_id"].split("|")
        tf = run["timeframe"]
        key = (mode, tf)
        if (prefix != "tv_final" or model != "Всички" or key not in GRID
                or tf not in frames or key in runs or run["symbol"] != "OANDA:XAUUSD"
                or run["source_sha256"] != source_sha):
            raise ValueError(f"Unexpected or repeated run registration: {path}")
        snapshots = [row for row in records if row["kind"] == "snapshot"]
        if (run["cutoff"] != cutoff or run["last_bar_open"] + int(tf) * 60_000 != cutoff
                or len(snapshots) != 1 or snapshots[0]["at_time"] != cutoff
                or run["bar_count"] <= 0 or snapshots[0]["at_bar"] != run["bar_count"] - 1):
            raise ValueError(f"Journal does not cover the registered completed cutoff: {path}")
        if data["status"] != "PASS_OBSERVED_JOURNAL" or not data["cash_reconciliation"]["matches"]:
            raise ValueError(f"Incomplete or unreconciled native journal: {path}")
        validate_execution(data)
        runs[key] = data, sha(path.read_bytes())
    return runs


def compare_observers(args):
    manifest = json.loads(args.manifest.read_bytes())
    if manifest["schema"] != SCHEMA or manifest["symbol"] != "OANDA:XAUUSD":
        raise ValueError("Unrecognized native pair manifest")
    frames = manifest["timeframes"]
    if frames != list(FRAMES) and (len(frames) != 1 or frames[0] not in FRAMES):
        raise ValueError("Unexpected manifest timeframe registration")
    cutoff = cutoff_epoch(manifest["cutoff_utc"], "all" if frames == list(FRAMES) else frames[0])
    if cutoff != manifest["cutoff_ms"] or manifest["arms"]["baseline"]["source_sha256"] != BASELINE_SHA:
        raise ValueError("Unexpected cutoff or baseline identity in manifest")
    runs = {}
    for arm, logs in (("baseline", args.baseline_logs), ("premium", args.candidate_logs)):
        registered = manifest["arms"][arm]
        name = f"{arm}-observer.txt"
        if registered["observer_file"] != name or sha((args.manifest.parent / name).read_bytes()) != registered["observer_sha256"]:
            raise ValueError(f"Observer bytes differ from manifest: {arm}")
        runs[arm] = read_observed(logs, registered["source_sha256"], cutoff, frames)
    baseline, premium = runs["baseline"], runs["premium"]
    expected = {(mode, tf) for mode, tf in GRID if tf in frames}
    if baseline.keys() != premium.keys():
        raise ValueError("Provide the same mode/timeframe cells for both arms")
    if args.require_complete_grid and baseline.keys() != expected:
        raise ValueError(f"Missing registered cells: {sorted(expected - baseline.keys())}")
    cells = []
    for key in sorted(baseline, key=lambda cell: (int(cell[1]), cell[0])):
        original, candidate = baseline[key], premium[key]
        cells.append({"mode": key[0], "timeframe": key[1], "baseline": compact(original),
                      "premium": compact(candidate), "differences": comparison(original[0], candidate[0])})
    aligned = {}
    for tf in frames:
        fingerprints = {
            tuple(data["run"][field] for field in ALIGNMENT_FIELDS)
            for arm in runs.values() for (mode, frame), (data, _) in arm.items()
            if frame == tf
        }
        if fingerprints:
            aligned[tf] = len(fingerprints) == 1
    matched = all(aligned.values()) and not any(cell["differences"] for cell in cells)
    report = {
        "schema": SCHEMA, "status": "MATCHED_OBSERVER_JOURNALS" if matched else "OBSERVER_MISMATCH",
        "manifest_sha256": sha(args.manifest.read_bytes()), "registration": manifest,
        "complete_registered_grid": baseline.keys() == expected,
        "missing_registered_cells": sorted(expected - baseline.keys()),
        "observed_rows_present": any(cell["baseline"]["parts"] > 0 for cell in cells),
        "intra_timeframe_fingerprints_match": aligned, "cells": cells,
        "qualification": "Observer-only parity of supplied logs. Full-source runtime, visualization, "
        "provenance and matching GUI settings require separate evidence.",
    }
    write_report(args.output, report)
    print(report["status"])
    return int(not matched)


def trade_list(path):
    raw = path.read_bytes()
    reader = csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True)
    header = next(reader, ())
    columns = [name.strip().casefold() for name in header]
    if (not set(columns).intersection({"trade #", "trade number", "trade no."})
            or "type" not in columns or not any("date" in name or "time" in name for name in columns)
            or not any("price" in name for name in columns) or not any("size" in name for name in columns)):
        raise ValueError(f"Expected an English TradingView List of Trades CSV: {path}")
    rows = Counter()
    for row in reader:
        if not row:
            continue
        if len(row) != len(header):
            raise ValueError(f"Malformed trade row in {path}")
        rows[tuple(row)] += 1
    return header, rows, sha(raw)


def compare_trade_lists(args):
    header_a, a, sha_a = trade_list(args.baseline)
    header_b, b, sha_b = trade_list(args.candidate)
    same = header_a == header_b and a == b
    status = ("MATCHED_EXPORTED_TRADE_LIST_ROWS" if a else "EMPTY_EXPORTED_TRADE_LISTS") if same else "TRADE_LIST_MISMATCH"
    write_report(args.output, {
        "schema": SCHEMA, "status": status, "mode": args.mode, "timeframe": args.timeframe,
        "baseline_csv_sha256": sha_a, "premium_csv_sha256": sha_b,
        "baseline_rows": sum(a.values()), "premium_rows": sum(b.values()), "columns_match": header_a == header_b,
        "baseline_only_rows": sum((a - b).values()), "premium_only_rows": sum((b - a).values()),
        "qualification": "Exact CSV row multiset only. The mode and timeframe are supplied by the caller. "
        "Verify dataset, settings, row coverage, campaign IDs, submitted geometry and equity drawdown separately.",
    })
    print(status)
    return int(not same or not a)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    setup = actions.add_parser("prepare", help="Create two temporary diagnostic buffers and a manifest")
    setup.add_argument("--cutoff", required=True, help="Confirmed UTC bar close, ISO 8601")
    setup.add_argument("--timeframe", choices=(*FRAMES, "all"), default="all")
    setup.add_argument("--output-dir", required=True, type=Path, help="New directory in an existing parent")
    setup.set_defaults(run=prepare)
    pair = actions.add_parser("compare", help="Compare fresh Pine Logs CSV or JSONL exports")
    pair.add_argument("--manifest", required=True, type=Path)
    pair.add_argument("--baseline-logs", required=True, nargs="+", type=Path)
    pair.add_argument("--candidate-logs", required=True, nargs="+", type=Path)
    pair.add_argument("--require-complete-grid", action="store_true")
    pair.add_argument("--output", required=True, type=Path)
    pair.set_defaults(run=compare_observers)
    trades = actions.add_parser("compare-trade-list", help="Compare one pair of Strategy Report trade list exports")
    trades.add_argument("--baseline", required=True, type=Path)
    trades.add_argument("--candidate", required=True, type=Path)
    trades.add_argument("--mode", required=True, choices=("S0", "S5"))
    trades.add_argument("--timeframe", required=True, choices=FRAMES)
    trades.add_argument("--output", required=True, type=Path)
    trades.set_defaults(run=compare_trade_lists)
    args = parser.parse_args()
    try:
        return args.run(args)
    except subprocess.CalledProcessError as exc:
        parser.exit(2, (exc.stderr or b"Observer builder failed").decode("utf-8", errors="replace") + "\n")
    except (OSError, ValueError, KeyError, csv.Error) as exc:
        parser.exit(2, f"{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
