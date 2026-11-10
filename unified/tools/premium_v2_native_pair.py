"""Build temporary V1/V2 observers and compare authentic TradingView Pine Logs."""

import argparse
from pathlib import Path

from audit_premium_parity import GRID, compact, comparison
from audit_tv_observations import ALIGNMENT_FIELDS
from build_sniper_premium_v2 import OUTPUT, PIN, PIN_SHA, RANGE_V1, RANGE_V2, sha, verify
from observe_tradingview_final import observed_buffer
from premium_native_pair import CUTOFF_LINE, FRAMES, cutoff_epoch, read_observed, write_report


def prepare(args, cutoff, source):
    args.output.mkdir()
    for arm, data in (("v1", PIN.read_bytes()), ("v2", source)):
        observer = observed_buffer(data, None, candidate=True)
        if len(CUTOFF_LINE.findall(observer)) != 1 or observer.count(sha(data).encode()) != 1:
            raise ValueError(f"Unexpected {arm} observer registration")
        observer = CUTOFF_LINE.sub(f"int dgCutoff = {cutoff}".encode(), observer, count=1)
        if arm == "v1":
            if observer.count(RANGE_V1) != 1:
                raise ValueError("Cannot align V1 diagnostic calculation range with V2")
            observer = observer.replace(RANGE_V1, RANGE_V2, 1)
        path = args.output / f"{arm}-observer.txt"
        path.write_bytes(observer)
        print(f"{path} sha256={sha(observer)} source_sha256={sha(data)}")


def compare(args, cutoff, source):
    original = read_observed(args.v1, PIN_SHA, cutoff, FRAMES)
    candidate = read_observed(args.v2, sha(source), cutoff, FRAMES)
    cells = []
    for mode, tf in GRID:
        key = mode, tf
        if key not in original or key not in candidate:
            cells.append({"mode": mode, "timeframe": tf, "status": "MISSING"})
            continue
        a, b = original[key], candidate[key]
        same_bars = all(a[0]["run"][f] == b[0]["run"][f] for f in ALIGNMENT_FIELDS)
        diff = comparison(a[0], b[0])
        cells.append({"mode": mode, "timeframe": tf,
                      "status": "DATASET_MISMATCH" if not same_bars else "MISMATCH" if diff else "MATCHED",
                      "v1": compact(a), "v2": compact(b), "differences": diff})
    status = "MATCHED_OBSERVER_JOURNALS" if all(cell["status"] == "MATCHED" for cell in cells) else "NOT_VERIFIED"
    write_report(args.output, {"status": status, "cutoff_utc": args.cutoff, "cells": cells,
                               "qualification": "Native diagnostics use the byte-verified strategy engine without the renderer. "
                               "The V1 observer has V2's matching default calculation range. Verify actual loaded bars via "
                               "fingerprints. The full script's native compilation and chart are tested separately."})
    print(status)
    return int(status == "NOT_VERIFIED")


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    sub = cli.add_subparsers(dest="command", required=True)
    for name in ("prepare", "compare"):
        p = sub.add_parser(name)
        p.add_argument("--cutoff", required=True)
        p.add_argument("--output", required=True, type=Path)
        if name == "compare":
            p.add_argument("--v1", required=True, nargs="+", type=Path)
            p.add_argument("--v2", required=True, nargs="+", type=Path)
    args = cli.parse_args()
    source = OUTPUT.read_bytes()
    verify(source)
    cutoff = cutoff_epoch(args.cutoff, "all")
    return (prepare if args.command == "prepare" else compare)(args, cutoff, source)


if __name__ == "__main__":
    raise SystemExit(main())
