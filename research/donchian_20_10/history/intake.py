"""Validate locally obtained OANDA v20 H4/D midpoint history without network access."""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from io import StringIO
import json
from math import isfinite
from pathlib import Path
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ..data import COLUMNS, HOLIDAY_SOURCE, SHORT_SESSIONS, utc
from ..replay import CUTOFF, START, WARMUP


SCHEMA = "qirvanta.oanda-v20-export.v1"
ROOT = Path(__file__).resolve().parents[3]
NY = ZoneInfo("America/New_York")
FRAMES = {"H4": 4, "D": 24}
H4_HOURS = {1, 5, 9, 13, 17, 21}
FIXED_QUERY = {
    "instrument": "XAU_USD", "price": "M", "dailyAlignment": 17,
    "alignmentTimezone": "America/New_York", "smooth": False,
}
ISO_UTC = re.compile(
    r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.(?P<fraction>\d{1,9}))?(?:Z|\+00:00)"
)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True)
class Candle:
    opening: int
    closing: int
    day: date
    prices: tuple[Decimal, Decimal, Decimal, Decimal]


def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def load_json(raw):
    return json.loads(raw, object_pairs_hook=no_duplicate_keys)


def instant(value):
    match = ISO_UTC.fullmatch(value) if isinstance(value, str) else None
    if not match or (match["fraction"] and match["fraction"][3:].strip("0")):
        raise ValueError("Expected a UTC RFC3339 timestamp at millisecond precision or coarser")
    return stamp(datetime.fromisoformat(value.replace("Z", "+00:00")))


def local_at(stamp):
    return datetime.fromtimestamp(stamp / 1000, timezone.utc).astimezone(NY)


def stamp(value):
    return (value.astimezone(timezone.utc) - EPOCH) // timedelta(milliseconds=1)


def parse_price(value):
    if not isinstance(value, str):
        raise ValueError("OANDA PriceValue must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("Invalid OANDA PriceValue") from exc
    if not parsed.is_finite():
        raise ValueError("Price must be finite and positive in the replay")
    number = float(parsed)
    if not isfinite(number) or number <= 0:
        raise ValueError("Price must be finite and positive in the replay")
    return parsed


def parse_candle(row, frame):
    if not isinstance(row, dict) or type(row.get("complete")) is not bool:
        raise ValueError("Expected a candle with a boolean complete field")
    if "ask" in row or "bid" in row or not isinstance(row.get("mid"), dict):
        raise ValueError("Only a midpoint-only OANDA candle is supported")
    opening = instant(row["time"])
    local = local_at(opening)
    hours = H4_HOURS if frame == "H4" else {17}
    if local.hour not in hours or local.minute or local.second or local.microsecond:
        raise ValueError(f"{frame} candle is off its 17:00 New York alignment: {utc(opening)}")
    day = local.date() + timedelta(days=local.hour >= 17)
    if day.weekday() > 4:
        raise ValueError(f"{frame} candle has an unexpected weekend session: {utc(opening)}")
    o, h, low, c = (parse_price(row["mid"][key]) for key in ("o", "h", "l", "c"))
    if not (low <= min(o, c) <= max(o, c) <= h):
        raise ValueError(f"Invalid OHLC at {utc(opening)}")
    return Candle(opening, stamp(local + timedelta(hours=FRAMES[frame])), day, (o, h, low, c))


def read_chunk(path, expected_hash, frame, start, end, retrieved):
    raw = path.read_bytes()
    digest = sha256(raw).hexdigest()
    if digest != expected_hash:
        raise ValueError(f"{frame} source hash differs for request starting {utc(start)}")
    body = load_json(raw)
    if not isinstance(body, dict) or (body.get("instrument"), body.get("granularity")) != ("XAU_USD", frame):
        raise ValueError(f"Expected an OANDA XAU_USD {frame} CandlestickResponse")
    rows = body.get("candles")
    if not isinstance(rows, list) or len(rows) > 5000:
        raise ValueError("Expected an OANDA candles array of up to 5000 rows")
    complete, incomplete, after_cutoff = [], 0, 0
    previous = None
    for i, row in enumerate(rows):
        candle = parse_candle(row, frame)
        if not start <= candle.opening < end:
            raise ValueError(f"{frame} candle lies outside the declared half-open request range")
        if previous is not None and candle.opening < previous.closing:
            raise ValueError(f"{frame} candle sequence overlaps, repeats or reverses at {utc(candle.opening)}")
        previous = candle
        if row["complete"]:
            if candle.closing > retrieved:
                raise ValueError(f"Complete {frame} candle closes after recorded retrieval")
            if candle.closing <= CUTOFF:
                complete.append(candle)
            else:
                after_cutoff += 1
        else:
            if i != len(rows) - 1 or candle.closing <= retrieved:
                raise ValueError(f"Inconsistent incomplete {frame} candle at {utc(candle.opening)}")
            incomplete += 1
    return complete, {
        "granularity": frame, "from_utc": utc(start), "to_utc": utc(end), "sha256": digest,
        "rows": len(rows), "retained": len(complete),
        "excluded_incomplete": incomplete, "excluded_after_cutoff": after_cutoff,
    }


def chunks(manifest, root, retrieved):
    pages = {frame: [] for frame in FRAMES}
    names = set()
    entries = manifest.get("chunks")
    if not isinstance(entries, list):
        raise ValueError("Expected chunks array in the export manifest")
    for row in entries:
        frame = row["granularity"]
        if frame not in FRAMES:
            raise ValueError("Expected OANDA granularity H4 or D")
        start, end = map(instant, (row["from_utc"], row["to_utc"]))
        if not start < end:
            raise ValueError("Invalid request range")
        relative = Path(row["file"])
        path = (root / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(root) or path in names:
            raise ValueError("Chunk files must be distinct paths inside the private bundle")
        names.add(path)
        pages[frame].append((start, end, path, row["sha256"]))
    tapes, sources = {}, {}
    for frame, requests in pages.items():
        tape, reports = [], []
        prev_end = None
        for start, end, path, digest in sorted(requests, key=lambda item: item[:2]):
            if prev_end is not None and start < prev_end:
                raise ValueError(f"{frame} request ranges overlap; choose one version of the source")
            prev_end = end
            page, report = read_chunk(path, digest, frame, start, end, retrieved)
            if tape and page and tape[-1].closing > page[0].opening:
                raise ValueError(f"{frame} response chunks contain overlapping candles")
            tape.extend(page)
            reports.append(report)
        if not tape:
            raise ValueError(f"No complete {frame} candles at or before the registered cutoff")
        tapes[frame], sources[frame] = tape, reports
    return tapes, sources


def calendar_overrides(rows):
    result = {day: (time(h, m), HOLIDAY_SOURCE) for day, (h, m) in SHORT_SESSIONS.items()}
    supplied = set()
    for row in rows:
        day = date.fromisoformat(row["date"])
        reference = row["source"]
        source = urlsplit(reference)
        host = source.hostname or ""
        if (day in supplied or day.weekday() > 4 or source.scheme != "https"
                or not (host == "oanda.com" or host.endswith(".oanda.com"))
                or source.username is not None or source.password is not None or source.query or source.fragment):
            raise ValueError("Invalid, undocumented or repeated session override")
        supplied.add(day)
        if row["state"] == "closed":
            close = None
        elif row["state"] == "early_close":
            close = time.fromisoformat(row["close_local"])
            if close.tzinfo or not time(0) < close < time(17):
                raise ValueError("Early close must be a New York clock time before 17:00")
        else:
            raise ValueError("Unrecognized session override")
        result[day] = (close, reference)
    return result


def weekdays(first, last):
    for days in range((last - first).days + 1):
        day = first + timedelta(days=days)
        if day.weekday() < 5:
            yield day


def expected_session(day, overrides):
    opening = datetime.combine(day - timedelta(days=1), time(17), NY)
    closing = opening + timedelta(days=1)
    override = overrides.get(day)
    if override is None:
        market_close = closing
    else:
        override_time, _ = override
        market_close = datetime.combine(day, override_time, NY) if override_time is not None else opening
    slots = tuple(stamp(opening + timedelta(hours=hours)) for hours in range(0, 24, 4)
                  if opening + timedelta(hours=hours) < market_close)
    return stamp(opening), stamp(closing), slots


def aggregate_check(tapes, overrides):
    h4, daily = tapes["H4"], tapes["D"]
    h4_by_day = {}
    for candle in h4:
        h4_by_day.setdefault(candle.day, []).append(candle)
    daily_by_day = {candle.day: candle for candle in daily}
    first, last = max(h4[0].opening, daily[0].opening), min(h4[-1].closing, daily[-1].closing)
    start_day, end_day = max(h4[0].day, daily[0].day), min(h4[-1].day, daily[-1].day)
    counts, findings, boundary_exclusions = Counter(), [], []
    for day in weekdays(start_day, end_day):
        opening, closing, slots = expected_session(day, overrides)
        h4_last_close = stamp(local_at(slots[-1]) + timedelta(hours=4)) if slots else closing
        if opening < first or closing > daily[-1].closing or h4_last_close > h4[-1].closing:
            boundary_exclusions.append(day.isoformat())
            continue
        children = h4_by_day.get(day, [])
        parent = daily_by_day.get(day)
        seen = tuple(row.opening for row in children)
        calendar = "ORDINARY" if day not in overrides else ("SHORTENED" if slots else "CLOSED")
        missing = sorted(set(slots) - set(seen))
        unexpected = sorted(set(seen) - set(slots))
        if not slots:
            status = "DOCUMENTED_CLOSURE" if not children and parent is None else "CALENDAR_CONFLICT"
        elif parent is None or seen != slots:
            status = "UNEXPLAINED_MISSING_SESSION" if parent is None and not children else "INCOMPLETE_FRAME_COVERAGE"
        else:
            prices = (children[0].prices[0], max(row.prices[1] for row in children),
                      min(row.prices[2] for row in children), children[-1].prices[3])
            status = "MATCH" if parent.prices == prices else "OHLC_MISMATCH"
        counts[status] += 1
        if status != "MATCH" or calendar != "ORDINARY":
            row = {"session_date_ny": day.isoformat(), "status": status, "calendar": calendar,
                   "daily_present": parent is not None, "expected_h4": len(slots), "actual_h4": len(seen),
                   "missing_h4_open_utc": [utc(t) for t in missing],
                   "unexpected_h4_open_utc": [utc(t) for t in unexpected]}
            if status == "OHLC_MISMATCH":
                row["different_fields"] = [name for name, actual, reconstructed in
                                           zip(COLUMNS[2:], parent.prices, prices) if actual != reconstructed]
            if day in overrides:
                row["calendar_source"] = overrides[day][1]
            findings.append(row)
    return {
        "status": ("CROSS_CHECK_MATCH" if counts["MATCH"] and not any(
            count for state, count in counts.items() if state not in {"MATCH", "DOCUMENTED_CLOSURE"}
        ) else "REVIEW_REQUIRED"),
        "overlap_nominal_open_utc": utc(first) if first < last else None,
        "overlap_nominal_close_utc": utc(last) if first < last else None,
        "counts": dict(counts), "findings": findings, "boundary_exclusions_ny": boundary_exclusions,
    }


def tape_csv(tape):
    buf = StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerows((row.opening, row.closing, *row.prices) for row in tape)
    return buf.getvalue().encode("utf-8")


def frame_summary(tape, raw):
    offsets = [local_at(row.opening).utcoffset() for row in tape]
    return {
        "bars": len(tape), "sha256": sha256(raw).hexdigest(),
        "first_open_utc": utc(tape[0].opening), "last_close_utc": utc(tape[-1].closing),
        "bars_closed_before_registered_start": sum(row.closing < START for row in tape),
        "first_replay_eligible_close_utc": next(
            (utc(row.closing) for row in tape[WARMUP:] if START <= row.closing < CUTOFF), None
        ),
        "utc_offset_transitions_between_observed_opens": [
            {"last_previous_offset_open_utc": utc(a.opening), "first_new_offset_open_utc": utc(b.opening),
             "offset_from_hours": before / timedelta(hours=1), "offset_to_hours": after / timedelta(hours=1)}
            for a, b, before, after in zip(tape, tape[1:], offsets, offsets[1:]) if before != after
        ],
        "gaps_minutes_from_nominal_close_to_next_open": dict(sorted(Counter(
            (b.opening - a.closing) // 60_000 for a, b in zip(tape, tape[1:]) if a.closing < b.opening
        ).items())),
    }


def prepare(manifest_path, private_dir):
    manifest_path = Path(manifest_path).resolve()
    private_dir = Path(private_dir).resolve()
    if private_dir.is_relative_to(ROOT):
        raise ValueError("Normalized provider candles must stay outside the repository")
    raw_manifest = manifest_path.read_bytes()
    manifest = load_json(raw_manifest)
    if not isinstance(manifest, dict) or manifest.get("schema") != SCHEMA:
        raise ValueError(f"Expected source manifest schema {SCHEMA}")
    for key, expected in FIXED_QUERY.items():
        value = manifest.get(key)
        if type(value) is not type(expected) or value != expected:
            raise ValueError(f"Manifest request must have {key}={expected!r}")
    environment = manifest.get("environment")
    if environment not in ("practice", "live"):
        raise ValueError("Manifest environment must identify practice or live OANDA v20")
    retrieved = instant(manifest["retrieved_utc"])
    tapes, sources = chunks(manifest, manifest_path.parent, retrieved)
    coverage = aggregate_check(tapes, calendar_overrides(manifest.get("session_overrides", [])))
    report = {
        "status": "USER_SUPPLIED_SOURCE_VALIDATED_OFFLINE",
        "source_declaration": "OANDA v20 metadata is declared by the exporter; validation does not authenticate capture or license.",
        "environment": environment, "retrieved_utc": utc(retrieved),
        "registered_start_utc": utc(START), "registered_cutoff_utc": utc(CUTOFF), "warmup_bars": WARMUP,
        "timestamps": "Provider nominal opening and New York-aligned interval boundary; exact executable tick time is unavailable.",
        "request": FIXED_QUERY, "source_manifest_sha256": sha256(raw_manifest).hexdigest(),
        "chunks": sources, "same_source_H4_to_D1": coverage, "frames": {},
    }
    output = {}
    for label, frame in (("H4", "H4"), ("D1", "D")):
        raw = tape_csv(tapes[frame])
        output[label] = raw
        report["frames"][label] = frame_summary(tapes[frame], raw)
    private_dir.mkdir(parents=True, exist_ok=False)
    for label, raw in output.items():
        (private_dir / f"{label}.csv").write_bytes(raw)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--private-dir", type=Path, required=True, help="New directory outside the repository")
    parser.add_argument("--provenance", type=Path, help="New metadata-only JSON output. Defaults to stdout.")
    args = parser.parse_args()
    try:
        report = prepare(args.manifest, args.private_dir)
        message = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
        if args.provenance is None:
            print(message, end="")
        else:
            with args.provenance.open("x", encoding="utf-8", newline="\n") as target:
                target.write(message)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
