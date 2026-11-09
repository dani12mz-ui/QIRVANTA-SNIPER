#!/usr/bin/env python3
"""Freeze git objects and run existing tests without rewriting legacy evidence."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
BASELINE = "48e5cddab17ea831bdc277f17f0406329fdfe39c"


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def save(name: str, value: object) -> None:
    (OUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def freeze() -> None:
    """Read exact committed blobs; never build, checkout, or duplicate Pine."""
    target = OUT / "baseline_manifest.json"
    if target.exists():
        raise SystemExit("Refusing to overwrite the frozen baseline manifest")
    if git("rev-parse", "HEAD").decode().strip() != BASELINE:
        raise SystemExit("Freeze must start at the exact requested baseline commit")
    rows = []
    objects = git("ls-tree", "-rz", "--full-tree", BASELINE).split(b"\0")
    process = subprocess.Popen(["git", "cat-file", "--batch"], cwd=ROOT, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert process.stdin and process.stdout
    try:
        for record in objects:
            if not record:
                continue
            metadata, path_bytes = record.split(b"\t", 1)
            mode, kind, oid = metadata.decode("ascii").split()
            path = path_bytes.decode("utf-8")
            if kind != "blob":
                raise RuntimeError(f"Unsupported non-file tree entry: {path} ({kind})")
            process.stdin.write(oid.encode() + b"\n")
            process.stdin.flush()
            header = process.stdout.readline().decode("ascii").strip().split()
            size = int(header[2])
            content = process.stdout.read(size)
            if process.stdout.read(1) != b"\n" or len(content) != size:
                raise RuntimeError("Incomplete git object stream")
            local = ROOT / path
            rows.append({"path": path, "git_mode": mode, "git_blob_oid": oid, "blob_bytes": size,
                         "git_blob_sha256": digest(content),
                         "working_tree_sha256_at_freeze": digest(local.read_bytes()) if local.is_file() else None})
    finally:
        process.stdin.close()
        process.stdout.close()
        process.wait()
    rows.sort(key=lambda row: row["path"])
    save(target.name, {
        "schema": "qirvanta-baseline-freeze-v1", "created_at_utc": utc_now(),
        "baseline_commit": BASELINE, "baseline_tree": git("rev-parse", f"{BASELINE}^{{tree}}").decode().strip(),
        "starting_branch": git("branch", "--show-current").decode().strip(),
        "commit_metadata": git("show", "-s", "--format=%H%n%T%n%cI%n%s", BASELINE).decode().splitlines(),
        "scope": "Every file tracked at the exact baseline commit, including source, generated Pine, tests, reports and images",
        "hash_semantics": "git_blob_sha256 hashes committed bytes; working_tree_sha256_at_freeze hashes checkout bytes (line endings may differ)",
        "tracked_file_count": len(rows),
        "file_index_sha256": digest(json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()),
        "files": rows,
    })
    print(json.dumps({"manifest": str(target.relative_to(ROOT)), "tracked_files": len(rows), "sha256": digest(target.read_bytes())}))


def unchanged_report() -> dict:
    manifest = json.loads((OUT / "baseline_manifest.json").read_text(encoding="utf-8"))
    changed = []
    missing = []
    for row in manifest["files"]:
        path = ROOT / row["path"]
        if not path.is_file():
            missing.append(row["path"])
        elif digest(path.read_bytes()) != row["working_tree_sha256_at_freeze"]:
            changed.append(row["path"])
    return {"checked_at_utc": utc_now(), "baseline_commit": BASELINE, "checked_files": len(manifest["files"]),
            "changed_tracked_files_since_freeze": changed, "missing_tracked_files": missing,
            "all_baseline_working_tree_bytes_unchanged": not (changed or missing)}


def run_tests() -> int:
    before = unchanged_report()
    if not before["all_baseline_working_tree_bytes_unchanged"]:
        save("test_preflight_failure.json", before)
        raise SystemExit("Baseline files changed before tests; see test_preflight_failure.json")
    reports = []
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    for suite, expected in (("unified", 99), ("refactored", 127)):
        command = [sys.executable, "-B", "-m", "unittest", "discover", "-s", "tests", "-v"]
        started = utc_now()
        timer = time.monotonic()
        result = subprocess.run(command, cwd=ROOT / suite, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        logfile = OUT / f"{suite}_baseline_tests.log"
        logfile.write_bytes(result.stdout)
        output = result.stdout.decode("utf-8", errors="replace")
        match = re.search(r"Ran (\d+) tests? in", output)
        count = int(match.group(1)) if match else None
        reports.append({"suite": suite, "started_at_utc": started, "duration_seconds": round(time.monotonic() - timer, 3),
                        "command": command, "cwd": suite, "returncode": result.returncode,
                        "expected_test_count": expected, "observed_test_count": count,
                        "status": "PASS" if result.returncode == 0 and count == expected else "FAIL_OR_COUNT_CHANGED",
                        "output_path": logfile.relative_to(ROOT).as_posix(), "output_sha256": digest(result.stdout)})
    after = unchanged_report()
    save("baseline_tests.json", {"baseline_commit": BASELINE,
         "scope": "Existing Python reference and static-source tests only; not new Pine compilation or proof of profitability",
         "python": sys.version, "platform": platform.platform(),
         "legacy_run_tests_py_not_used": "Those runners overwrite existing reports; unittest discovery writes only new diagnostics logs",
         "before": before, "suites": reports, "after": after})
    print(json.dumps({"suites": [{k: row[k] for k in ("suite", "status", "observed_test_count")} for row in reports],
                      "all_baseline_bytes_unchanged": after["all_baseline_working_tree_bytes_unchanged"]}))
    return 0 if after["all_baseline_working_tree_bytes_unchanged"] and all(row["status"] == "PASS" for row in reports) else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("freeze", "test", "verify"))
    args = parser.parse_args()
    if args.action == "freeze":
        freeze()
        return 0
    if args.action == "test":
        return run_tests()
    report = unchanged_report()
    save("baseline_integrity.json", report)
    print(json.dumps(report))
    return 0 if report["all_baseline_working_tree_bytes_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
