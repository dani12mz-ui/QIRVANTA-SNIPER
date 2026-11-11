"""Verify canonical Python OHLC context on the pinned historical TradingView archives."""

import argparse
import hashlib
import json
from pathlib import Path

from research.data.loader import load_bars
from unified.tools.audit_v22_intelligence import Bar as NativeV22Bar, records
from unified.tools.gold_market_data import context_rows


ROOT = Path(__file__).resolve().parents[2]
S5_REGISTRY = ROOT / "docs/premium_evidence/s5_screen_registration.json"
V22_ARCHIVE = ROOT / "docs/v22_evidence/intelligence_causality_final.json"


def digest(path, expected):
    value = hashlib.sha256(path.read_bytes()).hexdigest()
    if value != expected:
        raise ValueError(f"Unexpected archive SHA-256 {path}")
    return value


def compare_sources(rows, export_path):
    by_open = {row.bar.open_at: row for row in rows}
    counts = {label: {"compared": 0, "conservatively_unknown": 0} for label in ("h1", "h4")}
    overlap = 0
    for kind, known, payload in records(export_path):
        if kind != "BAR":
            continue
        exported = NativeV22Bar.parse(known, payload)
        row = by_open.get(exported.open_time)
        if row is None:
            continue
        overlap += 1
        if row.bar.close_at != known or row.bar.ohlc != (exported.open, exported.high, exported.low, exported.close):
            raise ValueError(f"Overlapping chart OHLC differs at {exported.open_time}")
        for label, count in counts.items():
            inferred = getattr(row, label)
            original_stamp = getattr(exported, f"{label}_at")
            original_price = getattr(exported, f"{label}_values")[0]
            if inferred.known_at is None or original_stamp is None:
                count["conservatively_unknown"] += 1
            elif (inferred.known_at, inferred.close) != (original_stamp, original_price):
                raise ValueError(f"Overlapping {label} source differs at {exported.open_time}")
            else:
                count["compared"] += 1
    if overlap == 0:
        raise ValueError("No overlapping chart OHLC")
    return overlap, counts


def evaluate(data_root, logs_dir):
    registration = json.loads(S5_REGISTRY.read_bytes())["frames"]
    exports = {row["frame"]: row for row in json.loads(V22_ARCHIVE.read_bytes())["cells"]}
    bars = {}
    archived = {}
    for frame, source in registration.items():
        path = data_root / Path(source["local_bars"]).parent.name / "bars.csv"
        archived[frame] = digest(path, source["bars_sha256"])
        bars[frame] = load_bars(path)
    report = {}
    for frame, chart in bars.items():
        earlier = [bar for bar in bars["30"] if bar["time_close"] <= chart[0]["time"]]
        rows = list(context_rows(chart, earlier=earlier))
        ready = [row for row in rows if row.ready]
        if not ready:
            raise ValueError(f"No initialized M{frame} chart rows")
        if any(group.known_at is not None and group.known_at > row.bar.open_at
               for row in rows for group in (row.h1, row.h4, row.d1)):
            raise ValueError(f"Future HTF source in M{frame}")
        cut_indices = (len(chart) // 2, len(chart) * 3 // 4)
        for size in cut_indices:
            if list(context_rows(chart[:size], earlier=earlier)) != rows[:size]:
                raise ValueError(f"M{frame} prefix depends on a later bar")
        path = logs_dir / f"indicator_final_M{frame}.csv"
        native_sha = digest(path, exports[frame]["raw_log_sha256"])
        overlap, checked = compare_sources(rows, path)
        report[frame] = {
            "bar_sha256": archived[frame], "native_v22_log_sha256": native_sha,
            "chart_bars": len(chart), "m30_warmup_bars": len(earlier),
            "eligible_bars": len(ready), "unknown_h1_or_h4_bars": len(rows) - len(ready),
            "first_eligible_close_utc_ms": ready[0].bar.close_at,
            "overlapping_native_v22_chart_rows": overlap,
            "source_comparison": checked,
            "unchanged_prefix_lengths": list(cut_indices),
        }
    return {
        "status": "VERIFIED_ARCHIVED_CONTEXT",
        "scope": "Pinned observed chart bars, reconstructed HTF source closes, finite EMA causality and UNKNOWN coverage. GOLD PRO native parity uses separate exports.",
        "frames": report,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=ROOT / "../mission")
    parser.add_argument("--logs-dir", type=Path, default=ROOT / "../../outputs/v22")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = evaluate(args.data_root, args.logs_dir)
    with args.out.open("x", encoding="utf-8", newline="\n") as output:
        json.dump(report, output, indent=2, sort_keys=True)
        output.write("\n")
    print(f"Audited {sum(row['chart_bars'] for row in report['frames'].values())} archived chart bars")


if __name__ == "__main__":
    main()
