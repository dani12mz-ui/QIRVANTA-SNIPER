"""Summarize the frozen matched M5 portfolio study without retesting strategy rules."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path
import subprocess

SOURCE = "587e46228040788fe893dd242769511257557993:research/market_coverage_entry_quality/evidence/evidence.json.gz"
SOURCE_SHA256 = "b1f8f4e7fc2e62b9d99b281e114564a134ef3724575bb35e6291d688ef4d1cb4"
METRICS = (
    "closed", "wins", "losses", "win_rate", "win_rate_wilson95", "net_r", "net_usd",
    "expectancy_r", "profit_factor", "average_loss_usd", "mean_worst_five_percent_r",
    "marked_bar_close_drawdown_usd", "modeled_total_cost_usd",
)


def summarize(study):
    output = {}
    for case, entry in study["entry_quality"]["costs"].items():
        portfolios = {
            name: lane["metrics"]["full"] for name, lane in entry["portfolios"].items()
        }
        portfolios.update({
            "s5_plus_" + name: lane["combined_metrics"]["full"]
            for name, lane in entry["protected_increment"].items()
        })
        output[case] = {
            name: {
                side: {key: metrics[key] for key in METRICS}
                for side, metrics in sides.items()
            } for name, sides in portfolios.items()
        }
    return {
        "source": SOURCE,
        "source_sha256": SOURCE_SHA256,
        "scope": "Previously used M5 OANDA archive, quote-side scenario, known dates; no new execution test.",
        "independent_holdout": study["independent_holdout"],
        "time_in_drawdown": None,
        "time_in_drawdown_reason": "Per-bar marked equity time series not retained in this pinned portable evidence.",
        "portfolio_results": output,
        "prior_gate_counterfactuals": {
            case: entry["new_gate_rejection_counterfactual_counts"]
            for case, entry in study["entry_quality"]["costs"].items()
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    compressed = subprocess.check_output(["git", "-C", str(args.repo), "show", SOURCE])
    if hashlib.sha256(compressed).hexdigest() != SOURCE_SHA256:
        raise ValueError("Unexpected archived controls hash")
    summary = summarize(json.loads(gzip.decompress(compressed)))
    args.output.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
