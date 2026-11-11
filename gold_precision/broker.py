"""Reprice fixed archived OANDA fills under explicitly hypothetical PU costs."""

import argparse
from collections import Counter
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
import gzip
from hashlib import sha256
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from gold_pro.archive import REGISTRATION, _read_json, load_archive

TAPE_SHA256 = "41db20178030940d5ecd5dce89b42b6fbf69f2d131c4c52b3f37c360478100a7"
BASE_SPREAD = Decimal("0.35")
BASE_SLIP = Decimal("0.002")
NY = ZoneInfo("America/New_York")
SCENARIOS = {
    "OANDA_REFERENCE": (BASE_SPREAD, BASE_SLIP, Decimal("0.35"), Decimal("0")),
    "PU_ASSUMPTION_LOW": (Decimal("0.30"), Decimal("0.03"), Decimal("0.00"), Decimal("0.05")),
    "PU_ASSUMPTION_CENTRAL": (Decimal("0.50"), Decimal("0.10"), Decimal("0.35"), Decimal("0.10")),
    "PU_ASSUMPTION_STRESS": (Decimal("0.80"), Decimal("0.20"), Decimal("0.50"), Decimal("0.20")),
}


def dec(value):
    return Decimal(str(value))


def rollover_weight(entry_ms, exit_bar_open_ms):
    """Count rollovers strictly inside confirmed holding time. No exit-bar timing guess."""
    begin = datetime.fromtimestamp(entry_ms / 1000, timezone.utc)
    end = datetime.fromtimestamp(exit_bar_open_ms / 1000, timezone.utc)
    date = begin.astimezone(NY).date()
    last = end.astimezone(NY).date()
    weight = 0
    while date <= last:
        rollover = datetime.combine(date, time(17), tzinfo=NY)
        if date.weekday() < 5 and begin < rollover < end:
            weight += 3 if date.weekday() == 2 else 1
        date += timedelta(days=1)
    return weight


def price_leg(part, entry_ms, exit_open_ms, settings):
    spread, slip, round_trip_fee, swap = settings
    units = dec(part["units"])
    source_fee = dec(part["commission"])
    slip_legs = 1 + int(not part["reason"].startswith("TP"))
    source_net = dec(part["net"])
    # Source net already embeds reference fill slippage, commissions and one spread.
    # Replace the fee and spread and add only the slippage difference.
    adjustment = units * ((BASE_SPREAD - spread) + (BASE_SLIP - slip) * slip_legs - round_trip_fee)
    funding = units * swap * rollover_weight(entry_ms, exit_open_ms)
    value = source_net + source_fee + adjustment - funding
    return value, funding


def compare(campaigns, bars):
    aggregate = {}
    for name, settings in SCENARIOS.items():
        peak = net = maximum_drawdown = funding = Decimal(0)
        counts = Counter()
        for row in sorted(campaigns, key=lambda item: (item["release"], item["plan"]["id"])):
            if row["state"] != "CLOSED":
                continue
            plan = row["plan"]
            start = bars[row["entry_bar"]]["time"]
            total = Decimal(0)
            for part in row["exits"]:
                cash, swap = price_leg(part, start, bars[part["bar"]]["time"], settings)
                total += cash
                funding += swap
            counts["campaigns"] += 1
            counts["wins"] += total > 0
            counts["losses"] += total < 0
            net += total
            peak = max(peak, net)
            maximum_drawdown = max(maximum_drawdown, peak - net)
        aggregate[name] = {"assumptions": [float(v) for v in settings], **counts,
                           "net_usd": float(net), "campaign_close_drawdown_usd": float(maximum_drawdown),
                           "hypothetical_swap_usd": float(funding)}
    return aggregate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tape", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--logs-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    blob = args.tape.read_bytes()
    if sha256(blob).hexdigest() != TAPE_SHA256:
        raise ValueError("Original GOLD PRO archive tape SHA-256 differs")
    bars, _, market_sha = load_archive(_read_json(REGISTRATION), args.data_root, args.logs_dir)[5]
    lanes = json.loads(gzip.decompress(blob))["5"]
    result = {
        "status": "EXPERIMENTAL_COST_SENSITIVITY",
        "pu_prime_account_specification": "BLOCKED",
        "scope": "Fixed OANDA price paths, order admissions and simulated quantities. No broker executable volume.",
        "tape_sha256": TAPE_SHA256, "market_sha256": market_sha,
        "parameter_order": ["spread_usd_per_unit_round_trip", "adverse_slip_usd_per_market_or_stop_unit",
                            "commission_usd_per_unit_round_trip", "swap_usd_per_unit_rollover_weight"],
        "lanes": {name: compare(lanes[name]["outcomes"], bars) for name in ("S5", "FAST")},
    }
    with args.out.open("x", encoding="utf-8", newline="\n") as f:
        json.dump(result, f, indent=2, sort_keys=True, allow_nan=False)
        f.write("\n")


if __name__ == "__main__":
    main()
