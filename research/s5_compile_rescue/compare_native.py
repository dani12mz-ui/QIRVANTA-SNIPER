"""Compare native S5/RC2 journals and independently compare Luminous zone events."""

import argparse
import csv
from decimal import Decimal
from hashlib import sha256
from io import BytesIO, StringIO
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from research.professional_signal_selector.native_qa import frame_checks, log_messages, native_window


ZONE_REF = "2a4d2e3daee64e7b73e2f3a95fce6a1762aa3d07"
ARCHIVE = "research/luminous_zones/evidence/native-logs.zip"
PRICE_KEYS = "open", "high", "low", "close"


def split_zone_log(source, protected):
    with source.open(encoding="utf-8-sig", newline="") as original, protected.open(
        "w", encoding="utf-8", newline=""
    ) as output:
        reader = csv.DictReader(original, strict=True)
        if reader.fieldnames != ["Date", "Message"]:
            raise ValueError(f"{source}: invalid Pine Logs CSV")
        writer = csv.DictWriter(output, fieldnames=reader.fieldnames)
        writer.writeheader()
        for row in reader:
            messages = [line for line in row["Message"].splitlines() if not line.startswith("LZ_EVENT|")]
            if messages:
                writer.writerow({**row, "Message": "\n".join(messages)})


def market_context(messages, tag, target):
    out = {}
    for line in messages:
        if not line.startswith(tag + "|"):
            continue
        row = json.loads(line.partition("|")[2], parse_float=Decimal)
        close = row["time_close"]
        if close not in target:
            continue
        market = (row["symbol"], row["timeframe"], row["time"], *(row[key] for key in PRICE_KEYS))
        if out.setdefault(close, market) != market:
            raise ValueError(f"{tag}: conflicting OHLC at {close}")
    return out


def event_fields(identifier, state, known, proximal, distal, level, pivot, fvg):
    return (identifier, state, int(known), Decimal(str(proximal)), Decimal(str(distal)),
            Decimal(str(level)), int(pivot), fvg)


def integrated_events(messages, target):
    result = []
    for line in messages:
        if line.startswith("LZ_EVENT|"):
            _, symbol, frame, side, formed, origin, state, known, _, prox, dist, level, pivot, fvg = line.split("|")
            if int(known) in target:
                identifier = "|".join((symbol, frame, side, formed, origin))
                result.append(event_fields(identifier, state, known, prox, dist, level, pivot, fvg == "true"))
    return result


def standalone_events(messages, target):
    result = []
    for line in messages:
        if line.startswith("PZ_EVENT|"):
            row = json.loads(line.partition("|")[2], parse_float=Decimal)
            if row["known_ms"] in target:
                result.append(event_fields(*(row[k] for k in (
                    "zone_id", "state", "known_ms", "proximal", "distal", "break_level", "pivot_known_ms", "fvg"
                ))))
    return result


def compare_zones(source, frame, archive):
    reference = archive.read(f"after/zone-M{frame}.csv")
    other = [line for row in csv.DictReader(StringIO(reference.decode("utf-8-sig"), newline=""))
             for line in row["Message"].splitlines()]
    actual = log_messages(source)
    target = set(native_window(frame))
    original_market = market_context(other, "PZ_MARKET", target)
    integrated_market = market_context(actual, "DT_MARKET", target)
    if original_market.keys() != target or original_market != integrated_market:
        return {"status": "NONIDENTICAL_OHLC", "expected_closes": len(target),
                "archive_closes": len(original_market), "integrated_closes": len(integrated_market)}
    original_events, actual_events = standalone_events(other, target), integrated_events(actual, target)
    mismatches = [i for i, pair in enumerate(zip(original_events, actual_events), 1) if pair[0] != pair[1]]
    equal = not mismatches and len(original_events) == len(actual_events)
    return {"status": "MATCH" if equal and actual_events else "NO_EXAMPLES" if equal else "MISMATCH",
            "scope": "Archived native 2a4d2e3 on matching target-window OHLC. Run prefixes differ.",
            "same_ohlc_closes": len(target), "standalone_events": len(original_events),
            "integrated_events": len(actual_events), "first_mismatch_rows": mismatches[:10],
            "counts_by_state": {state: sum(event[1] == state for event in actual_events)
                                for state in sorted({event[1] for event in actual_events})},
            "reference_csv_sha256": sha256(reference).hexdigest()}


def compare(directory, frames, combined):
    report = {}
    archive_bytes = subprocess.check_output(["git", "show", f"{ZONE_REF}:{ARCHIVE}"]) if combined else None
    with TemporaryDirectory() as temporary:
        for frame in frames:
            if not combined:
                report[f"M{frame}"] = {"protected": frame_checks(directory, frame)}
                continue
            baseline = directory / f"baseline-M{frame}.csv"
            source = directory / f"candidate-M{frame}.csv"
            clean = Path(temporary)
            (clean / baseline.name).write_bytes(baseline.read_bytes())
            split_zone_log(source, clean / source.name)
            protected = frame_checks(clean, frame)
            with ZipFile(BytesIO(archive_bytes)) as archive:
                zones = compare_zones(source, frame, archive)
            report[f"M{frame}"] = {"protected": protected, "zones": zones,
                                   "raw_candidate_sha256": sha256(source.read_bytes()).hexdigest()}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Native baseline-M*.csv and candidate-M*.csv")
    parser.add_argument("--frames", nargs="+", type=int, choices=(5, 15, 30), default=(5, 15, 30))
    parser.add_argument("--combined", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = compare(args.directory, args.frames, args.combined)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n",
                           encoding="utf-8", newline="\n")
    return int(any(row["protected"]["status"] != "MATCH"
                   or args.combined and row["zones"]["status"] != "MATCH" for row in result.values()))


if __name__ == "__main__":
    raise SystemExit(main())
