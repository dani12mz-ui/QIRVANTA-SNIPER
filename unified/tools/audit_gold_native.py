"""Verify observed GOLD PRO event clocks and next-bar emulator fills."""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re


def audit(path, policy, minutes):
    result = defaultdict(list)
    counts = Counter()
    rejects = Counter()
    event_clock_checks = 0
    price_states = {"ENTRY_VALID", "SUBMITTED", "SIMULATED_FILLED", "POSITION_CLOSED", "STOP_FILLED",
                    "TP1_FILLED", "TP2_FILLED", "TP3_FILLED"}
    event_rows = []
    distinct_alerts = set()
    duplicates = []
    with path.open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            message = row["Message"]
            if not message.startswith('{"schema":"qirvanta.goldpro.event.v1"'):
                continue
            item = json.loads(message)
            chart_open = int(datetime.fromisoformat(row["Date"]).timestamp() * 1000)
            if item["symbol"] != "OANDA:XAUUSD" or item["timeframe"] != str(minutes) or not item["informational"]:
                raise ValueError(f"Invalid event metadata: {path.name}")
            if item["known_at"] != chart_open + minutes * 60_000:
                raise ValueError(f"Event outside observed chart close: {item}")
            event_clock_checks += 1
            state = item["state"]
            counts[state] += 1
            if state in {"ENTRY_REJECTED", "RISK_BLOCKED"}:
                rejects[item["reason"]] += 1
            if state in {"BUY WATCH", "SELL WATCH", "ENTRY_VALID", "SECOND CHANCE", "POSITION_CLOSED"}:
                key = (state, item["id"], item["known_at"])
                if key in distinct_alerts:
                    duplicates.append(key)
                distinct_alerts.add(key)
            if state in price_states:
                result[item["id"]].append({**item, "chart_open": chart_open})
            event_rows.append(item)
    if not event_clock_checks:
        raise ValueError(f"No GOLD PRO observations: {path.name}")
    observed = []
    for id_, rows in result.items():
        sub = [r for r in rows if r["state"] == "SUBMITTED"]
        fills = [r for r in rows if r["state"] == "SIMULATED_FILLED"]
        closed = [r for r in rows if r["state"] == "POSITION_CLOSED"]
        legs = [r for r in rows if r["state"] in {"STOP_FILLED", "TP1_FILLED", "TP2_FILLED", "TP3_FILLED"}]
        if not sub or not fills:
            continue  # Submitted before the start of the bounded journal or still pending.
        if len(sub) != 1 or len(fills) != 1 or len(closed) > 1:
            raise ValueError(f"Unexpected native order lifecycle: {id_}")
        submitted, fill = sub[0], fills[0]
        if fill["chart_open"] < submitted["known_at"] or fill["known_at"] <= submitted["known_at"]:
            raise ValueError(f"Fill occurred before later executable bar: {id_}")
        direction = 1 if fill["direction"] == "BUY" else -1
        check = {"id": id_, "direction": fill["direction"], "submitted_at": submitted["known_at"],
                 "fill_bar_open": fill["chart_open"], "fill_observed_at": fill["known_at"],
                 "entry_price": fill["reference"], "initial_stop": submitted["stop"],
                 "next_contiguous_bar": fill["chart_open"] == submitted["known_at"],
                 "closed_at": closed[0]["known_at"] if closed else None,
                 "close_reason": closed[0]["reason"] if closed else None,
                 "exit_legs": [{"state": r["state"], "price": r["reference"], "observed_at": r["known_at"]} for r in legs]}
        if closed and closed[0]["reason"] in {"SL", "TP3"} and len(legs) == 3:
            # This audit run uses fixed three units and fixed brackets, 1 unit per observed exit.
            check["net_usd_reconstructed"] = round(sum((leg["reference"] - fill["reference"]) * direction - .35
                                                        for leg in legs), 9)
        observed.append(check)
    selected = [r for r in event_rows if r["state"] in {"BUY WATCH", "SELL WATCH", "OPPORTUNITY EXPIRED",
                                                       "ENTRY_VALID", "SECOND CHANCE", "ENTRY_REJECTED"}]
    return {"policy": policy, "minutes": minutes, "sha256": sha256(path.read_bytes()).hexdigest(),
            "event_clock_checks": event_clock_checks, "earliest_logged_close": min(x["known_at"] for x in event_rows),
            "latest_logged_close": max(x["known_at"] for x in event_rows),
            "states": dict(counts), "rejection_reasons": dict(rejects),
            "duplicate_alert_eligible_keys": duplicates, "matched_native_campaigns": observed,
            "examples": [{"state": r["state"], "time_utc": datetime.fromtimestamp(r["known_at"] / 1000, timezone.utc).isoformat(),
                          "direction": r["direction"], "model": r["model"], "reason": r["reason"]}
                         for r in selected[:16]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs", nargs="+", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    results = []
    for path in args.logs:
        match = re.fullmatch(r"gp_native_(fast|confirmed)_M(5|15|30)\.csv", path.name)
        if not match:
            parser.error(f"Unexpected native evidence filename: {path.name}")
        results.append(audit(path, match[1], int(match[2])))
    if {(r["policy"], r["minutes"]) for r in results} != {
        (policy, minutes) for policy in ("fast", "confirmed") for minutes in (5, 15, 30)
    } or len(results) != 6:
        parser.error("Supply six distinct native policy/frame files")
    report = {"status": "VERIFIED_NATIVE_BOUNDED_LIFECYCLE",
              "scope": "Pine logs, fixed three-unit Next-open market simulations. CONFIRMED uses model A.",
              "clock": "known_at is chart close; emulator fill was first observed on the later chart bar.",
              "delivery": "TradingView alert delivery was not exercised.",
              "runs": sorted(results, key=lambda x: (x["policy"], x["minutes"]))}
    with args.out.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False, sort_keys=True)
        stream.write("\n")


if __name__ == "__main__":
    main()
