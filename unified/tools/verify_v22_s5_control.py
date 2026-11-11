"""Compare a fresh S5 run with the immutable Premium V2 archive."""
import argparse
from pathlib import Path

from unified.tools.audit_premium_parity import compact, comparison
from unified.tools.audit_tv_observations import ALIGNMENT_FIELDS
from unified.tools.premium_native_pair import FRAMES, read_observed, write_report

SOURCE = "a920f15370b536f19def0205b21915e1a3d2e4ed4c76c80bd22f5e8aa6bf7c10"
CUTOFF = 1794321000000  # 2026-11-10 14:30 UTC


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", nargs=3, type=Path, required=True)
    parser.add_argument("--fresh", nargs=3, type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    old = read_observed(args.archive, SOURCE, CUTOFF, FRAMES)
    new = read_observed(args.fresh, SOURCE, CUTOFF, FRAMES)
    expected = {("S5", f) for f in FRAMES}
    if set(old) != expected or set(new) != expected:
        raise ValueError("Expected one S5 run on each of M5, M15 and M30")
    rows = []
    for frame in FRAMES:
        a, b = old["S5", frame], new["S5", frame]
        different_bars = [k for k in ALIGNMENT_FIELDS if a[0]["run"][k] != b[0]["run"][k]]
        discrepancies = comparison(a[0], b[0])
        rows.append({
            "frame": frame, "data_discrepancies": different_bars, "journal_discrepancies": discrepancies,
            "archive": compact(a), "fresh": compact(b),
        })
    passed = not any(r["data_discrepancies"] or r["journal_discrepancies"] for r in rows)
    write_report(args.out, {
        "status": "VERIFIED" if passed else "INCONCLUSIVE",
        "scope": "Fresh native S5 observer versus prior S5 observer at the same closed-bar cutoff.",
        "source_sha256": SOURCE,
        "cutoff_ms": CUTOFF,
        "runs": rows,
    })
    print("S5 control", "matched" if passed else "differs")
    return int(not passed)


if __name__ == "__main__":
    raise SystemExit(main())
