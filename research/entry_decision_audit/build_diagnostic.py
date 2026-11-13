"""Build and verify a diagnostic copy from the pinned Visual V2 source."""

import argparse
import hashlib
import json
from pathlib import Path
import subprocess

BASE = "37b903f46a2d878d9d0757da134620c22f2d067d"
SOURCE = "research/luminous_visual_v2/QIRVANTA_GOLD_PRO_VISUAL_V2.pine"
BASELINE_SHA256 = "77feedcbef84e77b9270ec43840be5b823d838300d0a09d5ddb4fec7acc089f4"


def replace_once(source, old, new):
    if source.count(old) != 1:
        raise ValueError(f"Expected exactly one anchor: {old!r}")
    return source.replace(old, new, 1)


def build(baseline, patch):
    source = replace_once(baseline, patch["moved"], "")
    for anchor, insertion in patch["insertions"]:
        source = replace_once(source, anchor, insertion + anchor)
    return source


def strip(candidate, patch, baseline):
    for anchor, insertion in reversed(patch["insertions"]):
        candidate = replace_once(candidate, insertion + anchor, anchor)
    anchor = 'bool rcCanDrawBar = rcSupported and processBar\n'
    return replace_once(candidate, anchor, anchor + patch["moved"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    baseline_bytes = subprocess.check_output(["git", "-C", str(args.repo), "show", f"{BASE}:{SOURCE}"])
    if hashlib.sha256(baseline_bytes).hexdigest() != BASELINE_SHA256:
        raise ValueError("Unexpected baseline hash")
    baseline = baseline_bytes.decode("utf-8")
    patch = json.loads(Path(__file__).with_name("instrumentation.json").read_text(encoding="utf-8"))
    candidate = build(baseline, patch)
    if strip(candidate, patch, baseline) != baseline:
        raise ValueError("Source stripping did not restore the exact baseline")
    protected = {
        token: baseline.count(token)
        for token in ("request.security(", "request.security_lower_tf(", "alert(", "alertcondition(", "strategy.entry(", "table.new(", "label.new(", "line.new(", "box.new(", "log.info(")
    }
    for token, count in protected.items():
        if token != "log.info(" and candidate.count(token) != count:
            raise ValueError(f"Unexpected change in {token!r}")
    if candidate.count("    [why, attempted] = f_tvCandidate(d)") != 1:
        raise ValueError("Changed S5 candidate call count")
    args.output.write_bytes(candidate.encode("utf-8"))
    manifest = {
        "baseline_commit": BASE,
        "baseline_sha256": BASELINE_SHA256,
        "candidate_sha256": hashlib.sha256(args.output.read_bytes()).hexdigest(),
        "source_bytes": {"baseline": len(baseline_bytes), "candidate": args.output.stat().st_size},
        "changed_lines": {"added": len(candidate.splitlines()) - len(baseline.splitlines()) + 3, "removed_and_relocated": 3},
        "protected_occurrences": protected,
        "additional_log_calls": candidate.count("log.info(") - protected["log.info("],
        "source_restored_exactly": True,
    }
    if args.report:
        args.report.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest))


if __name__ == "__main__":
    main()
