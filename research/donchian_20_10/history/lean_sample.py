"""Prepare the public, pinned QuantConnect LEAN OANDA XAUUSD sample for an isolated replay."""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from io import BytesIO, StringIO
from itertools import groupby
import json
from math import isfinite
from pathlib import Path
from statistics import median
from zipfile import ZipFile

from ..data import COLUMNS, NY, bounds, utc
from ..native_runtime import read_events
from ..replay import HOUR_MS, START, Bar, validate_bars


COMMIT = "cda76ee3162a5bf22d66f56a85e1db6a9d22cc3c"
PUBLIC_TREE = f"https://github.com/QuantConnect/Lean/blob/{COMMIT}/Data/cfd/oanda"
SOURCES = {
    "hour": ("2c0675003e21c8f1310914701053cc22157cc52d",
             "535aa150b81072eacdccf5be9c64942ba636235f0ea07e297976d40ced82c49f"),
    "daily": ("e574b1f4349ee0ade9cf093826dbe90aba9c3cdc",
              "9d0e71f96a3dd3cb32bd0af5f7ca2d86ae752eb4aae77fe1f392a1a4941022c5"),
}
PREHISTORY = datetime(2015, 1, 1, tzinfo=timezone.utc)
NATIVE_EVIDENCE = Path(__file__).resolve().parents[1] / "evidence"


@dataclass(frozen=True)
class Quote:
    at: datetime
    bid: tuple[float, float, float, float]
    ask: tuple[float, float, float, float]

    @property
    def stamp(self):
        return milliseconds(self.at)

    def midpoint_bar(self):
        # Bid and ask extrema need not occur on the same tick.
        midpoint = tuple((b + a) / 2 for b, a in zip(self.bid, self.ask))
        return Bar(self.stamp, self.stamp + HOUR_MS, *midpoint)


def milliseconds(moment):
    return int(moment.timestamp() * 1000)


def read_archive(path, resolution):
    data = Path(path).read_bytes()
    digest = sha256(data).hexdigest()
    git_blob, expected_hash = SOURCES[resolution]
    if digest != expected_hash:
        raise ValueError(f"Unexpected SHA-256 for pinned LEAN {resolution} XAUUSD archive")
    with ZipFile(BytesIO(data)) as archive:
        if archive.namelist() != ["xauusd.csv"]:
            raise ValueError(f"Unexpected LEAN {resolution} ZIP members")
        reader = csv.reader(StringIO(archive.read("xauusd.csv").decode("ascii")))
        quotes = []
        for number, row in enumerate(reader, start=1):
            if len(row) != 11:
                raise ValueError(f"Expected eleven LEAN quote fields at {resolution} row {number}")
            at = datetime.strptime(row[0], "%Y%m%d %H:%M").replace(tzinfo=timezone.utc)
            if at.minute or (resolution == "daily" and at.hour) or (quotes and at <= quotes[-1].at):
                raise ValueError(f"Out of order, duplicate or unaligned LEAN row {resolution} {number}")
            bid, ask = tuple(map(float, row[1:5])), tuple(map(float, row[6:10]))
            for prices in (bid, ask):
                o, h, low, close = prices
                if not all(isfinite(x) and x > 0 for x in prices) or not low <= min(o, close) <= max(o, close) <= h:
                    raise ValueError(f"Invalid quote OHLC at {resolution} row {number}")
            if any(b > a for b, a in zip(bid, ask)):
                raise ValueError(f"Crossed quote aggregate at {resolution} row {number}")
            quotes.append(Quote(at, bid, ask))
    return quotes, {"url": f"{PUBLIC_TREE}/{resolution}/xauusd.zip", "git_blob_sha1": git_blob,
                    "sha256": digest, "bytes": len(data), "rows": len(quotes),
                    "first_utc_label": utc(quotes[0].stamp), "last_utc_label": utc(quotes[-1].stamp)}


def aggregate_prices(prices):
    return (prices[0][0], max(row[1] for row in prices), min(row[2] for row in prices), prices[-1][3])


def audit_daily_utc(hour, daily):
    """Validate the separate daily archive's actual UTC-midnight convention."""
    published = {q.at.date(): (q.bid, q.ask) for q in daily if q.at >= PREHISTORY}
    matched, mismatches, absent = 0, [], []
    for day, group in groupby((q for q in hour if q.at >= PREHISTORY), key=lambda q: q.at.date()):
        rows = tuple(group)
        result = tuple(aggregate_prices([getattr(q, side) for q in rows]) for side in ("bid", "ask"))
        if day not in published:
            absent.append(day.isoformat())
        elif published.pop(day) == result:
            matched += 1
        else:
            mismatches.append(day.isoformat())
    return {"alignment": "UTC midnight, verified by aggregating the separate bid and ask hourly OHLC",
            "matched_utc_days": matched, "different_utc_days": mismatches,
            "utc_days_without_daily": absent, "utc_days_without_hourly": [x.isoformat() for x in sorted(published)],
            "in_replay": False}


def make_bar(rows, opening, closing):
    ohlc = aggregate_prices([(b.open, b.high, b.low, b.close) for b in rows])
    return Bar(opening, closing, *ohlc)


def session_bars(hour):
    """Keep H4 and D1 on the same set of completely observed research sessions."""
    by_frame = {"D1": [], "H4": []}
    observed, omitted, opening_hours = set(), [], Counter()
    active = [q.midpoint_bar() for q in hour if q.at >= PREHISTORY]
    for (opening, _, closing), group in groupby(active, key=lambda b: bounds(b.time_ms, "D1")):
        rows = tuple(group)
        start, end = milliseconds(opening), milliseconds(closing)
        local_open_hour = datetime.fromtimestamp(rows[0].time_ms / 1000, NY).hour
        valid_weekday = opening.weekday() in (6, 0, 1, 2, 3)
        allowed = (18, 19) if opening.weekday() == 6 else (17, 18)
        contiguous = tuple(b.time_ms for b in rows) == tuple(range(rows[0].time_ms, end, HOUR_MS))
        observed.add(opening)
        if not valid_weekday or local_open_hour not in allowed or not contiguous:
            reason = ("OUTSIDE_STANDARD_WEEK" if not valid_weekday else
                      "UNVERIFIED_OPEN" if local_open_hour not in allowed else "MISSING_OR_SHORT_HOURS")
            omitted.append({"open_utc": utc(start), "close_utc": utc(end), "reason": reason,
                            "observed_hours": len(rows), "first_observed_utc": utc(rows[0].time_ms),
                            "last_observed_end_utc": utc(rows[-1].close_ms)})
            continue
        opening_hours[f"{'Sunday' if opening.weekday() == 6 else 'Monday–Thursday'} {local_open_hour:02d}:00"] += 1
        by_frame["D1"].append(make_bar(rows, start, end))
        for (first, _, last), h4 in groupby(rows, key=lambda b: bounds(b.time_ms, "H4")):
            by_frame["H4"].append(make_bar(tuple(h4), milliseconds(first), milliseconds(last)))
    cursor, last_observed = min(observed), max(observed)
    while cursor < last_observed:
        if cursor.weekday() in (6, 0, 1, 2, 3) and cursor not in observed:
            omitted.append({"open_utc": utc(milliseconds(cursor)),
                            "close_utc": utc(milliseconds(cursor + timedelta(days=1))),
                            "reason": "NO_QUOTE_HOURS", "observed_hours": 0})
        cursor += timedelta(days=1)
    for frame in by_frame.values():
        validate_bars(tuple(frame))
    return by_frame, {"accepted_open_hours_ny": dict(sorted(opening_hours.items())),
                      "omitted_sessions": sorted(omitted, key=lambda row: row["open_utc"])}


def audit_tradingview_overlap(bars, fills):
    opens = {bar.time_ms: bar.open for bar in bars}
    shared = []
    for fill in fills:
        timestamp = fill["entry_ms"]
        if timestamp not in opens:
            continue
        raw, sample = float(fill["open"]), opens[timestamp]
        shared.append({"utc": utc(timestamp), "tradingview_open": raw, "lean_open": sample,
                       "absolute_difference": abs(raw - sample)})
    differences = [r["absolute_difference"] for r in shared]
    return {"shared_d1_native_fill_timestamps": len(shared),
            "matching_prices_at_1e_minus_9": sum(d < 1e-9 for d in differences),
            "median_absolute_difference_price": median(differences) if differences else None,
            "maximum_absolute_difference_price": max(differences, default=None),
            "first_shared_open": shared[0] if shared else None,
            "scope": "TradingView logged raw bar open versus LEAN estimated midpoint open, not filled prices"}


def prepare(hour_zip, daily_zip, output):
    hour, hour_source = read_archive(hour_zip, "hour")
    daily, daily_source = read_archive(daily_zip, "daily")
    check = audit_daily_utc(hour, daily)
    if (not check["matched_utc_days"] or check["different_utc_days"] or check["utc_days_without_daily"]
            or check["utc_days_without_hourly"]):
        raise ValueError("Pinned LEAN UTC daily cross-check did not match")
    by_frame, coverage = session_bars(hour)
    native = (NATIVE_EVIDENCE / "native-logs.zip").read_bytes()
    expected = json.loads((NATIVE_EVIDENCE / "capture_manifest.json").read_text(encoding="utf-8"))
    if sha256(native).hexdigest() != expected["artifact_sha256"]["native-logs.zip"]:
        raise ValueError("Unexpected SHA-256 for the frozen TradingView journal")
    with ZipFile(BytesIO(native)) as archive:
        overlap = audit_tradingview_overlap(by_frame["D1"], read_events(archive.read("D1.csv"))["D20_FILL"])
    output.mkdir(parents=True, exist_ok=True)
    manifest = {
        "status": "ISOLATED_PUBLIC_OANDA_QUOTEBAR_SAMPLE",
        "provider": "OANDA XAUUSD bid/ask quotes distributed in QuantConnect LEAN public sample",
        "source_commit": COMMIT,
        "source_license": "https://github.com/QuantConnect/Lean/blob/cda76ee3162a5bf22d66f56a85e1db6a9d22cc3c/LICENSE",
        "source_format": "https://www.quantconnect.com/docs/v2/lean-engine/data-format/forex-and-cfd",
        "sources": {"hour": hour_source, "daily": daily_source},
        "earliest_input_utc": PREHISTORY.isoformat(),
        "aggregation": "NY 17:00 nominal sessions and H4 boundaries. Same accepted sessions in both frames.",
        "quality_policy": "Contiguous observed hours through NY 17:00; keep Sunday starts at 18:00 or 19:00 and "
                          "Monday–Thursday starts at 17:00 or 18:00. Other gaps and unverified shortened sessions "
                          "are omitted from both frames, including missing full weekday sessions. Calendar dates "
                          "alone cannot distinguish omissions from historical holidays. Pending orders and stops "
                          "across these gaps in coarse replay may miss unobserved price action.",
        "price_model": "Hourly midpoint estimates average separately aggregated bid and ask OHLC; extrema may not "
                       "be simultaneous. Input is a distinct market-data lane and does not establish TradingView parity.",
        "utc_daily_validation": check, "coverage": coverage, "frames": {},
        "tradingview_d1_source_overlap": overlap,
    }
    for name, bars in by_frame.items():
        path = output / f"{name}.csv"
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(COLUMNS)
            writer.writerows(tuple(getattr(bar, key) for key in COLUMNS) for bar in bars)
        manifest["frames"][name] = {
            "sha256": sha256(path.read_bytes()).hexdigest(), "bars": len(bars),
            "pre_2016_bars": sum(b.close_ms < START for b in bars),
            "first_open_utc": utc(bars[0].time_ms), "last_close_utc": utc(bars[-1].close_ms),
            "bars_by_close_year": dict(sorted(Counter(utc(b.close_ms)[:4] for b in bars).items())),
        }
    (output / "data_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hour-zip", type=Path, required=True)
    parser.add_argument("--daily-zip", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.hour_zip, args.daily_zip, args.output)


if __name__ == "__main__":
    main()
