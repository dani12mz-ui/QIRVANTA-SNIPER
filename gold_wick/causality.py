"""Verify confirmed wick decisions against shorter and later-start archive runs."""

import argparse
from bisect import bisect_right
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path

from gold_pro.archive import REGISTRATION, ROOT, _read, _read_json, _write, load_archive
from gold_pro.replay import Costs
from unified.tools.gold_market_data import context_rows

from .engine import Settings, scan


ARCHIVE_REGISTRATION_SHA = "4a416187bbce040f977d8e68efc4aaff8d8e7e0d922bf06fecaaa49a72c6476c"
DROPPED_BARS = (20, 100, 250, 1000)
RANGE_WARMUP_BARS = 40


def confirmed_context(bars, earlier):
    sides = []
    for row in context_rows(bars, earlier=earlier):
        if any(item.known_at is not None and item.known_at > row.bar.open_at for item in (row.h1, row.h4)):
            raise ValueError(f"Higher-timeframe source was unavailable at chart open {row.bar.open_at}")
        sides.append((row.h1.side, row.h4.side))
    return sides


def normalized(result, bars, *, start=0, stop=None):
    """Make comparison independent of where the local bar-index count starts."""
    if stop is None:
        stop = len(bars)
    events = [{k: v for k, v in item.items() if k not in ("bar", "breakout_bar")}
              for item in result.events if start <= item["bar"] < stop]
    features = [{k: v for k, v in item.items() if k != "bar"} for item in result.features[start:stop]]
    plans = [{**{k: v for k, v in asdict(plan).items() if k != "bar"},
              "known_ms": bars[plan.bar]["time_close"]}
             for plan in result.plans if start <= plan.bar < stop]
    return {"events": events, "plans": plans, "features": features}


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def comparison(left, right):
    return {
        "equal": left == right,
        "reference_counts": {key: len(items) for key, items in left.items()},
        "recalculated_counts": {key: len(items) for key, items in right.items()},
        "reference_sha256": fingerprint(left), "recalculated_sha256": fingerprint(right),
    }


def verify(data_root, logs_dir):
    config = _read_json(_read(REGISTRATION, ARCHIVE_REGISTRATION_SHA))
    frames = load_archive(config, data_root, logs_dir)
    m30 = frames[30][0]
    m30_closes = [bar["time_close"] for bar in m30]
    costs = Costs()
    settings = Settings()
    results = {}
    for timeframe, (bars, _, bars_sha) in sorted(frames.items()):
        earlier = m30[:bisect_right(m30_closes, bars[0]["time"])]
        context = confirmed_context(bars, earlier)
        reference = scan(bars, costs, minutes=timeframe, context=context, settings=settings)
        prefixes, shortened = [], []
        for stop in (len(bars) // 2, len(bars) * 3 // 4):
            prefix = bars[:stop]
            reduced_context = confirmed_context(prefix, earlier)
            recalc = scan(prefix, costs, minutes=timeframe, context=reduced_context, settings=settings)
            prefixes.append({
                "chart_bars": stop, "last_close_utc_ms": prefix[-1]["time_close"],
                "confirmed_context_equal": context[:stop] == reduced_context,
                **comparison(normalized(reference, bars, stop=stop), normalized(recalc, prefix)),
            })
        for dropped in DROPPED_BARS:
            tail = bars[dropped:]
            # Keep higher-timeframe prehistory available when truncating chart bars.
            reduced_context = confirmed_context(tail, earlier + bars[:dropped])
            recalc = scan(tail, costs, minutes=timeframe, context=reduced_context, settings=settings)
            start = dropped + RANGE_WARMUP_BARS
            shortened.append({
                "chart_bars_removed": dropped, "chart_warmup_bars": RANGE_WARMUP_BARS,
                "first_compared_close_utc_ms": bars[start]["time_close"],
                "confirmed_context_equal": context[dropped:] == reduced_context,
                **comparison(normalized(reference, bars, start=start),
                             normalized(recalc, tail, start=RANGE_WARMUP_BARS)),
            })
        results[timeframe] = {
            "bar_sha256": bars_sha, "chart_bars": len(bars), "earlier_m30_bars": len(earlier),
            "last_close_utc_ms": bars[-1]["time_close"], "prefixes": prefixes, "later_starts": shortened,
        }
    passed = all(row["equal"] and row["confirmed_context_equal"] for frame in results.values()
                 for row in frame["prefixes"] + frame["later_starts"])
    return {
        "status": "VERIFIED" if passed else "INCONCLUSIVE",
        "symbol": config["market"], "gold_archive_registration_sha256": ARCHIVE_REGISTRATION_SHA,
        "wick_settings": asdict(settings), "cost_settings": asdict(costs), "frames": results,
        "scope": ("Archived confirmed-bar comparison of every wick event, plan geometry and feature, "
                  "with local bar indices normalized to UTC. Higher-timeframe prehistory is retained "
                  "for later-start cases."),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=ROOT / "../mission")
    parser.add_argument("--logs-dir", type=Path, default=ROOT / "../tv-final")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = verify(args.data_root, args.logs_dir)
    _write(args.out, report)
    chart_bars = sum(row["chart_bars"] for row in report["frames"].values())
    print(f"{report['status']} · {chart_bars} confirmed chart bars · 6 prefixes · 12 later starts")
    return int(report["status"] != "VERIFIED")


if __name__ == "__main__":
    raise SystemExit(main())
