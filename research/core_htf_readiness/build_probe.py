"""Generate a temporary QA probe from revision 4875918, without editing a request."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

BASE = "4875918c17db967b706364c35c7cd074c5fbc14d"
SOURCE = "research/entry_decision_audit/QIRVANTA_GOLD_PRO_ENTRY_AUDIT.pine"
DIGEST = "7be6412d3d22f62ea6e8ea1cd92a1dfd017ba38173e2289782c177169fdb6221"


def once(text, old, new):
    if text.count(old) != 1:
        raise ValueError(f"Unexpected anchor count: {old[:100]}")
    return text.replace(old, new, 1)


def build(source, pairs):
    result = source
    for old, new in pairs:
        result = once(result, old, new)
    restored = result
    for old, new in reversed(pairs):
        restored = once(restored, new, old)
    if restored != source:
        raise ValueError("Source restore failed")
    for token in ("request.security(", "request.security_lower_tf(", "indicator(", "alert(", "alertcondition("):
        old_lines = [line for line in source.splitlines() if token in line]
        new_lines = [line for line in result.splitlines() if token in line]
        if old_lines != new_lines:
            raise ValueError(f"Protected {token} list changed")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = subprocess.check_output(["git", "-C", str(args.repo), "show", f"{BASE}:{SOURCE}"])
    if hashlib.sha256(source).hexdigest() != DIGEST:
        raise ValueError("Unexpected pinned Pine source hash")
    pairs = json.loads(Path(__file__).with_name("probe-patch.json").read_text(encoding="utf-8"))
    result = build(source.decode("utf-8"), pairs).encode("utf-8")
    args.output.write_bytes(result)
    print(hashlib.sha256(result).hexdigest())


if __name__ == "__main__":
    main()
