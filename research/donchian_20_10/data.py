"""Build OANDA chart-session research inputs from the pinned earlier M30 tapes."""

import argparse
from collections import Counter
import csv
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
from io import StringIO
from itertools import groupby
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile
from zoneinfo import ZoneInfo

from gold_pro.rc1_qa import read_export
from gold_pro.rc2_research import chart_bars
from research.donchian_20_10.replay import Bar


ROOT = Path(__file__).resolve().parents[2]
PROTOCOL = Path(__file__).with_name("protocol.json")
ARCHIVE = ROOT / "releases/gold-pro-rc2/evidence/native-logs.zip"
NY = ZoneInfo("America/New_York")
OHLC = ("open", "high", "low", "close")
COLUMNS = ("time_ms", "close_ms", *OHLC)
M30 = 1_800_000
SHORT_SESSIONS = {
    date(2026, 1, 19): (14, 30), date(2026, 2, 16): (14, 30), date(2026, 5, 25): (14, 30),
    date(2026, 6, 19): (13, 0), date(2026, 7, 3): (13, 0), date(2026, 9, 7): (14, 30),
}
HOLIDAY_SOURCE = "https://www.oanda.com/ca-en/trading/holiday-trading-hours/"


def ms(iso):
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def utc(stamp):
    return datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def digest(raw):
    return sha256(raw).hexdigest()


def parse(source):
    return Bar(int(source["time"]), int(source["time_close"]), *(float(source[key]) for key in OHLC))


def load_m30(path, registration):
    local_raw = Path(path).read_bytes()
    if digest(local_raw) != registration["data"]["local_oanda_m30_archive_sha256"]:
        raise ValueError("Local OANDA M30 archive differs from its recorded SHA-256")
    if digest(ARCHIVE.read_bytes()) != registration["data"]["rc2_native_archive_sha256"]:
        raise ValueError("RC2 control archive differs from its pinned SHA-256")
    cutoff = ms(registration["evaluation"]["historical_end_utc_exclusive_for_signals"])
    local = [parse(row) for row in csv.DictReader(StringIO(local_raw.decode("utf-8-sig")))]
    with ZipFile(ARCHIVE) as bundle, TemporaryDirectory() as scratch:
        name, = [x for x in bundle.namelist() if Path(x).name == "fixed_M30.csv"]
        tape = Path(scratch) / "fixed_M30.csv"
        tape.write_bytes(bundle.read(name))
        tail = [parse(row) for row in chart_bars(read_export(tape, 30, "audit"))]
    if len({bar.time_ms for bar in local}) != len(local) or len({bar.time_ms for bar in tail}) != len(tail):
        raise ValueError("Duplicate bars in a source tape")
    by_time = {bar.time_ms: bar for bar in local}
    overlap = 0
    differences = []
    for bar in tail:
        if bar.time_ms in by_time:
            overlap += 1
            if by_time[bar.time_ms] != bar:
                differences.append({"open_utc": utc(bar.time_ms), "archive": repr(by_time[bar.time_ms]),
                                    "rc2_tail": repr(bar)})
        else:
            by_time[bar.time_ms] = bar
    if not overlap or differences:
        raise ValueError(f"Cannot merge OANDA M30 tapes: {overlap} overlaps, first difference {differences[:1]}")
    bars = sorted((bar for bar in by_time.values() if bar.close_ms <= cutoff), key=lambda bar: bar.time_ms)
    for i, bar in enumerate(bars):
        if bar.close_ms - bar.time_ms != M30 or (i and bar.time_ms < bars[i - 1].close_ms):
            raise ValueError(f"Unexpected M30 duration or overlap at {utc(bar.time_ms)}")
    return bars, {"overlap_bars": overlap, "overlap_differences": 0, "local_rows": len(local),
                  "rc2_rows": len(tail), "merged_rows_to_cutoff": len(bars),
                  "first_open_utc": utc(bars[0].time_ms), "last_close_utc": utc(bars[-1].close_ms),
                  "raw_gap_duration_minutes": dict(sorted(Counter(
                      (b.time_ms - a.close_ms) // 60_000 for a, b in zip(bars, bars[1:])
                      if b.time_ms > a.close_ms).items()))}


def bounds(opening, frame):
    local = datetime.fromtimestamp(opening / 1000, timezone.utc).astimezone(NY)
    day17 = local.replace(hour=17, minute=0, second=0, microsecond=0)
    if local < day17:
        day17 -= timedelta(days=1)
    if frame == "D1":
        return day17, day17 + timedelta(hours=1), day17 + timedelta(days=1)
    if frame != "H4":
        raise ValueError(f"Unknown research frame {frame}")
    offset = int((local - day17).total_seconds() // (4 * 3600))
    first = day17 + timedelta(hours=offset * 4)
    return first, first + timedelta(hours=1 if offset == 0 else 0), first + timedelta(hours=4)


def aggregate(bars, frame):
    groups, exclusions, shortened = [], [], []
    for (nominal, first, end), group in groupby(bars, key=lambda bar: bounds(bar.time_ms, frame)):
        children = tuple(group)
        # Published early-close minutes round to the end of the last observed M30 candle.
        # TradingView's diagnostic timestamps preserve the nominal higher-frame boundary.
        local_session_end = (end - timedelta(microseconds=1)).date()
        short_at = SHORT_SESSIONS.get(local_session_end) if frame == "D1" or first.hour < 17 else None
        market_end = end if not short_at else min(end, end.replace(hour=short_at[0], minute=short_at[1]))
        opening, active, closing, effective = (
            int(stamp.timestamp() * 1000) for stamp in (nominal, first, end, market_end))
        expected = (effective - active) // M30
        complete = len(children) == expected and all(
            child.time_ms == active + i * M30 and child.close_ms == active + (i + 1) * M30
            for i, child in enumerate(children)
        )
        if complete:
            groups.append(Bar(opening, closing, children[0].open, max(b.high for b in children),
                              min(b.low for b in children), children[-1].close))
            if effective != closing:
                shortened.append({"nominal_open_utc": utc(opening), "nominal_close_utc": utc(closing),
                                  "last_child_close_utc": utc(effective), "m30_bars": len(children)})
        else:
            exclusions.append({"nominal_open_utc": utc(opening), "nominal_close_utc": utc(closing),
                               "first_child_utc": utc(children[0].time_ms),
                               "last_child_close_utc": utc(children[-1].close_ms),
                               "expected_m30": expected, "observed_m30": len(children)})
    return groups, exclusions, shortened


def read_bars(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        rows = csv.DictReader(stream)
        if tuple(rows.fieldnames or ()) != COLUMNS:
            raise ValueError(f"Expected CSV columns {COLUMNS}")
        return [Bar(int(row["time_ms"]), int(row["close_ms"]), *(float(row[key]) for key in OHLC))
                for row in rows]


def prepare(local_m30, private_dir):
    registration = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    bars, meta = load_m30(local_m30, registration)
    manifest = {"provider": "OANDA via pre-existing local TradingView research exports",
                "symbol": registration["instrument"], "timestamps": "UTC, NY wall-clock bucket edges",
                "m30_archive_sha256": registration["data"]["local_oanda_m30_archive_sha256"],
                "rc2_archive_sha256": registration["data"]["rc2_native_archive_sha256"],
                "raw_sources": meta, "frames": {}, "revisions": "None applied. Sources agreed on overlap.",
                "whole_missing_sessions": "No fabricated candles; only published 2026 holiday early closures accepted with complete M30 coverage to shortened end.",
                "holiday_schedule": HOLIDAY_SOURCE,
                "redistribution": "Full provider OHLC and reconstructed tapes stay local."}
    Path(private_dir).mkdir(parents=True, exist_ok=True)
    warmup = registration["signal"]["minimum_prior_bars"]
    for frame in ("D1", "H4"):
        series, exclusions, shortened = aggregate(bars, frame)
        target = Path(private_dir) / f"{frame}.csv"
        with target.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(COLUMNS)
            writer.writerows(tuple(getattr(row, key) for key in COLUMNS) for row in series)
        manifest["frames"][frame] = {
            "bars": len(series), "sha256": digest(target.read_bytes()),
            "first_open_utc": utc(series[0].time_ms), "last_close_utc": utc(series[-1].close_ms),
            "first_eligible_close_utc": utc(series[warmup].close_ms) if len(series) > warmup else None,
            "excluded_incomplete_groups": exclusions,
            "complete_shortened_sessions": shortened,
            "gaps_exceeding_normal_weekend": [
                {"previous_close_utc": utc(a.close_ms), "next_open_utc": utc(b.time_ms)}
                for a, b in zip(series, series[1:]) if b.time_ms - a.close_ms > 49 * 3_600_000],
        }
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-m30", type=Path, required=True)
    parser.add_argument("--private-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args()
    args.manifest.write_text(json.dumps(prepare(args.local_m30, args.private_dir), indent=2) + "\n",
                             encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
