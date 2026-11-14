"""Build a hash-pinned, headless S5 H1 diagnostic with no execution integration."""

import argparse
from hashlib import sha256
from pathlib import Path
import re
import subprocess


CONTROL = "37b903f46a2d878d9d0757da134620c22f2d067d"
CONTROL_PATH = "research/luminous_visual_v2/QIRVANTA_GOLD_PRO_VISUAL_V2.pine"
CONTROL_SHA = "77feedcbef84e77b9270ec43840be5b823d838300d0a09d5ddb4fec7acc089f4"
SECTION_RE = re.compile(r"^// @h1 ([a-z]+)\n", re.MULTILINE)
CORE_END = "// END S5 SPECIALIZATION\n"
EXPECTED_SECTIONS = {"input", "memory", "selection", "call", "tail"}
SOURCE_TUPLE = "[htfClose, htfEMA50, htfEMA200, htfEMA50prev]"
SOURCE_EXPRESSION = "ta.ema(close, 50)[3]], lookahead=barmerge.lookahead_on)\n[priorDayHigh"
BASE_COOLDOWN = (
    '            reason := extraReason != "" ? extraReason : not cool ? "Пауза след потвърден модел" : commonReason\n'
    '            if reason == "УСЛОВИЯТА СА ИЗПЪЛНЕНИ"\n'
    "                array.push(candidates, p)\n"
    "                array.set(tvCandidateLast, idx, bar_index)\n"
)
BASE_CALL = "        [why, attempted] = f_tvCandidate(d)\n"


def once(source, before, after):
    if source.count(before) != 1:
        raise ValueError(f"Expected one pinned source match: {before[:100]!r}")
    return source.replace(before, after, 1)


def include_sections(include):
    pieces = SECTION_RE.split(include)
    if pieces[0].strip() or len(pieces) != 11:
        raise ValueError("Malformed H1 include; expected exactly five named sections")
    parsed = {pieces[i]: pieces[i + 1].rstrip() + "\n" for i in range(1, len(pieces), 2)}
    if set(parsed) != EXPECTED_SECTIONS:
        raise ValueError("Incomplete H1 include sections")
    return parsed


def between(source, begin, end):
    start = source.index(begin)
    stop = source.index(end, start)
    return source[start:stop]


def build(original, include):
    if sha256(original).hexdigest() != CONTROL_SHA:
        raise ValueError("Original Visual V2 source must match the pinned commit hash")
    source = original.decode("utf-8")
    segments = include_sections(include)
    pivot = source.index(CORE_END) + len(CORE_END)
    base = source[:pivot]
    body = once(base, '"QIRVANTA GOLD PRO · VISUAL SIGNATURE", shorttitle="QIRV GOLD V2"',
                '"QIRVANTA S5 · H1 NATIVE DIAGNOSTIC", shorttitle="QIRV H1 QA"')
    body = once(body, "const string CORE_SHA256", segments["input"] + "\nconst string CORE_SHA256")
    body = once(body, SOURCE_TUPLE, SOURCE_TUPLE[:-1] + ", h1SourceClose1, h1SourceClose3]")
    body = once(body, SOURCE_EXPRESSION,
                "ta.ema(close, 50)[3], time_close[1], time_close[3]], lookahead=barmerge.lookahead_on)\n[priorDayHigh")
    body = once(body, "\nf_tvCandidate(int d) =>\n", "\n" + segments["memory"] + "\nf_tvCandidate(int d) =>\n")
    body = once(body, BASE_COOLDOWN, segments["selection"])
    body = once(body, BASE_CALL, segments["call"])

    # Reuse the original payload and plan acceptance predicates, minus the rendering and alert bridge.
    quantity = between(source, "f_tvQtyOnGrid(float amount) =>", "const bool rc2Parity")
    stages = between(source, "const int RC_NONE", "bool rcSupported")
    side_and_payload = between(source, "f_rcSide(int side) =>", "Plan rcSelected")
    ready_reason = between(source, "const string RC_READY_REASON", "\nif rcQaBar")
    script = body + "\n" + quantity + stages + side_and_payload + ready_reason + "\n" + segments["tail"]

    protected = between(base, "f_addClosed(Ledger a, Paper s) =>", CORE_END)
    if protected not in script:
        raise ValueError("Protected paper, allocation, ledger or evidence shield processing changed")
    if any(token in script for token in ("alert(", "alertcondition(", "strategy.entry(", "strategy.order(", "strategy.exit(", "request.security_lower_tf(")):
        raise ValueError("Headless diagnostic unexpectedly includes an external action or alternate request")
    # Compare request lines after discarding just the two returned observational timestamps.
    unchanged = script.replace(", h1SourceClose1, h1SourceClose3", "").replace(", time_close[1], time_close[3]", "")
    requests = lambda text: [line for line in text.splitlines() if "request.security(" in line]
    if requests(unchanged) != requests(base):
        raise ValueError("Original core request inputs or evaluation changed")
    return script.encode("utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    original = subprocess.check_output(["git", "-C", str(args.repo), "show", f"{CONTROL}:{CONTROL_PATH}"])
    include = Path(__file__).with_name("capture.pineinc").read_text(encoding="utf-8")
    diagnostic = build(original, include)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(diagnostic)
    print(f"SHA256 {sha256(diagnostic).hexdigest()}  {len(diagnostic)} bytes")


if __name__ == "__main__":
    main()

