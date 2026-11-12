"""Verify the visual delta and exact native S5/RC2 parity."""
import argparse
from difflib import SequenceMatcher, diff_bytes, unified_diff
from hashlib import sha256
import json
from pathlib import Path

from research.professional_signal_selector import native_qa

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
CANDIDATE = HERE / "QIRVANTA_GOLD_PRO_HTF_DISPLAY_FIX.pine"
BASELINE_SHA = "91f2f7687fd07f836cad3dfad145721a5c8f9cc74207b1db00afc60f6cb86b4c"
COMPILED_SHA = "a9955a1ed3f402f1b8674b3ef2fabfc3d6578bb20f61d8a7d12ade5f527353a2"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def source_report():
    before = native_qa.CANDIDATE.read_bytes()
    after = CANDIDATE.read_bytes()
    baseline_sha, candidate_sha = (sha256(raw).hexdigest() for raw in (before, after))
    require(baseline_sha == BASELINE_SHA and candidate_sha == COMPILED_SHA, "Unexpected Pine source")
    native_qa.source_checks()
    old, new = (raw.splitlines(keepends=True) for raw in (before, after))
    edits = [(start + 1, end, c + 1, d) for tag, start, end, c, d in
             SequenceMatcher(None, old, new, autojunk=False).get_opcodes() if tag != "equal"]
    require(len(edits) == 6 and all(2145 < start < 2220 or 2945 < start < 2960
                                    for start, _, _, _ in edits), "Change outside the display sections")
    for call in (b"request.security(", b"request.security_lower_tf(", b"alert(", b"alertcondition(",
                 b"log.info(", b"table.new("):
        require([line for line in old if call in line] == [line for line in new if call in line],
                f"Changed protected call: {call!r}")
    path = native_qa.CANDIDATE.relative_to(ROOT).as_posix()
    patch = b"".join(diff_bytes(unified_diff, old, new, f"a/{path}".encode(), f"b/{path}".encode(), n=1))
    require(patch == (HERE / "DISPLAY_ONLY.patch").read_bytes(), "Published patch differs from the compiled Pine")
    return {"baseline_commit": "6e2c2b4425dd5bca6741e8d3f91c244fbacb6daa",
            "canonical_research_commit": "416cb2fef55b98ac09e762563055c297573e5962",
            "baseline_sha256": baseline_sha, "compiled_candidate_sha256": candidate_sha,
            "forward_seal_sha256": native_qa.FORWARD_SHA, "display_ranges_old_new": edits}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = native_qa.native_report(args.native_dir)
    exact = report["status"] == "MATCH" and report["protected_parity_closes"] == 318 and all(
        row.get("raw_csv_byte_identical") and row.get("indicator_context_and_warmup_match")
        for row in report["frames"].values())
    args.output.write_text(json.dumps({"source": source_report(), "exact": exact, "native": report},
                                      indent=2, ensure_ascii=False, default=str) + "\n",
                           encoding="utf-8", newline="\n")
    return 0 if exact else 1


if __name__ == "__main__":
    raise SystemExit(main())

