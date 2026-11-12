"""Audit the Premium renderer and compare the original and Premium Pine Logs."""

import argparse
import csv
from collections import Counter
from hashlib import sha256
from itertools import zip_longest
import json
from pathlib import Path
import re

from research.precision_buy_sell_zones.native_qa import alert_compare, read_zone_native, utc


ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
SYMBOL = "OANDA:XAUUSD"
FRAMES = (5, 15, 30)
BASELINE_COMMIT = "4059963f4e3091ae7a92864ab5b8944c140bc429"
REMOTE_CANDIDATE_COMMIT = "101db3408139f30c2d80b210c49c1603cc1e45ba"
BASELINE = ROOT / "research/precision_buy_sell_zones/QIRVANTA_GOLD_PRO_PRECISION_ZONES.pine"
CANDIDATE = HERE / "QIRVANTA_PRECISION_ZONES_PREMIUM.pine"
PROTECTED = {
    BASELINE: "acfaf5ac3fd80f08efd7f179cd3802eb41043f957a70800c3444595bf5ebc09a",
    ROOT / "research/precision_buy_sell_zones/PROTOCOL.json":
        "7406878761ffa1d6db918b72a2e283efde224f542723238a49f10cf80023ecc4",
    ROOT / "research/final_htf_data_integrity/QIRVANTA_GOLD_PRO_HTF_DISPLAY_FIX.pine":
        "a9955a1ed3f402f1b8674b3ef2fabfc3d6578bb20f61d8a7d12ade5f527353a2",
    ROOT / "docs/gold_pro_evidence/forward_seal_m5.json":
        "09181ecaf9e30cde62c5203b524cb458d808a07c24029e961e32bb25c474afd1",
}
SOURCE_BLOCKS = (
    ("market_inputs_and_state", "bool pzSupported", "bool pzLight"),
    ("source_helpers", "f_pzNumber(float value)", "f_pzEmit(PzZone zone, string state)"),
    ("event_and_alert_payload", "f_pzEmit(PzZone zone, string state)", "    if pzShow and pzMarks"),
    ("closed_bar_lifecycle_and_alert_flow", "if pzBar\n", "// Two bands"),
)
PROTECTED_INPUTS = ("pzNotify", "pzQa", "pzReview", "pzReviewAt")
NONCODE = re.compile(r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|//[^\n]*''')
RESTRICTED_CALL = re.compile(r"\b(request\.\w+|strategy(?:\.\w+)?|alert(?:condition)?)\s*\(")
INDICATOR = re.compile(r'^indicator\("(?:\\.|[^"\\])*",\s*shorttitle="(?:\\.|[^"\\])*"(.*)\)$', re.MULTILINE)


def digest(raw):
    return sha256(raw).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def block(source, start, end):
    require(source.count(start) == 1 and source.count(end) == 1, f"Expected unique source anchors: {start!r}, {end!r}")
    left, right = source.index(start), source.index(end)
    require(left < right, f"Source anchors in wrong order: {start!r}, {end!r}")
    return source[left:right]


def indicator_settings(source):
    parts = INDICATOR.findall(source)
    require(len(parts) == 1 and source.startswith("//@version=6\n"), "Missing single Pine v6 indicator declaration")
    return parts[0]


def protected_input(source, name):
    matches = re.findall(rf"^(?:bool|int) {name} = input\..*$", source, flags=re.MULTILINE)
    require(len(matches) == 1, f"Expected single protected input {name}")
    return matches[0]


def source_checks():
    pins = {}
    for path, expected in PROTECTED.items():
        actual = digest(path.read_bytes())
        require(actual == expected, f"Protected SHA-256 mismatch: {path.relative_to(ROOT)}")
        pins[path.relative_to(ROOT).as_posix()] = actual
    old = BASELINE.read_bytes().decode("utf-8")
    new_bytes = CANDIDATE.read_bytes()
    new = new_bytes.decode("utf-8")
    require(indicator_settings(old) == indicator_settings(new), "Changed indicator calculation or object limits")
    for name in PROTECTED_INPUTS:
        require(protected_input(old, name) == protected_input(new, name), f"Changed protected input {name}")
    sections = {}
    for name, start, end in SOURCE_BLOCKS:
        before, after = (block(source, start, end) for source in (old, new))
        require(before == after, f"Changed protected source block: {name}")
        sections[name] = {"start": start, "end": end, "sha256": digest(before.encode("utf-8"))}
    calls = [Counter(RESTRICTED_CALL.findall(NONCODE.sub(" ", source))) for source in (old, new)]
    require(calls[0] == calls[1] == {"alert": 1}, "Changed request, strategy or alert call inventory")
    return {"status": "MATCH", "baseline_commit": BASELINE_COMMIT,
            "original_remote_premium_commit": REMOTE_CANDIDATE_COMMIT,
            "candidate": CANDIDATE.relative_to(ROOT).as_posix(), "candidate_sha256": digest(new_bytes),
            "protected_sha256": pins, "byte_identical_blocks": sections, "protected_inputs": list(PROTECTED_INPUTS),
            "indicator_settings": indicator_settings(new), "restricted_call_counts": dict(calls[1])}


def extent(items, key):
    return [utc(items[0][key]), utc(items[-1][key])] if items else None


def describe(path, run):
    return {"file": path.as_posix(), "file_sha256": run.file_sha256,
            "loaded_bars": len(run.bars), "first_source_index": run.market[0]["index"],
            "first_loaded_open_utc": utc(run.bars[0]["time"]),
            "last_confirmed_history_index": run.last_confirmed_index,
            "last_confirmed_history_close_utc": utc(run.bars[run.last_confirmed_index]["time_close"]),
            "last_logged_close_utc": utc(run.bars[-1]["time_close"]),
            "closed_events": len(run.events), "event_close_window_utc": extent(run.events, "known_ms"),
            "events_by_state": dict(Counter(row["state"] for row in run.events)),
            "alert_intent_batches": len(run.alert_intents),
            "alert_intent_close_window_utc": extent([row for _, row in run.alert_intents], "known_ms"),
            "alert_intent_events": sum(len(row["events"]) for _, row in run.alert_intents)}


def first_difference(before, after):
    for index, (left, right) in enumerate(zip_longest(before, after), 1):
        if left != right:
            return {"row": index, "before": left, "after": right}
    return None


def native_frame(directory, frame):
    paths = tuple(directory / side / f"zone-M{frame}.csv" for side in ("before", "after"))
    if missing := [path.as_posix() for path in paths if not path.is_file()]:
        return {"status": "MISSING_EVIDENCE", "files": missing}
    before, after = (read_zone_native(path, frame, SYMBOL) for path in paths)
    fields = ("bars", "market", "events", "alert_intents")
    exact = {field: first_difference(getattr(before, field), getattr(after, field)) for field in fields}
    exact["last_confirmed_history_index"] = (None if before.last_confirmed_index == after.last_confirmed_index
                                              else {"before": before.last_confirmed_index,
                                                    "after": after.last_confirmed_index})
    intents = {side: alert_compare(run, frame, SYMBOL) for side, run in (("before", before), ("after", after))}
    enough_history = before.market[0]["index"] == 0 and before.last_confirmed_index >= 1198
    complete = enough_history and before.events and all(result["status"] == "MATCH" for result in intents.values())
    status = "MISMATCH" if any(value is not None for value in exact.values()) else (
        "MATCH" if complete else "INCONCLUSIVE")
    return {"status": status, "before": describe(paths[0].relative_to(directory), before),
            "after": describe(paths[1].relative_to(directory), after),
            "raw_csv_byte_identical": before.file_sha256 == after.file_sha256,
            "exact_streams": {field: value is None for field, value in exact.items()},
            "first_differences": {field: value for field, value in exact.items() if value is not None},
            "native_alert_payloads": intents}


def capture(check):
    try:
        return check()
    except (ValueError, OSError, KeyError, TypeError, csv.Error) as exc:
        return {"status": "INVALID_EVIDENCE", "reason": str(exc)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-dir", type=Path, help="Directory with before/ and after/ zone-M5/M15/M30.csv")
    parser.add_argument("--output", type=Path, help="Write JSON evidence to this file instead of stdout")
    args = parser.parse_args()
    result = {"source": capture(source_checks)}
    if args.native_dir is not None:
        result["native"] = {f"M{frame}": capture(lambda: native_frame(args.native_dir, frame)) for frame in FRAMES}
    passed = result["source"]["status"] == "MATCH" and all(
        row["status"] == "MATCH" for row in result.get("native", {}).values())
    result["status"] = "MATCH" if passed and args.native_dir else "SOURCE_ONLY_MATCH" if passed else "NOT_VERIFIED"
    evidence = json.dumps(result, ensure_ascii=False, indent=2, default=str, allow_nan=False) + "\n"
    if args.output is None:
        print(evidence, end="")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(evidence, encoding="utf-8", newline="\n")
    return int(not passed)


if __name__ == "__main__":
    raise SystemExit(main())
