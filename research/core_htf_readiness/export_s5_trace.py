"""Export observed formations, plans and paper execution by chart depth."""
import argparse
from collections import Counter
import csv
from datetime import datetime, timezone
import json
from pathlib import Path

TAGS = {"RC1_CORE", "RC1_READY", "DTV_PAPER"}


def utc(stamp):
    if stamp is None:
        return ""
    return datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def trace(path, frame, depth):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for record in csv.DictReader(handle):
            tag, _, payload = record["Message"].partition("|")
            if tag not in TAGS:
                continue
            row = json.loads(payload)
            yield {
                "frame": frame,
                "calculated_bars": depth,
                "source": tag,
                "event_id": row["event_id"],
                "plan_or_source_id": row.get("plan_id", row.get("source_id")),
                "event": row.get("event", row.get("state")),
                "side": row.get("side", row.get("direction")),
                "known_utc": utc(row.get("known_time", row.get("confirmed_ms"))),
                "effective_utc": utc(row.get("effective_time")),
                "price": row.get("price"),
                "entry_reference": row.get("reference", row.get("entry")),
                "initial_sl": row.get("initial_sl", row.get("sl")),
                "tp1": row.get("tp1"),
                "tp2": row.get("tp2"),
                "tp3": row.get("tp3"),
                "total_net_r": row.get("net_r"),
                "cost_r": row.get("cost_r"),
                "reason": row.get("reason"),
            }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("native", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    windows = {
        (frame, n): list(trace(args.native / f"M{frame}-{n}-add.csv", frame, n))
        for frame in (5, 15, 30) for n in (1223, 3200)
    }
    rows = []
    for (frame, n), events in windows.items():
        other = {row["event_id"] for row in windows[frame, 3200 if n == 1223 else 1223]}
        rows.extend(dict(row, same_id_other_depth=row["event_id"] in other) for row in events)
        print(f"M{frame} {n} {dict(Counter(row['event'] for row in events))}")
    with args.output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tuple(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
