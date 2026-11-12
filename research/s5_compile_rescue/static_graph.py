"""Static Pine v6 dependency and protected-call audit for the S5 compile rescue."""

import argparse
from collections import Counter, defaultdict
import difflib
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess


BASELINE_COMMIT = "5528556584910faa75f1b76d2d96f14d3852e6e7"
BASELINE_PATH = "research/final_htf_data_integrity/QIRVANTA_GOLD_PRO_HTF_DISPLAY_FIX.pine"
BASELINE_SHA = "a9955a1ed3f402f1b8674b3ef2fabfc3d6578bb20f61d8a7d12ade5f527353a2"
SEAL_PATH = "docs/gold_pro_evidence/forward_seal_m5.json"
SEAL_SHA = "09181ecaf9e30cde62c5203b524cb458d808a07c24029e961e32bb25c474afd1"
PINE_TOKEN = re.compile(
    r"""(?P<comment>//[^\n]*)|(?P<space>\s+)"""
    r"""|(?P<string>"(?:[^"\\\r\n]|\\[^\r\n])*"|'(?:[^'\\\r\n]|\\[^\r\n])*')"""
    r"""|(?P<identifier>[A-Za-z_][A-Za-z_0-9]*)"""
    r"""|(?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?)"""
    r"""|(?P<color>#[0-9a-fA-F]{8}\b|#[0-9a-fA-F]{6}\b)"""
    r"""|(?P<operator>=>|:=|[+*/%<>!\-]=|==|[=<>+*/%\-.,:?()[\]])"""
)
OPENING = {"(": ")", "[": "]"}
PROTECTED_NAMES = {
    "alert", "alertcondition", "barcolor", "bgcolor", "fill", "hline",
    "indicator", "library", "plot", "plotarrow", "plotbar", "plotcandle",
    "plotchar", "plotshape", "strategy",
}
PROTECTED_NAMESPACES = ("request.", "strategy.", "input.")
DRAWING_NAMESPACES = ("box.", "label.", "line.", "linefill.", "polyline.", "table.")
GLOBAL = "<global>"
WICK_ONLY_REQUESTS = tuple(
    ["syminfo", ".", "tickerid", ",", frame, ",", "f_wfHtfContext", "(", ")", ",",
     "gaps", "=", "barmerge", ".", "gaps_off", ",", "lookahead", "=", "barmerge", ".", "lookahead_on"]
    for frame in ('"60"', '"240"')
)


def check(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return sha256(data).hexdigest()


def lex(raw):
    text = raw.decode("utf-8")
    check(text.splitlines()[0:1] == ["//@version=6"], "Missing Pine v6 directive")
    tokens, closing, stack = [], {}, []
    pos = 0
    line = column = 1
    while pos < len(text):
        match = PINE_TOKEN.match(text, pos)
        check(match is not None, f"Unrecognized token or unfinished string at {line}:{column}")
        word, kind = match[0], match.lastgroup
        if kind not in {"comment", "space"}:
            if kind == "operator" and word in OPENING.values():
                check(stack and OPENING[stack[-1][0]] == word, f"Unexpected {word} at {line}:{column}")
                _, opening_index = stack.pop()
                closing[opening_index] = len(tokens)
            tokens.append((word, kind, line, column, len(stack)))
            if kind == "operator" and word in OPENING:
                stack.append((word, len(tokens) - 1))
        newlines = word.count("\n")
        line += newlines
        column = len(word.rsplit("\n", 1)[1]) + 1 if newlines else column + len(word)
        pos = match.end()
    check(not stack, f"Unclosed delimiters at lines {[tokens[index][2] for _, index in stack]}")
    return tokens, closing


def functions(tokens, closing):
    """Find top-level function declarations and map their tokens to each body."""
    headers = {}
    owners = [GLOBAL] * len(tokens)
    owner = GLOBAL
    pending = GLOBAL
    for index, (word, kind, line, column, depth) in enumerate(tokens):
        if column == 1 and depth == 0:
            owner = GLOBAL
            pending = GLOBAL
            is_method = word == "method"
            name_index = index + int(is_method)
            opening = name_index + 1
            end = closing.get(opening, -2) + 1
            if (kind == "identifier" and 0 <= end < len(tokens)
                    and tokens[opening][0] == "(" and tokens[end][0] == "=>"):
                name = tokens[name_index][0]
                # Named functions are unique in this source. Reject an overload
                # instead of conflating two independent implementations.
                check(name not in headers, f"Duplicate function {name} at line {line}")
                headers[name] = (line, name_index, end)
                pending = name
        if pending != GLOBAL and index > headers[pending][2]:
            owner = pending
        owners[index] = owner
    return headers, owners


def calls(tokens, closing):
    for index, (word, kind, line, _, _) in enumerate(tokens):
        if kind != "identifier" or (index and tokens[index - 1][0] == "."):
            continue
        name = word
        end = index + 1
        while end + 1 < len(tokens) and tokens[end][0] == "." and tokens[end + 1][1] == "identifier":
            name += "." + tokens[end + 1][0]
            end += 2
        if end in closing and tokens[end][0] == "(":
            yield name, line, index, end, closing[end]


def protected_invocation(name, arguments):
    if name != "indicator":
        return [argument[0] for argument in arguments]
    # Keep the execution and drawing settings fixed. The test candidate can
    # have a distinct displayed title and short title.
    result = []
    for word, kind, _, _, depth in arguments:
        if depth == 1 and kind == "string":
            previous = result[-2:] if len(result) >= 2 else result
            if not result or previous == ["shorttitle", "="]:
                word = "<display title>"
        result.append(word)
    return result


def inventory(raw):
    tokens, closing = lex(raw)
    headers, owners = functions(tokens, closing)
    declarations = {header[1] for header in headers.values()}
    graph = {owner: defaultdict(list) for owner in [GLOBAL, *headers]}
    counts, protected, drawings = Counter(), defaultdict(list), Counter()
    for name, line, index, opening, end in calls(tokens, closing):
        if index in declarations:
            continue
        counts[name] += 1
        graph[owners[index]][name].append(line)
        if name in PROTECTED_NAMES or name.startswith(PROTECTED_NAMESPACES):
            protected[name].append(protected_invocation(name, tokens[opening + 1:end]))
        if name.startswith(DRAWING_NAMESPACES):
            drawings[name] += 1

    check(counts["indicator"] == 1 and not counts["library"] and not counts["strategy"],
          "Expected one indicator declaration and no library or strategy declaration")
    check(not any(name.startswith("strategy.") for name in counts), "Found strategy call in indicator")
    reachable, pending = set(), list(graph[GLOBAL])
    while pending:
        name = pending.pop()
        if name in headers and name not in reachable:
            reachable.add(name)
            pending.extend(graph[name])

    sources = defaultdict(list)
    identifier_sites = defaultdict(set)
    for index, (word, kind, line, _, _) in enumerate(tokens):
        if kind == "identifier" and index not in declarations:
            identifier_sites[word].add(line)
        if owners[index] != GLOBAL:
            sources[owners[index]].append(word)
    loops = Counter(word for word, kind, _, column, depth in tokens
                    if kind == "identifier" and word in {"for", "while"} and depth == 0 and (column - 1) % 4 == 0)
    return {
        "sha256": digest(raw),
        "lines": len(raw.splitlines()),
        "bytes": len(raw),
        "lexical_tokens": len(tokens),
        "loop_sites": dict(sorted(loops.items())),
        "function_declarations": len(headers),
        "functions_reachable_from_global": len(reachable),
        "unreachable_functions_syntactically": sorted(headers.keys() - reachable),
        "function_lines": {name: header[0] for name, header in sorted(headers.items())},
        "function_body_digests": {name: digest("\0".join(sources[name]).encode()) for name in sorted(headers)},
        "function_signature_digests": {
            name: digest("\0".join(token[0] for token in tokens[start:end]).encode())
            for name, (_, start, end) in sorted(headers.items())
        },
        "user_call_graph": {
            owner: {target: lines for target, lines in sorted(targets.items()) if target in headers}
            for owner, targets in sorted(graph.items())
        },
        "protected_call_counts": {name: counts[name] for name in sorted(protected)},
        "drawing_call_counts": dict(sorted(drawings.items())),
        "log_call_counts": {name: count for name, count in sorted(counts.items()) if name.startswith("log.")},
    }, protected, identifier_sites


def compare(baseline_raw, candidate_raw):
    baseline, baseline_calls, _ = inventory(baseline_raw)
    candidate, candidate_calls, candidate_references = inventory(candidate_raw)
    before, after = baseline["function_body_digests"], candidate["function_body_digests"]
    removed = sorted(before.keys() - after.keys())
    added = sorted(after.keys() - before.keys())
    modified = sorted(name for name in before.keys() & after.keys() if before[name] != after[name])
    survivors = {name: sorted(candidate_references[name]) for name in removed if candidate_references[name]}
    changed_signatures = sorted(
        name for name in before.keys() & after.keys()
        if baseline["function_signature_digests"][name] != candidate["function_signature_digests"][name]
    )
    # Permit only the exact pair of Wick requests whose final values have no readers.
    # Canonical, strategy and Pressure HTF calls stay pinned.
    allowed_wick_calls = []
    name = "request.security"
    if all(baseline_calls[name].count(request) == 1 and request not in candidate_calls[name]
           for request in WICK_ONLY_REQUESTS):
        baseline_calls[name] = [request for request in baseline_calls[name] if request not in WICK_ONLY_REQUESTS]
        allowed_wick_calls = [
            {"function": name, "frame": request[4].strip('"'), "expression": "f_wfHtfContext()"}
            for request in WICK_ONLY_REQUESTS
        ]
    changed_calls = sorted(name for name in baseline_calls.keys() | candidate_calls.keys()
                           if baseline_calls[name] != candidate_calls[name])
    old_lines, new_lines = baseline_raw.splitlines(), candidate_raw.splitlines()
    changes = [
        {"kind": op, "baseline_start": i + 1, "baseline_end": j, "candidate_start": k + 1, "candidate_end": m}
        for op, i, j, k, m in difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes()
        if op != "equal"
    ]
    unexpected_bodies = sorted(set(modified) - {"f_tvCandidate"})
    passed = not (survivors or changed_calls or changed_signatures or added or unexpected_bodies)
    return {
        "status": "STATIC_CHECKS_PASSED" if passed else "STATIC_CHECKS_FAILED",
        "baseline_commit": BASELINE_COMMIT,
        "baseline": baseline,
        "candidate": candidate,
        "removed_functions": removed,
        "added_functions": added,
        "executable_token_modified_functions": modified,
        "unexpected_function_body_changes": unexpected_bodies,
        "modified_function_signatures": changed_signatures,
        "removed_function_name_references": survivors,
        "changed_protected_calls": changed_calls,
        "permitted_dead_wick_request_removals": allowed_wick_calls,
        "changed_line_intervals": changes,
        "scope": (
            "Source-level lexical and direct-call analysis; global reachability treats every conditional branch as "
            "possible. Verify constant-guard proofs, global data/state dependencies, Pine scopes/types, and native "
            "parity independently. Token counts are not Pine IL size or a compilation forecast."
        ),
    }



def compare_combined(raw):
    from build_slim import assemble

    slim_raw = assemble().encode()
    original_limits = b"max_labels_count=42, max_lines_count=16, max_boxes_count=6"
    combined_limits = b"max_labels_count=60, max_lines_count=26, max_boxes_count=12"
    check(slim_raw.count(original_limits) == 1, "Unexpected slim object quotas")
    prefix = slim_raw.replace(original_limits, combined_limits)
    check(raw.startswith(prefix), "Combined core changed outside the drawing quotas")
    zone = raw[len(prefix):]
    slim, slim_calls, _ = inventory(slim_raw)
    candidate, calls, _ = inventory(raw)
    retained = set(slim["function_lines"])
    check(set(candidate["function_lines"]) - retained == {"f_lzStatus", "f_lzEvent"},
          "Unexpected new functions in zone overlay")
    check(all(slim["function_body_digests"][name] == candidate["function_body_digests"][name]
              for name in retained), "Changed a slim function body")
    for name in slim_calls.keys() | calls.keys():
        if name == "indicator":
            continue
        expected = slim_calls[name]
        actual = calls[name]
        check(actual[:len(expected)] == expected and
              (name.startswith("input.") or actual == expected), f"Changed protected {name} calls")
    check(b"if lzShow and rcViewPaintBar" in zone and b"bar_index - zone.born >= 24" in zone,
          "Missing confirmed zone lifecycle guards")
    return {
        "status": "STATIC_CHECKS_PASSED",
        "slim_sha256": slim["sha256"],
        "combined_sha256": candidate["sha256"],
        "only_core_change": "drawing quotas 42/16/6 to 60/26/12; all other core bytes identical",
        "slim_lines": slim["lines"],
        "combined_lines": candidate["lines"],
        "zone_lines": len(zone.splitlines()),
        "slim_lexical_tokens": slim["lexical_tokens"],
        "combined_lexical_tokens": candidate["lexical_tokens"],
        "combined_calls": candidate["protected_call_counts"],
    }

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, help="Omit to audit the pinned baseline alone.")
    parser.add_argument("--output", type=Path, help="Write the audit as JSON.")
    parser.add_argument("--combined", action="store_true", help="Verify a zones build against the exact slim source.")
    args = parser.parse_args()
    if args.combined and not args.candidate:
        parser.error("--combined requires --candidate")
    try:
        baseline = subprocess.check_output(["git", "show", f"{BASELINE_COMMIT}:{BASELINE_PATH}"])
        check(digest(baseline) == BASELINE_SHA, "Pinned baseline SHA-256 mismatch")
        seal = Path(SEAL_PATH).read_bytes()
        check(digest(seal) == SEAL_SHA, "Protected forward seal SHA-256 mismatch")
        if args.combined:
            report = compare_combined(args.candidate.read_bytes())
        else:
            report = compare(baseline, args.candidate.read_bytes() if args.candidate else baseline)
        report["forward_seal_sha256"] = digest(seal)
    except (IndexError, OSError, ValueError, subprocess.CalledProcessError) as exc:
        report = {"status": "STATIC_CHECKS_FAILED", "error": str(exc)}
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(output, encoding="utf-8", newline="\n")
    else:
        print(output, end="")
    return int(report["status"] != "STATIC_CHECKS_PASSED")


if __name__ == "__main__":
    raise SystemExit(main())
