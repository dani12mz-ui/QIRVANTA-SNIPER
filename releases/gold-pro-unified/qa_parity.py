"""Check Unified source identity and verify native TradingView QA evidence."""

import argparse
import csv
from hashlib import sha256
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from gold_pro import pressure_qa, rc1_qa, rc2_qa
from research.direct_trend_visibility import native_qa


RELEASE = Path(__file__).resolve().parent
VISIBILITY = ROOT / "research/direct_trend_visibility"
RC2 = ROOT / "releases/gold-pro-rc2"
BASELINE_SHA = "91fba94db0d71d2dd25fcaf72aedf34a7d7cb955d4362996b086ca07c05163d0"
VISIBILITY_ARCHIVE_SHA = "a70e6713446e64d3fcd87a0a6279225de05e7b60d5f202e9448dc78a430755a8"
RC2_ARCHIVE_SHA = "a058bd9789bf9f5d258a08316217f3bebc8b9951460ee0c499aae848c761813a"
FORWARD_SEAL_SHA = "09181ecaf9e30cde62c5203b524cb458d808a07c24029e961e32bb25c474afd1"
BASELINE_COMMIT = "203f9d8a88a8c4b51a5dc646fc0f1752a596ba1f"
RELEASE_COMMIT = "6fe621fc22052c0c745e97feb2f1575c41abd85c"
FRAMES = (5, 15, 30)
HEADER = (
    "// QIRVANTA GOLD PRO UNIFIED — observation + S5/RC2 permission\n"
    "// Research/educational indicator. Directional opportunities are NOT approved trades.\n"
    "// The S5 engine and execution logic are unchanged; DT SHADOW is display-only.\n"
    "// No broker orders. No verified positive expectancy. Confirmation required.\n"
)


def digest(data):
    return sha256(data).hexdigest()


def pinned(path, expected):
    data = path.read_bytes()
    if digest(data) != expected:
        raise ValueError(f"Unexpected SHA-256 for {path.relative_to(ROOT)}")
    return data


def replace_once(source, before, after):
    if source.count(before) != 1:
        raise ValueError(f"Expected a single source marker: {before!r}")
    return source.replace(before, after, 1)


def source_checks():
    raw = pinned(VISIBILITY / "QIRVANTA_GOLD_PRO_SELL_VISIBILITY.pine", BASELINE_SHA)
    baseline = raw.decode("utf-8")
    expected = replace_once(baseline, "//@version=6\n", "//@version=6\n" + HEADER)
    expected = replace_once(
        expected, 'indicator("QIRVANTA GOLD PRO · Directional Visibility", shorttitle="DTV RC2"',
        'indicator("QIRVANTA GOLD PRO · UNIFIED", shorttitle="QIRVANTA GOLD"',
    )
    expected = replace_once(expected, "dtShow = input.bool(false,", "dtShow = input.bool(true,")
    initial_sha = digest(expected.encode("utf-8"))
    expected = replace_once(
        expected, 'f_rcViewCell(0, 0, "  QIRVANTA GOLD PRO RC2",',
        'f_rcViewCell(0, 0, "  QIRVANTA GOLD PRO UNIFIED",',
    )
    actual = (RELEASE / "QIRVANTA_GOLD_PRO_UNIFIED.pine").read_bytes()
    if actual != expected.encode("utf-8"):
        raise ValueError("Unified differs beyond release identity, default shadow visibility or dashboard title")
    audit = pinned(RC2 / "QIRVANTA_GOLD_PRO_RC2_AUDIT.pine", native_qa.AUDIT_SHA256)
    seal = pinned(ROOT / "docs/gold_pro_evidence/forward_seal_m5.json", FORWARD_SEAL_SHA)
    return {
        "status": "MATCH",
        "baseline_commit": BASELINE_COMMIT,
        "baseline_sha256": digest(raw),
        "initial_release_commit": RELEASE_COMMIT,
        "initial_release_sha256": initial_sha,
        "unified_sha256": digest(actual),
        "verified_differences": ["header comments", "indicator title and shorttitle",
                                 "default shadow visibility", "dashboard title"],
        "rc2_audit_sha256": digest(audit),
        "forward_seal_sha256": digest(seal),
    }


def extract_archive(path, expected, directory, names):
    pinned(path, expected)
    with ZipFile(path) as archive:
        for name in names:
            (directory / name).write_bytes(archive.read(name))


def compare_indicator(directory, frame, right_prefix, review_prefix):
    baseline = directory / f"baseline-M{frame}.csv"
    candidate = directory / f"{right_prefix}-M{frame}.csv"
    review = directory / f"{review_prefix}-M{frame}.csv"
    return {
        "s5_rc2_paper_and_alerts": checked(lambda: native_qa.verify_pair(baseline, candidate, frame, "indicator")),
        "shadow_timeline": checked(lambda: native_qa.verify_shadow(baseline, candidate, frame)),
        "directional_state": checked(lambda: native_qa.verify_visibility(candidate, frame, review)),
    }


def audit_pair(directory, frame):
    lanes = {}
    results = {}
    for filename, policy in (("fixed", "fixed"), ("parity", "parity")):
        path = directory / f"{filename}_M{frame}.csv"
        lane = rc1_qa.read_export(path, frame, "audit")
        lanes[policy] = lane
        results[policy] = {
            "csv_sha256": digest(path.read_bytes()),
            "context": lane.meta,
            **rc2_qa.native_checks(path, lane, rc2_qa.extras(path), frame, rc2_qa.MODES[policy]),
        }
    return {
        "scope": "Original RC2 Strategy Tester exports for both stop policies",
        "s5_signal_and_ready": rc2_qa.shared(lanes["fixed"], lanes["parity"], ("core", "ready")),
        "runs": results,
    }


def archived_checks():
    with TemporaryDirectory() as tmp:
        folder = Path(tmp)
        visibility = folder / "visibility"
        rc2 = folder / "rc2"
        visibility.mkdir()
        rc2.mkdir()
        extract_archive(VISIBILITY / "evidence/native-logs.zip", VISIBILITY_ARCHIVE_SHA, visibility,
                        [f"{name}-M{frame}.csv" for name in ("baseline", "visibility") for frame in FRAMES]
                        + ["visibility-review-M15.csv"])
        extract_archive(RC2 / "evidence/native-logs.zip", RC2_ARCHIVE_SHA, rc2,
                        [f"{name}_M{frame}.csv" for name in ("fixed", "parity") for frame in FRAMES])
        return {
            "scope": "Recorded native executions of the pinned prior sources. Separate from fresh Unified runs.",
            "visibility_archive_sha256": VISIBILITY_ARCHIVE_SHA,
            "rc2_archive_sha256": RC2_ARCHIVE_SHA,
            "frames": {f"M{frame}": {
                "visibility": compare_indicator(visibility, frame, "visibility", "visibility-review"),
                "strategy_audits": audit_pair(rc2, frame),
            } for frame in FRAMES},
        }


def pressure_pair(directory, frame):
    runs, reports = [], {}
    for name in ("baseline", "unified"):
        path = directory / f"{name}-M{frame}.csv"
        run, problem = pressure_qa.pressure_source(path, frame)
        runs.append(run)
        reports[name] = problem if problem else pressure_qa.pressure_checks(
            run, {}, rc1_context=rc1_qa.read_export(path, frame, "indicator").context,
        )
    return {"independent_replay": reports, "repeated_native_values": pressure_qa.pressure_repeat(*runs)}


def fresh_audit(directory, frame):
    paired = native_qa.verify_pair(directory / f"baseline-audit-M{frame}.csv",
                                   directory / f"unified-audit-M{frame}.csv", frame, "audit")
    other = {}
    for filename, policy in (("audit-fixed", "fixed"), ("audit-s5", "parity")):
        path = directory / f"{filename}-M{frame}.csv"
        if path.is_file():
            lane = rc1_qa.read_export(path, frame, "audit")
            candidate = directory / f"unified-M{frame}.csv"
            run = rc1_qa.read_export(candidate, frame, "indicator") if candidate.is_file() else None
            other[policy] = {
                "csv_sha256": digest(path.read_bytes()),
                "strategy_tester": rc2_qa.native_checks(path, lane, rc2_qa.extras(path), frame, rc2_qa.MODES[policy]),
                "shared_core_ready": rc2_qa.shared(lane, run, ("core", "ready")),
            }
    return {"paired_fixed_sl": paired, "original_audits": other}


def checked(check):
    try:
        return check()
    except FileNotFoundError as exc:
        return {"status": "MISSING_EVIDENCE", "file": Path(exc.filename).name}
    except (OSError, ValueError, KeyError, csv.Error) as exc:
        return {"status": "MISMATCH", "reason": str(exc)}


def fresh_checks(directory):
    return {f"M{frame}": {
        "indicator": compare_indicator(directory, frame, "unified", "unified-review"),
        "pressure": checked(lambda: pressure_pair(directory, frame)),
        "strategy_audits": checked(lambda: fresh_audit(directory, frame)),
    } for frame in FRAMES}


def mismatches(report):
    if isinstance(report, dict):
        return report.get("status") in {"MISMATCH", "INVALID_EXPORT"} or any(
            mismatches(value) for value in report.values()
        )
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-dir", type=Path, help="Directory containing fresh TradingView CSV journals")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {"source": source_checks(), "archived": archived_checks()}
    if args.native_dir:
        report["fresh"] = fresh_checks(args.native_dir)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n",
                           encoding="utf-8", newline="\n")
    return int(mismatches(report))


if __name__ == "__main__":
    raise SystemExit(main())
