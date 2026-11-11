"""Create disposable native QA scripts from the pinned baseline and Unified indicator."""

import argparse
from datetime import datetime, timezone
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[2]
RELEASE = Path(__file__).resolve().parent
BASELINE = ROOT / "research/direct_trend_visibility/QIRVANTA_GOLD_PRO_SELL_VISIBILITY.pine"
UNIFIED = RELEASE / "QIRVANTA_GOLD_PRO_UNIFIED.pine"
RC2_AUDIT = ROOT / "releases/gold-pro-rc2/QIRVANTA_GOLD_PRO_RC2_AUDIT.pine"
sys.path.insert(0, str(ROOT / "unified/tools"))
from build_rc2_pressure import qa_script, utc_ms


def set_default(source, name, previous, updated):
    pattern = rf"(\b{name}\s*=\s*input\.(?:bool|string|time)\(){re.escape(previous)}(?=,)"
    source, replaced = re.subn(pattern, lambda match: match[1] + updated, source)
    if replaced != 1:
        raise ValueError(f"Expected a single input default for {name}")
    return source


def set_time(source, name, value):
    pattern = rf'(\b{name}\s*=\s*input\.time\()timestamp\("[^"]+"\)(?=,)'
    source, replaced = re.subn(pattern, lambda match: match[1] + str(value), source)
    if replaced != 1:
        raise ValueError(f"Expected a single timestamp input for {name}")
    return source


def probe(source, start, end, bars, is_baseline, pressure=True):
    source = qa_script(source, start, end, bars, False, pressure=pressure)
    flags = ["dtQa", "dvNotify"]
    if is_baseline:
        flags.append("dtShow")
    for name in flags:
        source = set_default(source, name, "false", "true")
    return source


def save(path, source):
    with path.open("xb") as stream:
        stream.write(source.encode("utf-8"))


def build(output, start, end, bars, m30_start):
    output.mkdir(parents=True, exist_ok=True)
    baseline = BASELINE.read_text(encoding="utf-8")
    unified = UNIFIED.read_text(encoding="utf-8")
    audit = RC2_AUDIT.read_text(encoding="utf-8")
    save(output / "baseline.pine", probe(baseline, start, end, bars, True))
    save(output / "unified.pine", probe(unified, start, end, bars, False))
    if m30_start is not None:
        save(output / "baseline-m30-wide.pine", probe(baseline, m30_start, end, bars, True))
        save(output / "unified-m30-wide.pine", probe(unified, m30_start, end, bars, False))
    # Strategy exits within the indicator window can belong to older entries.
    audit_start = min(start, utc_ms("2026-10-01T00:00:00Z"))
    for name, s5 in (("audit-fixed", False), ("audit-s5", True)):
        save(output / f"{name}.pine", qa_script(audit, audit_start, end, bars, s5))
    historical = set_default(unified, "rcPreset", '"PREMIUM"', '"FULL AUDIT"')
    historical = set_default(historical, "rcReviewEnabled", "false", "true")
    for time in ("00:15", "02:15", "11:30", "11:45", "12:00", "12:15", "12:30", "13:30", "14:45", "15:30"):
        stamp = datetime.fromisoformat(f"2026-11-11T{time}:00").replace(tzinfo=timezone.utc)
        case = time.replace(":", "")
        save(output / f"review-{case}.pine", set_time(historical, "rcReviewAt", int(stamp.timestamp() * 1000)))
        if time == "12:00":
            history_journal = probe(historical, start, end, bars, False)
            save(output / "review-journal-1200.pine",
                 set_time(history_journal, "rcReviewAt", int(stamp.timestamp() * 1000)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--bars", type=int, default=1200)
    parser.add_argument("--start", type=utc_ms, default="2026-11-11T00:00:00Z")
    parser.add_argument("--end", type=utc_ms, default="2026-11-11T17:30:00Z")
    parser.add_argument("--m30-start", type=utc_ms, help="Additional wider M30 journal start in UTC")
    args = parser.parse_args()
    if args.bars < 1 or args.start >= args.end or (args.m30_start is not None and args.m30_start >= args.start):
        parser.error("Invalid QA period or history length")
    build(args.output, args.start, args.end, args.bars, args.m30_start)


if __name__ == "__main__":
    main()
