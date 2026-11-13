"""Check the protected Visual V2 prefix and the scope of the new Pine code."""

import argparse
from hashlib import sha256
import json
from pathlib import Path
from subprocess import check_output

from research.s5_compile_rescue.static_graph import inventory

HERE = Path(__file__).resolve().parent
CONTROL = "37b903f46a2d878d9d0757da134620c22f2d067d"
CONTROL_FILE = "research/luminous_visual_v2/QIRVANTA_GOLD_PRO_VISUAL_V2.pine"
CONTROL_HASH = "77feedcbef84e77b9270ec43840be5b823d838300d0a09d5ddb4fec7acc089f4"
PROTOCOL_HASH = "e7adbaedc1e9519256729439e59b09156aab8e9876901e40aac18ff5b564462c"
SEAL_HASH = "09181ecaf9e30cde62c5203b524cb458d808a07c24029e961e32bb25c474afd1"


def require(condition, message):
    if not condition:
        raise ValueError(message)


def audit(candidate_path):
    protected = check_output(["git", "show", f"{CONTROL}:{CONTROL_FILE}"])
    candidate = candidate_path.read_bytes()
    require(sha256(protected).hexdigest() == CONTROL_HASH, "Wrong pinned Visual V2 source")
    require(Path(CONTROL_FILE).read_bytes() == protected, "Visual V2 changed in this branch")
    require(sha256((HERE / "FROZEN_PROTOCOL.json").read_bytes()).hexdigest() == PROTOCOL_HASH, "Rules changed")
    seal = Path("docs/gold_pro_evidence/forward_seal_m5.json")
    require(sha256(seal.read_bytes()).hexdigest() == SEAL_HASH, "Forward seal changed")
    require(candidate.startswith(protected), "Protected Pine source differs")
    added = candidate[len(protected):].decode()
    require("tfSeconds == 300" in added and "rcSupported" in added, "Frame or symbol gate missing")
    require("if m5LeadActive and rcCanDrawBar" in added, "Confirmed-bar scope missing")
    require("barstate.isconfirmed and barstate.isrealtime and not rcReview" in added, "Alert scope missing")
    require("f_rcViewRemember(m5LeadLabels, marker, 8)" in added, "Unbounded labels")
    require("offset=" not in added and "strategy." not in added and "request." not in added, "Scope expanded")
    before, before_calls, _ = inventory(protected)
    after, after_calls, _ = inventory(candidate)
    for name, expected in before_calls.items():
        require(after_calls[name][:len(expected)] == expected, f"Protected {name} changed")
    counts = {
        name: after["protected_call_counts"].get(name, 0) - before["protected_call_counts"].get(name, 0)
        for name in before_calls
    }
    require({k: v for k, v in counts.items() if v} == {"input.bool": 2, "alertcondition": 4, "plot": 5},
            "Unexpected added input, plotting or alert API")
    return {
        "status": "STATIC_CHECKS_PASSED", "protected_commit": CONTROL,
        "protected_prefix_byte_identical": True, "candidate_sha256": sha256(candidate).hexdigest(),
        "protocol_sha256": PROTOCOL_HASH, "sealed_control_sha256": SEAL_HASH,
        "lines": [before["lines"], after["lines"]],
        "lexical_tokens": [before["lexical_tokens"], after["lexical_tokens"]],
        "protected_call_deltas": counts, "research_labels_retained": 8,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path, help="Unpublished Pine draft submitted to the compiler")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    args.output.write_text(json.dumps(audit(args.candidate), indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
