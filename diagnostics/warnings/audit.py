"""Read-only scope/dependency audit of the exact QIRVANTA diagnostic baseline.

This is a narrow source scanner, not a Pine parser/compiler or execution engine.
It deliberately emits the evidence behind its classification. Mutation tests
exercise the hazardous local-history/stateful-call cases as positive controls.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BASELINE = "48e5cddab17ea831bdc277f17f0406329fdfe39c"
ROOTS = ("f_fast", "f_reversal")
FILES = ("inputs", "features", "types_helpers", "decision", "paper_engine", "native_adapter")
BUILTIN_SERIES = {"close", "open", "high", "low", "volume", "time", "time_close", "bar_index"}
TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|//[^\n]*')
HEADER = re.compile(r"^([A-Za-z_]\w*)\((.*?)\)\s*=>", re.MULTILINE)
HISTORY = re.compile(r"\b([A-Za-z_]\w*(?:\.\w+)*)\s*\[([^\]\n]+)\]")
CALL = re.compile(r"\b((?:[A-Za-z_]\w*\.)?[A-Za-z_]\w*)\s*\(")
DECL = re.compile(r"^\s*(?:(var|varip)\s+)?(?:float|int|bool|string|Plan|Setup|Event|Ledger|Paper|EvidenceGate|array<[^>]+>)\s+([A-Za-z_]\w*)\s*(?==)", re.MULTILINE)


def code_only(text: str) -> str:
    return TOKEN.sub(lambda m: " " * len(m.group()), text)


@dataclass(frozen=True)
class Function:
    name: str
    file: str
    line: int
    parameters: frozenset[str]
    code: str


def functions(sources: dict[str, str]) -> dict[str, Function]:
    result = {}
    for filename, text in sources.items():
        clean = code_only(text)
        for match in HEADER.finditer(clean):
            after_header = clean.find("\n", match.end())
            stop = len(clean)
            if after_header >= 0:
                for candidate in re.finditer(r"(?m)^\S[^\n]*", clean[after_header + 1:]):
                    stop = after_header + 1 + candidate.start()
                    break
            params = frozenset(p.strip().split()[-1].split("=")[0] for p in match.group(2).split(",") if p.strip())
            name = match.group(1)
            result[name] = Function(name, filename, clean[:match.start()].count("\n") + 1, params, clean[match.end():stop])
    return result


def global_names(sources: dict[str, str]) -> set[str]:
    names = set(BUILTIN_SERIES)
    for text in sources.values():
        for line in code_only(text).splitlines():
            if not line or line[0].isspace():
                continue
            m = DECL.match(line)
            if m:
                names.add(m.group(2))
            if line.startswith("[") and "] =" in line:
                names.update(x.strip() for x in line[1:line.index("]")].split(","))
    return names


def inspect(sources: dict[str, str], root: str) -> dict:
    all_functions = functions(sources)
    global_series = global_names(sources)
    queue, seen, evidence = [root], set(), []
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        fn = all_functions[name]
        locals_ = {m.group(2) for m in DECL.finditer(fn.code)}
        histories = []
        for match in HISTORY.finditer(fn.code):
            owner, offset = match.groups()
            scope = "parameter" if owner in fn.parameters else "local" if owner in locals_ else "global" if owner in global_series else "unclassified"
            histories.append({"owner": owner, "offset": offset.strip(), "scope": scope})
        calls = sorted(set(CALL.findall(fn.code)))
        user_calls = [c for c in calls if c in all_functions]
        queue.extend(user_calls)
        persistent = [m.group(2) for m in DECL.finditer(fn.code) if m.group(1)]
        historical_calls = [c for c in calls if c.startswith(("ta.", "request.")) or c == "math.sum"]
        evidence.append({"function": name, "file": fn.file, "line": fn.line, "history": histories, "persistent_locals": persistent, "historical_builtin_calls": historical_calls, "called_user_functions": user_calls})
    hazards = []
    for item in evidence:
        for h in item["history"]:
            if h["scope"] != "global":
                hazards.append(f'{item["function"]}: {h["scope"]} history {h["owner"]}[{h["offset"]}]')
        hazards.extend(f'{item["function"]}: historical built-in {c}' for c in item["historical_builtin_calls"])
        hazards.extend(f'{item["function"]}: persistent local {c}' for c in item["persistent_locals"])
    return {"root": root, "reachable": evidence, "hazards": hazards, "scope": "SOURCE_DEPENDENCY_AUDIT_NOT_PINE_RUNTIME"}


def source_inputs() -> dict[str, str]:
    return {f"unified/src/{name}.pineinc": (REPO / "unified/src" / f"{name}.pineinc").read_text(encoding="utf-8") for name in FILES}


def baseline_manifest() -> list[dict]:
    paths = sorted((REPO / "unified/src").glob("*.pineinc")) + [REPO / "unified/QIRVANTA_UNIFIED.pine"]
    rows = []
    for path in paths:
        rel = path.relative_to(REPO).as_posix()
        baseline = subprocess.check_output(["git", "show", f"{BASELINE}:{rel}"], cwd=REPO)
        current = path.read_bytes()
        rows.append({"path": rel, "baseline_sha256": hashlib.sha256(baseline).hexdigest(), "current_sha256": hashlib.sha256(current).hexdigest(), "byte_identical": baseline == current})
    return rows


def rejection_witness(*, news_at_open: bool, fill: float = 100, reference: float = 100, stop: float = 90, tp1: float = 110, atr: float = 10) -> dict:
    """Isolated witness of baseline rejection precedence; not trade simulation.

    Other gates are deliberately passing, side=long, minimum tick=.01.
    The selected facts make the exact source branch outcome unambiguous.
    """
    wrong_side = fill - stop < .01 or tp1 - fill <= 0
    far_open = abs(fill - reference) > atr * .20
    reason = "Отваряне извън SL/TP1" if wrong_side else "Новинарска пауза при отваряне" if news_at_open else "Гап/отстояние" if far_open else ""
    return {"price_gap": fill - reference, "rejection_reason": reason, "native_divergence_flag": reason != "", "native_exit_comment_if_flattened": "ИЗХОД СЛЕД ГАП" if reason else None}


def report() -> dict:
    sources = source_inputs()
    manifest = baseline_manifest()
    return {"baseline": BASELINE, "source_unchanged": all(row["byte_identical"] for row in manifest), "manifest": manifest, "functions": [inspect(sources, root) for root in ROOTS], "warning_observation": {"kind": "PRIOR_COMMITTED_TRADINGVIEW_EVIDENCE", "path": "unified/reports/tradingview/readded_final_compile.txt", "generated_lines": {"f_fast": 755, "f_reversal": 756}}, "zero_gap_news_witness": rejection_witness(news_at_open=True), "pine_probe_status": "NOT_RUN_BY_THIS_AUDIT", "profitability_conclusion": "NONE"}


if __name__ == "__main__":
    print(json.dumps(report(), ensure_ascii=False, indent=2))
