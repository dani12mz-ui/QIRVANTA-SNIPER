"""Audit the Luminous renderer and compare matched Premium and Luminous Pine Logs."""

import argparse
from collections import Counter
import csv
import json
from pathlib import Path

from research.precision_buy_sell_zones_premium import qa as premium


HERE = Path(__file__).resolve().parent
BASELINE = premium.CANDIDATE
CANDIDATE = HERE / "QIRVANTA_LUMINOUS_ZONES.pine"
BASELINE_COMMIT = "eb0428a13e626ec233bed85752e81028890e2d8f"
ORIGINAL_CANDIDATE_COMMIT = "e9a51963d4d67bd2fe4f16e0a543c76eca3f3f87"
PROTECTED = {**premium.PROTECTED, BASELINE: "124db7b8324cd1070a8f380372501d28c654dd965003cf11b3250a4ca818cd88"}
SOURCE_BLOCKS = (
    *premium.SOURCE_BLOCKS[:2],
    ("event_and_alert_payload", "f_pzEmit(PzZone zone, string state)", "            array.push(pzAlertItems, message)\n"),
    premium.SOURCE_BLOCKS[-1],
)
NATIVE_TAGS = {"PZ_MARKET", "PZ_EVENT", "PZ_ALERT_INTENT", "PZ_END"}


def source_checks():
    pins = {}
    for path, expected in PROTECTED.items():
        actual = premium.digest(path.read_bytes())
        premium.require(actual == expected, f"Protected SHA-256 mismatch: {path.relative_to(premium.ROOT)}")
        pins[path.relative_to(premium.ROOT).as_posix()] = actual
    old = BASELINE.read_bytes().decode("utf-8")
    new_bytes = CANDIDATE.read_bytes()
    new = new_bytes.decode("utf-8")
    old_settings = premium.indicator_settings(old)
    settings = premium.indicator_settings(new)
    premium.require(settings == old_settings.replace("max_boxes_count=2", "max_boxes_count=4, max_lines_count=8"),
                    "Changed calculation settings or rendering limits beyond the four boxes and eight rails")
    for name in premium.PROTECTED_INPUTS:
        premium.require(premium.protected_input(old, name) == premium.protected_input(new, name),
                        f"Changed protected input {name}")
    sections = {}
    for name, start, end in SOURCE_BLOCKS:
        before, after = (premium.block(source, start, end) for source in (old, new))
        premium.require(before == after, f"Changed protected source block: {name}")
        # The payload anchor is the last statement in the protected alert body.
        if name == "event_and_alert_payload":
            before += end
        sections[name] = {"start": start, "end": end, "sha256": premium.digest(before.encode("utf-8"))}
    calls = [Counter(premium.RESTRICTED_CALL.findall(premium.NONCODE.sub(" ", source))) for source in (old, new)]
    premium.require(calls[0] == calls[1] == {"alert": 1}, "Changed request, strategy or alert call inventory")
    return {"status": "MATCH", "baseline_commit": BASELINE_COMMIT,
            "original_candidate_commit": ORIGINAL_CANDIDATE_COMMIT,
            "candidate": CANDIDATE.relative_to(premium.ROOT).as_posix(), "candidate_sha256": premium.digest(new_bytes),
            "protected_sha256": pins, "byte_identical_blocks": sections,
            "protected_inputs": list(premium.PROTECTED_INPUTS), "indicator_settings": settings,
            "restricted_call_counts": dict(calls[1])}


def raw_native_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        rows = csv.DictReader(stream, strict=True)
        premium.require(rows.fieldnames == ["Date", "Message"], f"{path.name}: unexpected Pine Logs CSV format")
        return tuple((row["Date"], line) for row in rows for line in row["Message"].splitlines()
                     if line.partition("|")[0] in NATIVE_TAGS)


def native_frame(directory, frame):
    result = premium.native_frame(directory, frame)
    if "before" not in result or "after" not in result:
        return result
    paths = tuple(directory / side / f"zone-M{frame}.csv" for side in ("before", "after"))
    before, after = (raw_native_rows(path) for path in paths)
    difference = premium.first_difference(before, after)
    result["raw_pine_rows"] = {"status": "MATCH" if difference is None else "MISMATCH",
                               "before_rows": len(before), "after_rows": len(after),
                               "first_difference": difference,
                               "scope": "PZ_MARKET, PZ_EVENT, PZ_ALERT_INTENT and PZ_END; original timestamps and messages"}
    if difference is not None:
        result["status"] = "MISMATCH"
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-dir", type=Path, help="Directory with before/ and after/ zone-M5/M15/M30.csv")
    parser.add_argument("--output", type=Path, help="Write JSON evidence to this file instead of stdout")
    args = parser.parse_args()
    result = {"source": premium.capture(source_checks)}
    if args.native_dir is not None:
        result["native"] = {f"M{frame}": premium.capture(lambda: native_frame(args.native_dir, frame))
                            for frame in premium.FRAMES}
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
