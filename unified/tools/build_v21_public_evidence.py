"""Publish V2.1 audit decisions and fill rows without redistributing dense OHLC tapes."""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def put(zip_, name, data):
    zip_.writestr(ZipInfo(name, (2026, 11, 10, 15, 0, 0)), data,
                  compress_type=ZIP_DEFLATED, compresslevel=9)


def csv_bytes(rows):
    out = io.StringIO(newline="")
    csv.writer(out, lineterminator="\n").writerows(rows)
    return out.getvalue().encode()


def build(case, logs, output):
    manifest = {
        "scope": "Decision and trade projection; original OHLC and full private evaluation tapes stay in the local audit workspace.",
        "case_zip_sha256": hashlib.sha256(case.read_bytes()).hexdigest(),
        "native_original_sha256": {},
    }
    columns = (
        "time_open_utc", "known_close_utc", "position_size", "warmup", "entry_frame", "tv_context_ready",
        "core_htf_ready", "valid_atr", "acceptable_bar", "time_gap", "date_pass", "news_paused",
        "session_pass", "require_h1_match", "require_fvg", "require_premium_discount", "h1_bias", "h4_bias",
        "buy_reason", "sell_reason", "buy_phase", "sell_phase", "s5_buy_decision", "s5_sell_decision",
        "intel_h1_bias", "intel_h4_bias", "intel_displacement_up", "intel_displacement_down",
        "intel_exhaustion", "intel_candidate_side", "intel_candidate_grade", "intel_pending_side",
        "intel_last_structure", "intel_warning",
    )
    with ZipFile(case) as src, ZipFile(output, "x") as out:
        put(out, "case/missed_sell_diagnostic.json", src.read("audit/missed_sell_diagnostic.json"))
        for frame in (5, 15):
            tape = src.read(f"audit/missed_sell_M{frame}_timeline.csv").decode()
            rows = csv.DictReader(io.StringIO(tape))
            projection = [columns + ("s5_event_reason_trace",)]
            for row in rows:
                events = json.loads(row["s5_events"] or "[]")
                reasons = [f'{e["event"]}:{e["side"]}:{e["reason"]}' for e in events]
                projection.append(tuple(row[k] for k in columns) + (" | ".join(reasons),))
            put(out, f"case/M{frame}_decision_timeline.csv", csv_bytes(projection))
        for path in sorted(logs):
            original = path.read_bytes()
            manifest["native_original_sha256"][path.name] = hashlib.sha256(original).hexdigest()
            rows = list(csv.reader(io.StringIO(original.decode("utf-8-sig"))))
            if not any(len(row) > 1 and row[1].startswith("V21|SNAPSHOT|") for row in rows):
                raise ValueError(f"Missing native snapshot in {path}")
            projected = [row for row in rows if len(row) < 2 or not row[1].startswith("V21|BAR|")]
            put(out, f"native/{path.name}", csv_bytes(projected))
        put(out, "manifest.json", (json.dumps(manifest, indent=2) + "\n").encode())


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--case", type=Path, required=True)
    cli.add_argument("--native-logs", type=Path, nargs=3, required=True)
    cli.add_argument("--out", type=Path, required=True)
    args = cli.parse_args()
    build(args.case, args.native_logs, args.out)


if __name__ == "__main__":
    main()
