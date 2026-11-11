"""Compare V3 with the pinned Wick sources and ordered native TradingView exports."""

import argparse
from collections import Counter
from decimal import Decimal
from hashlib import sha256
from itertools import zip_longest
import json
from pathlib import Path
import re

from gold_wick.native_audit import END, FRAMES, START, SYMBOL, native_execution, native_rows, require
from unified.tools.build_gold_wick import DECLARATIONS, TARGETS


ROOT = Path(__file__).resolve().parents[1]
REFERENCE_COMMIT = "c7e16f35b52c8fa2051535b0caacb10ae7595fca"
REFERENCE_AUDIT = Path("docs/wick_evidence/native_audit.json")
REFERENCE_AUDIT_SHA = "02262aea6e59d5b1295a1b1c77067ddafd6397ec6ca54f002c2abc42336a0808"
FORWARD_SEAL = Path("docs/gold_pro_evidence/forward_seal_m5.json")
FORWARD_SEAL_SHA = "09181ecaf9e30cde62c5203b524cb458d808a07c24029e961e32bb25c474afd1"
EXPERIMENTAL = Path("unified/experimental")
COMPONENT_SHA = {
    "engine": "b662883f93c8f67e92227592ec4168b55ebd8475a7b227cab78ba0fba49b636b",
    "indicator_execution": "031345026c58ebde0d96b3fc3feb20288eaaa7ee6de01c04ce9df26add5cec9a",
    "strategy_execution": "b2ef9622eb3058552895d351a92580a88b0d34410683ef183eed13a3aa9bc7ec",
}
REFERENCE_SOURCE_SHA = {
    "indicator": "497e4973bdb707660547ede81cb36eb377cceb267970aae6d1d5599e59a8de6e",
    "strategy": "680dff2860c16d116a80029722948366099ee5d838ea837170217e634be681d9",
}
SOURCES = {
    "indicator": EXPERIMENTAL / "QIRVANTA_GOLD_WICK_V3_INTELLIGENCE.pine",
    "strategy": EXPERIMENTAL / "QIRVANTA_GOLD_WICK_V3_FORMATION_STRATEGY.pine",
}
PRESENTATION = "// V3 PRESENTATION ONLY"
VISUAL_DECLARATION_FIELDS = {
    "title", "shorttitle", "max_lines_count", "max_labels_count", "max_boxes_count", "max_polylines_count",
}
PINE_STRING = r'"(?:[^"\\\n]|\\.)*"'
PINE_COMMENT = r"//[^\n]*"
ORDER_CALL = r"strategy\.(?:entry|exit|order|close|close_all|cancel|cancel_all|risk\.\w+)"
BEHAVIOR_CALL = re.compile(
    rf"\b(?:{ORDER_CALL}|alert(?:condition)?|f_wf(?:Event|Exec|LogPlan)|log\.\w+)\s*\("
)
PROTECTED_WRITE = re.compile(r"\bwf[A-Z]\w*(?:\.\w+)*\s*(?::=|[+*/%-]=)")


def digest(data):
    return sha256(data).hexdigest()


def declaration_options(declaration, role):
    match = re.fullmatch(rf"{role}\((.+)\)", declaration, re.DOTALL)
    require(match is not None, f"{role}: unexpected declaration")
    args = match.group(1)
    chunks = re.findall(rf'(?:{PINE_STRING}|[^",])+', args)
    require(",".join(chunks) == args, f"{role}: malformed declaration options")
    options = {}
    for index, chunk in enumerate(chunks):
        text = chunk.strip()
        key, separator, value = text.partition("=")
        if index == 0 and re.fullmatch(PINE_STRING, text):
            key, separator, value = "title", "=", text
        key, value = key.strip(), value.strip()
        require(separator and re.fullmatch(r"\w+", key) and value and key not in options,
                f"{role}: malformed or duplicate declaration option")
        options[key] = value
    require(re.fullmatch(PINE_STRING, options.get("title", "")), f"{role}: missing title")
    return {key: value for key, value in options.items() if key not in VISUAL_DECLARATION_FIELDS}


def without_comments_and_strings(text):
    return re.sub(rf"{PINE_STRING}|{PINE_COMMENT}", " ", text)


def check_source(candidate, role, engine, execution):
    source = candidate.decode("utf-8")
    prefix, separator, presentation = source.partition("\n\n" + PRESENTATION + "\n")
    require(separator and presentation.strip(), f"{role}: missing presentation boundary")
    header, separator, actual = prefix.partition("\n\n")
    require(separator, f"{role}: missing engine boundary")
    expected = engine + "\n\n" + execution
    require(actual == expected, f"{role}: engine or execution prefix differs from Wick")
    version, _, declaration = header.partition("\n")
    require(version == "//@version=6", f"{role}: unexpected Pine version")
    declaration = re.sub(r"\n//[^\n]*\Z", "", declaration)
    settings = declaration_options(declaration, role)
    require(settings == declaration_options(DECLARATIONS[role], role), f"{role}: behavioral declaration differs")

    code = without_comments_and_strings(presentation)
    require(not BEHAVIOR_CALL.search(code), f"{role}: presentation contains a trading, alert or audit call")
    require(not PROTECTED_WRITE.search(code), f"{role}: presentation assigns to Wick engine state")
    aliases = re.findall(r"\b(?:WickPlan|WickFormation)\s+(\w+)\b", code)
    if aliases:
        alias_write = rf"\b(?:{'|'.join(aliases)})\.\w+(?:\.\w+)*\s*(?::=|[+*/%-]=)"
        require(not re.search(alias_write, code), f"{role}: presentation assigns to a Wick object")
    behavior = without_comments_and_strings(expected)
    orders = len(re.findall(rf"\b{ORDER_CALL}\s*\(", behavior))
    disabled = len(re.findall(r"\bdisable_alert\s*=\s*true\b", behavior))
    require(orders == disabled == (5 if role == "strategy" else 0), f"{role}: alert suppression differs")
    require(not re.search(r"\balert(?:condition)?\s*\(", behavior), f"{role}: unexpected custom alert")
    return {
        "status": "VERIFIED", "candidate_sha256": digest(candidate),
        "identical_engine_and_execution_sha256": digest(expected.encode("utf-8")),
        "identical_behavioral_declaration": True, "custom_alert_calls": 0,
        "order_calls_with_alerts_disabled": disabled,
    }


def source_parity(root):
    try:
        fragments = {}
        for name, pinned_sha in COMPONENT_SHA.items():
            path = root / EXPERIMENTAL / "gold_wick" / f"{name}.pineinc"
            data = path.read_bytes()
            require(digest(data) == pinned_sha, f"Wick fragment differs from reference: {name}")
            fragments[name] = data.decode("utf-8").strip()
        for role, pinned_sha in REFERENCE_SOURCE_SHA.items():
            data = (root / TARGETS[role].relative_to(ROOT)).read_bytes()
            require(digest(data) == pinned_sha, f"{role}: compiled Wick reference differs")
            header = ("//@version=6\n" + DECLARATIONS[role] + "\n").encode("utf-8")
            require(data.startswith(header), f"{role}: original generator declaration differs from Wick reference")
        require(digest((root / FORWARD_SEAL).read_bytes()) == FORWARD_SEAL_SHA, "Forward seal differs from reference")
    except (OSError, ValueError) as exc:
        return {"status": "BLOCKED", "problem": str(exc)}

    results = {}
    for role, path in SOURCES.items():
        try:
            candidate = (root / path).read_bytes()
        except OSError:
            results[role] = {"status": "BLOCKED", "problem": f"Missing source: {path.as_posix()}"}
            continue
        try:
            results[role] = check_source(candidate, role, fragments["engine"], fragments[f"{role}_execution"])
            results[role]["reference_sha256"] = REFERENCE_SOURCE_SHA[role]
        except ValueError as exc:
            results[role] = {"status": "INCONCLUSIVE", "problem": str(exc)}
    return {"status": combined_status(results.values()), "roles": results, "forward_seal_sha256": FORWARD_SEAL_SHA}


def first_difference(reference, candidate):
    for index, (expected, actual) in enumerate(zip_longest(reference, candidate), start=1):
        if expected == actual:
            continue
        difference = {"row": index}
        for name, row in (("reference", expected), ("candidate", actual)):
            if row is not None:
                difference[f"{name}_close_ms"], difference[f"{name}_tag"] = row[0], row[1][0]
        if expected is not None and actual is not None:
            difference["close_timestamp_differs"] = expected[0] != actual[0]
            difference["different_field_indexes"] = [
                i for i, (a, b) in enumerate(zip_longest(expected[1], actual[1])) if a != b
            ]
        return difference
    return None


def compare_export(reference, candidate, frame, pinned_sha):
    try:
        reference_sha = digest(reference.read_bytes())
    except OSError:
        return {"status": "BLOCKED", "problem": f"Missing or unreadable baseline export: {reference.name}"}
    try:
        require(reference_sha == pinned_sha, f"{reference.name}: baseline export checksum differs")
        candidate_sha = digest(candidate.read_bytes())
        expected, _, _, baseline_counts = native_rows(reference, frame)
        actual, bars, _, counts = native_rows(candidate, frame)
    except OSError:
        return {"status": "BLOCKED", "problem": f"Missing or unreadable native export: {candidate.name}"}
    except (KeyError, TypeError, ValueError) as exc:
        return {"status": "BLOCKED", "problem": str(exc)}

    result = {"reference_sha256": reference_sha, "candidate_sha256": candidate_sha,
              "raw_export_bytes_equal": reference_sha == candidate_sha,
              "baseline_rows": baseline_counts, "candidate_rows": counts}
    difference = first_difference(expected, actual)
    if difference is not None:
        return {"status": "INCONCLUSIVE", **result, "first_difference": difference}

    # Equality retains the exact decimal representation of every fill and exit.
    try:
        campaigns = native_execution(actual, bars, frame) if counts["WICK_EXEC"] else []
    except (IndexError, KeyError, ValueError) as exc:
        return {"status": "INCONCLUSIVE", **result, "problem": str(exc)}
    execution = Counter(fields[3] for _, fields in actual if fields[0] == "WICK_EXEC")
    net = sum((Decimal(fields[9]) for _, fields in actual
               if fields[0] == "WICK_EXEC" and fields[3] == "CLOSED"), Decimal(0))
    scored_rows = Counter(fields[0] for stamp, fields in actual if START <= stamp <= END)
    return {
        "status": "VERIFIED", **result, "exact_ordered_native_rows": len(actual),
        "first_bar_open_ms": bars[0]["time"], "last_bar_close_ms": bars[-1]["time_close"],
        "scored_rows": scored_rows, "execution_states": execution, "native_campaigns": len(campaigns),
        "closed_campaign_net_usd": str(net),
    }


def combined_status(results):
    statuses = {result["status"] for result in results}
    return "INCONCLUSIVE" if "INCONCLUSIVE" in statuses else "BLOCKED" if "BLOCKED" in statuses else "VERIFIED"


def audit(baseline, candidate, root=ROOT):
    source = source_parity(root)
    report = {"reference_commit": REFERENCE_COMMIT, "reference_audit_sha256": REFERENCE_AUDIT_SHA,
              "symbol": SYMBOL, "scored_close_ms": [START, END], "source_parity": source,
              "native_scope": "All WICK_CTX, WICK_EVT, WICK_PLAN and WICK_EXEC rows in each named native export"}
    if baseline.resolve() == candidate.resolve():
        return {"status": "BLOCKED", **report, "problem": "Baseline and V3 require separate native export directories"}
    try:
        data = (root / REFERENCE_AUDIT).read_bytes()
        require(digest(data) == REFERENCE_AUDIT_SHA, "Native baseline report checksum differs")
        pinned = json.loads(data)
    except (OSError, ValueError) as exc:
        return {"status": "BLOCKED", **report, "problem": str(exc)}

    exports = {}
    for frame in FRAMES:
        for role in SOURCES:
            filename = f"{role}_M{frame}.csv"
            sha = pinned["frames"][str(frame)][f"{role}_sha256"]
            exports[filename] = compare_export(baseline / filename, candidate / filename, frame, sha)
    extras, absent = {}, []
    for variant, evidence in pinned["native_variants_M5"].items():
        filename = f"strategy_M5_{variant}.csv"
        if not (candidate / filename).exists():
            absent.append(filename)
            continue
        extras[filename] = compare_export(baseline / filename, candidate / filename, 5, evidence["sha256"])

    return {"status": combined_status([source, *exports.values(), *extras.values()]), **report,
            "matched_windows": exports, "supplemental_M5_variants": extras,
            "supplemental_not_exported": absent,
            "alert_scope": "Static Pine alert semantics. Runtime alert delivery was not exercised."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = audit(args.baseline, args.candidate)
    with args.out.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write("\n")
    matches = sum(item["status"] == "VERIFIED" for item in report.get("matched_windows", {}).values())
    print(report["status"], f"{matches}/{2 * len(FRAMES)} native windows", f"source {report['source_parity']['status']}")
    return int(report["status"] != "VERIFIED")


if __name__ == "__main__":
    raise SystemExit(main())
