"""Audit the unified Pine source without invoking a compiler."""

import argparse
from collections import Counter, defaultdict
from hashlib import sha256
import json
from pathlib import Path
import re
import subprocess


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
BASELINE_COMMIT = "5528556584910faa75f1b76d2d96f14d3852e6e7"
ORIGINAL_LITE_COMMIT = "0041ed5291fd78393e60647f4cafea93b7954db2"
BASELINE = ROOT / "research/final_htf_data_integrity/QIRVANTA_GOLD_PRO_HTF_DISPLAY_FIX.pine"
CANDIDATE = HERE / "QIRVANTA_GOLD_PRO_LUMINOUS_LITE.pine"
FORWARD = ROOT / "docs/gold_pro_evidence/forward_seal_m5.json"
PINNED_SHA256 = {
    BASELINE: "a9955a1ed3f402f1b8674b3ef2fabfc3d6578bb20f61d8a7d12ade5f527353a2",
    FORWARD: "09181ecaf9e30cde62c5203b524cb458d808a07c24029e961e32bb25c474afd1",
}
ORIGINAL_SHA256 = "e25f90017b6f90891fdf16588e50103c2248636b4821c96d2603d81fa189b827"
DRAWING_PREFIXES = {"box", "label", "line", "linefill", "polyline", "table"}
PLOT_CALLS = {"barcolor", "bgcolor", "fill", "hline", "plot", "plotarrow", "plotbar",
              "plotcandle", "plotchar", "plotshape"}
QUOTAS = ("max_boxes_count", "max_labels_count", "max_lines_count")
PINE_TOKEN = re.compile(
    r"""(?P<comment>//[^\n]*)|(?P<space>\s+)"""
    r"""|(?P<string>"(?:[^"\\\r\n]|\\[^\r\n])*"|'(?:[^'\\\r\n]|\\[^\r\n])*')"""
    r"""|(?P<identifier>[A-Za-z_][A-Za-z_0-9]*)"""
    r"""|(?P<number>(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?)"""
    r"""|(?P<color>#[0-9a-fA-F]{8}\b|#[0-9a-fA-F]{6}\b)"""
    r"""|(?P<operator>=>|:=|[+*/%<>!\-]=|==|[=<>+*/%\-.,:?()[\]])"""
)
MUTABLE_ARGUMENT = re.compile(
    r'\b(?:shorttitle="(?:[^"\\]|\\.)*"|(?:max_boxes_count|max_labels_count|max_lines_count)=\d+)'
)
OPENING = {"(": ")", "[": "]"}
BLOCK_STARTERS = {"if", "else", "for", "while", "type", "enum", "method"}


def require(condition, detail):
    if not condition:
        raise ValueError(detail)


def digest(raw):
    return sha256(raw).hexdigest()


def snapshot(commit, path):
    return subprocess.check_output(
        ["git", "show", f"{commit}:{path.relative_to(ROOT).as_posix()}"], cwd=ROOT
    )


def split_source(baseline, candidate):
    """Return the verified declaration and the trailing Lite-only source."""
    protected = baseline.splitlines(keepends=True)
    lines = candidate.splitlines(keepends=True)
    declarations = [i for i, row in enumerate(protected) if row.startswith(b"indicator(")]
    require(declarations == [6] and len(lines) >= len(protected), "Unexpected indicator layout")
    header = declarations[0]
    require(lines[:header] == protected[:header] and lines[header + 1:len(protected)] == protected[header + 1:],
            "Protected UNIFIED source differs outside the indicator declaration")
    old, new = (line.decode("utf-8") for line in (protected[header], lines[header]))
    require(new.startswith('indicator("'), "Missing indicator declaration")

    def settings_without_display_options(declaration):
        rest = re.sub(r'^indicator\("(?:[^"\\]|\\.)*"(?=,)', "indicator(TITLE", declaration)
        return MUTABLE_ARGUMENT.sub("DRAWING_OPTION", rest)

    require(settings_without_display_options(new) == settings_without_display_options(old),
            "Indicator settings differ outside title, shorttitle or drawing quotas")
    quotas = {}
    for name in QUOTAS:
        old_value = int(re.search(rf"\b{name}=(\d+)\b", old)[1])
        new_value = re.search(rf"\b{name}=(\d+)\b", new)
        require(new_value is not None and old_value <= int(new_value[1]) <= 500, f"Unexpected quota for {name}")
        quotas[name] = int(new_value[1])
    for name in ("max_bars_back", "calc_bars_count"):
        quotas[name] = int(re.search(rf"\b{name}=(\d+)\b", new)[1])
    return quotas, b"".join(lines[len(protected):]), len(protected) - 1


def lex(source):
    """Consume every byte of decoded source and validate typed delimiter nesting."""
    tokens = []
    stack = []
    pos = 0
    line = column = 1
    while pos < len(source):
        match = PINE_TOKEN.match(source, pos)
        require(match is not None, f"Unrecognized token or unterminated string at {line}:{column}")
        word = match[0]
        kind = match.lastgroup
        if kind not in {"comment", "space"}:
            tokens.append((kind, word, line, column, len(stack)))
            if kind == "operator":
                if word in OPENING:
                    stack.append((word, line, column))
                elif word in OPENING.values():
                    require(stack and OPENING[stack[-1][0]] == word, f"Unmatched {word} at {line}:{column}")
                    stack.pop()
        added_lines = word.count("\n")
        line += added_lines
        column = len(word.rsplit("\n", 1)[1]) + 1 if added_lines else column + len(word)
        pos = match.end()
    require(not stack, f"Unclosed delimiters: {stack}")
    return tokens


def source_calls(tokens):
    calls = Counter()
    values = [token[1] for token in tokens]
    for index, token in enumerate(tokens):
        if token[0] != "identifier" or (index and values[index - 1] == "."):
            continue
        end = index + 1
        while end + 1 < len(tokens) and values[end] == "." and tokens[end + 1][0] == "identifier":
            end += 2
        if end < len(tokens) and values[end] == "(":
            calls["".join(values[index:end])] += 1
    return calls


def inventory(raw, whole=False):
    source = raw.decode("utf-8")
    if whole:
        require(source.startswith("//@version=6\n"), "Source must declare Pine v6")
    tokens = lex(source)
    calls = source_calls(tokens)
    code_rows = defaultdict(list)
    for token in tokens:
        code_rows[token[2]].append(token)

    block_starters = Counter()
    loops = Counter()
    function_headers = 0
    variables_by_indent = Counter()
    for row in code_rows.values():
        first = row[0]
        if first[4]:
            continue
        # Pine uses four-space indentation for local blocks. This census does
        # not try to recover line wrapping or construct compiler scopes.
        if (first[3] - 1) % 4:
            continue
        word = first[1]
        if word in BLOCK_STARTERS:
            block_starters[word] += 1
        elif any(token[1] == "switch" and token[0] == "identifier" and token[4] == 0 for token in row):
            block_starters["switch"] += 1
        if word in {"for", "while"}:
            loops[word] += 1
        elif len(row) > 2 and row[1][1] == "(" and any(token[1] == "=>" and token[4] == 0 for token in row):
            function_headers += 1
        if any(token[1] == "=" and token[4] == 0 for token in row):
            variables_by_indent[first[3] - 1] += 1

    drawings = {name: count for name, count in calls.items() if name.partition(".")[0] in DRAWING_PREFIXES}
    protected_calls = {name: count for name, count in calls.items()
                       if name in {"alert", "alertcondition", "strategy"} or name.startswith(("request.", "strategy."))}
    if whole:
        require(calls["indicator"] == 1 and not calls["library"] and not calls["strategy"],
                "Expected one indicator, no library or strategy declaration")
    return {
        "bytes": len(raw),
        "lines": len(source.splitlines()),
        "code_lines": len(code_rows),
        "lexical_tokens": len(tokens),
        "function_header_candidates": function_headers,
        "block_header_candidates": dict(block_starters),
        "loop_sites": dict(loops),
        "max_delimiter_depth": max((token[4] for token in tokens), default=0),
        "global_assignment_lines": variables_by_indent[0],
        "drawing_call_sites": dict(sorted(drawings.items())),
        "drawing_constructor_sites": sum(count for name, count in drawings.items() if name.endswith(".new")),
        "plot_call_sites": {name: count for name, count in sorted(calls.items()) if name in PLOT_CALLS},
        "protected_call_sites": dict(sorted(protected_calls.items())),
    }


def audit():
    pins = {}
    for path, expected in PINNED_SHA256.items():
        raw = path.read_bytes()
        require(digest(raw) == expected, f"Protected file SHA-256 mismatch: {path.relative_to(ROOT)}")
        pins[path.relative_to(ROOT).as_posix()] = expected
    baseline = BASELINE.read_bytes()
    require(snapshot(BASELINE_COMMIT, BASELINE) == baseline, "Protected baseline differs from pinned commit")
    original = snapshot(ORIGINAL_LITE_COMMIT, CANDIDATE)
    require(digest(original) == ORIGINAL_SHA256, "Original Lite source SHA-256 mismatch")
    candidate = CANDIDATE.read_bytes()
    old_quotas, old_tail, protected_lines = split_source(baseline, original)
    new_quotas, new_tail, _ = split_source(baseline, candidate)
    metrics = {
        "baseline": inventory(baseline, whole=True),
        "original_lite": inventory(original, whole=True),
        "candidate": inventory(candidate, whole=True),
        "original_lite_layer": inventory(old_tail),
        "candidate_lite_layer": inventory(new_tail),
    }
    require(metrics["baseline"]["protected_call_sites"] == metrics["candidate"]["protected_call_sites"],
            "Changed alert, request or strategy call sites")
    require(not metrics["candidate_lite_layer"]["protected_call_sites"], "Lite renderer adds protected call sites")
    return {
        "status": "STATIC_MATCH",
        "baseline_commit": BASELINE_COMMIT,
        "original_lite_commit": ORIGINAL_LITE_COMMIT,
        "original_lite_sha256": ORIGINAL_SHA256,
        "candidate_sha256": digest(candidate),
        "protected_sha256": pins,
        "byte_identical_protected_lines_excluding_declaration": protected_lines,
        "declaration_quotas": {"original_lite": old_quotas, "candidate": new_quotas},
        "metrics": metrics,
        "lexical_delimiters_and_strings": "BALANCED",
        "scope": ("Full-source lexical scan and byte comparison. Counts describe source, not compiled Pine tokens "
                  "or compiler scope size. This does not predict compilation, runtime or native parity."),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Write the JSON report to this path.")
    args = parser.parse_args()
    try:
        report = audit()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        report = {"status": "NOT_VERIFIED", "error": str(exc)}
    output = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is None:
        print(output, end="")
    else:
        args.output.write_text(output, encoding="utf-8", newline="\n")
    return 0 if report["status"] == "STATIC_MATCH" else 1


if __name__ == "__main__":
    raise SystemExit(main())
