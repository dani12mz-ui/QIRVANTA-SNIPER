"""Render publishable experiment and regime tables without exporting full OHLC."""

import argparse
import csv
import gzip
import json
from pathlib import Path


def write_csv(path, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]), lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def publish(replay, output):
    result = json.loads(gzip.decompress(replay.read_bytes()))
    regimes, experiments = [], []
    names = {"S5_ARCHIVE": "CONTROL", "G_BASE": "GAP_ABLATION", "G_HOLD": "GAP_ACCEPTANCE",
             "M_CROSS": "MIDPOINT_ABLATION", "M_HOLD": "MIDPOINT_HOLD"}
    for frame, arms in result["frames"].items():
        for arm, scenarios in arms.items():
            for costs, replay_run in scenarios.items():
                for window, scored in replay_run["windows"].items():
                    regimes.extend({"window": window, **row} for row in scored["metrics"])
                    headline = scored["metrics"][0]
                    if window != "full_seen":
                        continue
                    experiments.append({
                        "frame": frame, "hypothesis": names[arm], "arm": arm, "cost": costs,
                        "status": ("ARCHIVAL_CONTROL" if arm == "S5_ARCHIVE" else
                                   "NO_EXECUTED_CAMPAIGNS" if headline["closed"] == 0 else
                                   "NEGATIVE_NET" if headline["net_usd"] < 0 else "POSITIVE_SPARSE_RESEARCH"),
                        **{k: headline[k] for k in ("closed", "wins", "losses", "flats", "net_r", "net_usd",
                                                   "marked_drawdown_usd", "timely_hours")},
                        "observed_outside_label_hours": scored["unmatched_observational_hour_sides"],
                        "all_observed_eligible_hour_sides": scored["observable_hour_sides"],
                        "full_window_gate_states": json.dumps(scored["gate_states"], sort_keys=True, separators=(",", ":")),
                    })
    experiments.append({**dict.fromkeys(experiments[0]), "frame": "M15", "hypothesis": "H1_CANCELLED_ZERO_FILL_COOLDOWN",
                        "arm": "H1_NATIVE_AB", "status": "BLOCKED_MISSING_FULL_NATIVE_AB"})
    output.mkdir(parents=True, exist_ok=True)
    write_csv(output / "EXPERIMENTS.csv", experiments)
    write_csv(output / "REGIME_RESULTS.csv", regimes)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    publish(args.replay, args.output)


if __name__ == "__main__":
    main()
