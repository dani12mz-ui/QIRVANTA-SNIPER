"""Verify M5 Lead native events and protected TradingView journals."""

import argparse
import csv
from decimal import Decimal
from hashlib import sha256
from itertools import zip_longest
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from research.luminous_visual_v2.compare_native import confirmed_history, tagged, verify_frame
from research.professional_signal_selector.native_qa import FRAMES, log_messages, native_window


CONTROL = "37b903f46a2d878d9d0757da134620c22f2d067d"
CONTROL_PINE_SHA256 = "77feedcbef84e77b9270ec43840be5b823d838300d0a09d5ddb4fec7acc089f4"
SYMBOL = "OANDA:XAUUSD"
EVENT_TAG = "M5_LEAD_EVENT"
END_TAG = "M5_LEAD_END"
CONTEXT_FIELDS = {"m15", "h1", "h4"}
EXACT_FIELDS = {"event", "id", "known_ms", "side", "reason", "entry_ms"}
PRICE_FIELDS = {"level", "entry", "stop", "obstacle"}
FLOAT_FIELDS = {"atr", "room_r", "cost_r"}
EVENT_FIELDS = CONTEXT_FIELDS | EXACT_FIELDS | PRICE_FIELDS | FLOAT_FIELDS
TOLERANCE = Decimal("0.0000001")


def parse_json(line):
    return json.loads(line.partition("|")[2], parse_float=Decimal)


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def remove_new_records(source, destination, new_tags):
    """Strip only registered lead tags and keep all other records in source order."""
    with source.open(encoding="utf-8-sig", newline="") as original, destination.open(
        "w", encoding="utf-8", newline=""
    ) as filtered:
        rows = csv.DictReader(original, strict=True)
        if rows.fieldnames != ["Date", "Message"]:
            raise ValueError(f"{source}: unexpected Pine Logs CSV header")
        writer = csv.DictWriter(filtered, rows.fieldnames)
        writer.writeheader()
        for row in rows:
            messages = [message for message in row["Message"].splitlines()
                        if message.partition("|")[0] not in new_tags]
            if messages:
                writer.writerow({**row, "Message": "\n".join(messages)})


def lead_records(messages, target):
    """Check each lead event against its audited M5 close and native witness."""
    history = confirmed_history(messages)
    bars = {}
    for line in tagged(history, "DT_MARKET"):
        bar = parse_json(line)
        bars[bar["time_close"]] = bar
    end = parse_json(history[-1])
    if any(bar["symbol"] != SYMBOL or bar["timeframe"] != 5 for bar in bars.values()):
        raise ValueError("Expected standard OANDA:XAUUSD M5 confirmed market bars")
    if [bar["index"] for bar in bars.values()] != list(range(len(bars))):
        raise ValueError("Lead audit requires a contiguous bar_index starting at zero")
    if end["time_close"] != next(reversed(bars)):
        raise ValueError("Lead audit could not establish the final confirmed close")
    context = {}
    for line in tagged(messages, "RC1_CTX"):
        parts = line.split("|")
        context[int(parts[7])] = parts[12:14]
    events = []
    seen = set()
    checked = {"m15": 0, "h1": 0, "h4": 0, "h1_without_source": 0, "h4_without_source": 0}
    for line in tagged(messages, EVENT_TAG):
        item = parse_json(line)
        if set(item) != EVENT_FIELDS:
            raise ValueError(f"{EVENT_TAG}: event fields differ from the schema")
        known = item["known_ms"]
        side = item["side"]
        if type(known) is not int or known not in target or known not in bars:
            raise ValueError(f"{EVENT_TAG}: no audited confirmed bar at {known}")
        if events and events[-1]["known_ms"] > known:
            raise ValueError(f"{EVENT_TAG}: event order regresses at {known}")
        if type(side) is not int or side not in (-1, 1):
            raise ValueError(f"{EVENT_TAG}: invalid side at {known}")
        if any(not isinstance(item[key], str) for key in ("event", "id", "reason")):
            raise ValueError(f"{EVENT_TAG}: event name, ID and reason must be strings")
        entry_ms = item["entry_ms"]
        if entry_ms is not None and (type(entry_ms) is not int or entry_ms != bars[known]["time"]):
            raise ValueError(f"{EVENT_TAG}: effective entry time must be that confirmed bar’s open")
        if any(item[key] is not None and type(item[key]) not in (int, Decimal)
               for key in PRICE_FIELDS | FLOAT_FIELDS):
            raise ValueError(f"{EVENT_TAG}: geometry requires a number or null")
        if item["level"] is None:
            raise ValueError(f"{EVENT_TAG}: absent ID level at {known}")
        identity = item["id"].split("|")
        if (len(identity) != 5 or identity[:3] != [SYMBOL, "5", str(side)]
                or int(identity[3]) not in bars or int(identity[3]) > known
                or identity[4] != f"{Decimal(item['level']):.3f}"):
            raise ValueError(f"{EVENT_TAG}: invalid known-at timestamp or level in event ID")
        for name in CONTEXT_FIELDS:
            value = item[name]
            if value is not None and (type(value) is not int or value not in (-1, 0, 1)):
                raise ValueError(f"{EVENT_TAG}: invalid {name} trend at {known}")
            checked[name] += int(value is not None)
        if known not in context:
            raise ValueError(f"{EVENT_TAG}: missing RC1_CTX at {known}")
        for name, closed in zip(("h1", "h4"), context[known]):
            if closed == "null":
                if item[name] is not None:
                    raise ValueError(f"{EVENT_TAG}: {name} is set without a native source close at {known}")
                checked[f"{name}_without_source"] += 1
            elif int(closed) > bars[known]["time"]:
                raise ValueError(f"{EVENT_TAG}: {name} source close occurs after the chart bar opened")
        key = (item["event"], item["id"], known)
        if key in seen:
            raise ValueError(f"{EVENT_TAG}: duplicate event {key}")
        seen.add(key)
        events.append(item)

    witnesses = tagged(messages, END_TAG)
    if len(witnesses) != 1:
        raise ValueError(f"{END_TAG}: expected one coverage witness")
    witness = parse_json(witnesses[0])
    if witness != {"known_ms": end["time_close"], "bars": len(bars), "emitted": len(events)}:
        raise ValueError(f"{END_TAG}: coverage disagrees with confirmed market bars or emitted events")
    return events, witness, checked


def compare_events(actual, expected):
    expected_fields = EVENT_FIELDS - CONTEXT_FIELDS
    for number, (native, python) in enumerate(zip_longest(actual, expected), 1):
        if python is not None and (not isinstance(python, dict) or not expected_fields <= python.keys()):
            raise ValueError("Python replay event is missing fields required for parity")
        equal = native is not None and python is not None
        if equal:
            equal = all(native[key] == python[key] for key in EXACT_FIELDS | PRICE_FIELDS)
        if equal:
            for key in FLOAT_FIELDS:
                a, b = native[key], python[key]
                if (a is None) != (b is None) or a is not None and abs(a - b) > TOLERANCE:
                    equal = False
                    break
        if not equal:
            return {"status": "MISMATCH", "native_count": len(actual), "python_count": len(expected),
                    "first_difference": {"row": number, "native": native, "python": python}}
    return {"status": "MATCH" if actual else "NO_EVENT_EXAMPLES",
            "native_count": len(actual), "python_count": len(expected),
            "float_abs_tolerance": str(TOLERANCE),
            "context_scope": "Native source availability and valid trend enums only"}

def verify_pair(directory, frame, expected):
    original, candidate = [directory / f"{role}-M{frame}.csv" for role in ("baseline", "candidate")]
    if missing := [path.name for path in (original, candidate) if not path.is_file()]:
        return {"status": "MISSING_EVIDENCE", "files": missing}
    legacy, native = log_messages(original), log_messages(candidate)
    new_tags = {EVENT_TAG, END_TAG}
    control_rows = sum(len(tagged(legacy, tag)) for tag in new_tags)
    if control_rows:
        raise ValueError("New event tags occurred in the control journal")
    with TemporaryDirectory() as temp:
        filtered = Path(temp)
        (filtered / original.name).write_bytes(original.read_bytes())
        remove_new_records(candidate, filtered / candidate.name, new_tags)
        for role in ("baseline", "candidate"):
            review = directory / f"{role}-review-M{frame}.csv"
            if review.is_file():
                remove_new_records(review, filtered / review.name, new_tags)
        protected = verify_frame(filtered, frame)
    counts = {tag: len(tagged(native, tag)) for tag in sorted(new_tags)}
    if frame != 5:
        lead = {"status": "UNEXPECTED_EVENTS" if any(counts.values()) else "INACTIVE_AS_EXPECTED"}
    else:
        records, witness, checked_context = lead_records(native, set(native_window(frame)))
        lead = compare_events(records, expected) if expected is not None else {
            "status": "PYTHON_COMPARISON_PENDING", "native_count": len(records)
        }
        lead.update(coverage=witness, advisory_context=checked_context)
    match = protected["status"] == "MATCH" and lead["status"] in {"MATCH", "INACTIVE_AS_EXPECTED"}
    return {
        "status": "MATCH" if match else "INCOMPLETE_OR_MISMATCH",
        "raw_sha256": {path.name: digest(path) for path in (original, candidate)},
        "protected": protected,
        "lead": {**lead, "native_tag_counts": counts},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="Fresh baseline-M*.csv and candidate-M*.csv")
    parser.add_argument("--python-events", type=Path, help="JSON from replay.py or an ordered event array")
    parser.add_argument("--frames", nargs="+", choices=FRAMES, default=FRAMES, type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    expected = None if args.python_events is None else json.loads(
        args.python_events.read_text(encoding="utf-8"), parse_float=Decimal
    )
    if isinstance(expected, dict):
        expected = expected.get("events")
    if args.python_events is not None:
        if not isinstance(expected, list) or not all(isinstance(event, dict) and "known_ms" in event for event in expected):
            parser.error("--python-events must contain ordered events with known_ms")
        audit_closes = set(native_window(5))
        expected = [event for event in expected if event["known_ms"] in audit_closes]
    frames = {}
    for frame in args.frames:
        try:
            frames[f"M{frame}"] = verify_pair(args.directory, frame, expected)
        except (OSError, ValueError, KeyError, IndexError, TypeError, csv.Error) as exc:
            frames[f"M{frame}"] = {"status": "INVALID_EXPORT", "reason": str(exc)}
    match = all(lane["status"] == "MATCH" for lane in frames.values())
    report = {
        "status": "MATCH" if match else "INCOMPLETE_OR_MISMATCH",
        "control_commit": CONTROL,
        "control_pine_sha256": CONTROL_PINE_SHA256,
        "alert_scope": "Native payload and intent parity",
        "frames": frames,
    }
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n",
                           encoding="utf-8", newline="\n")
    return 0 if match else 1


if __name__ == "__main__":
    raise SystemExit(main())
