"""Audit the standalone TradingView ledger against its observed order evidence."""

import argparse
from collections import Counter, defaultdict
import csv
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from hashlib import sha256
import io
import json
from pathlib import Path
from zipfile import ZipFile


CUTOFF = 1794355200000
TOLERANCE = Decimal("0.00000002")
COMMISSION = Decimal("0.175")
RISK_FRACTION = Decimal("0.0025")
SPREAD = Decimal("0.35")
ANNUAL_FINANCING = Decimal("0.04")
EVENTS = {"D20_META", "D20_ANCHOR", "D20_END", "D20_SIGNAL", "D20_FILL",
          "D20_EXIT", "D20_CHANNEL", "D20_REJECT"}


def utc(milliseconds):
    return datetime.fromtimestamp(milliseconds / 1000, UTC).isoformat().replace("+00:00", "Z")


def compare(checks, field, trade_id, observed, expected):
    if isinstance(expected, Decimal):
        if abs(observed - expected) <= TOLERANCE:
            return
    elif observed == expected:
        return
    checks[field].append({"id": trade_id, "native": observed, "expected": expected})


def read_events(data):
    reader = csv.DictReader(io.StringIO(data.decode("utf-8-sig"), newline=""), strict=True)
    if reader.fieldnames != ["Date", "Message"]:
        raise ValueError("Expected Pine Logs CSV Date,Message header")
    records = defaultdict(list)
    for row in reader:
        if set(row) != {"Date", "Message"} or not all(row.values()):
            raise ValueError("Invalid Pine Logs row")
        tag, sep, payload = row["Message"].partition("|")
        if not sep or tag not in EVENTS:
            raise ValueError(f"Unexpected journal record {tag}")
        records[tag].append(json.loads(payload, parse_float=Decimal))
    if any(len(records[tag]) != 1 for tag in ("D20_META", "D20_ANCHOR", "D20_END")):
        raise ValueError("Native capture requires exactly one metadata, anchor and end record")
    return records


def audit(data, frame):
    rows = read_events(data)
    meta, anchor, end = (rows["D20_" + tag][0] for tag in ("META", "ANCHOR", "END"))
    if (meta["symbol"], meta["timeframe"], meta["currency"], meta["last_signal_utc_exclusive"]) != (
            "OANDA:XAUUSD", {"D1": 1440, "H4": 240}[frame], "USD", CUTOFF):
        raise ValueError("Unexpected native instrument, timeframe, currency or cutoff")
    signal_rows, fill_rows, channel_rows, exit_rows = (
        rows["D20_" + tag] for tag in ("SIGNAL", "FILL", "CHANNEL", "EXIT")
    )
    signals, fills, channels, exits = ({row["id"]: row for row in batch} for batch in (
        signal_rows, fill_rows, channel_rows, exit_rows
    ))
    if any(len(batch) != len(keyed) for batch, keyed in (
            (signal_rows, signals), (fill_rows, fills), (channel_rows, channels), (exit_rows, exits))):
        raise ValueError("Duplicate native event for a single-position campaign")
    if not exits.keys() <= fills.keys() <= signals.keys() or not channels.keys() <= fills.keys():
        raise ValueError("Native journal has an event without its earlier order")
    if any(b["bar"] <= a["bar"] for a, b in zip(signal_rows, signal_rows[1:])):
        raise ValueError("Native signals are unordered or duplicate one bar")

    tick, step, pointvalue = map(Decimal, (meta["mintick"], meta["mincontract"], meta["pointvalue"]))
    checks = defaultdict(list)
    same_bar_stops, price_differences = [], []
    gaps = 0

    def estimated_unit_risk(basis, ticks):
        return (ticks * tick * pointvalue + SPREAD * pointvalue + 4 * tick * pointvalue
                + 2 * COMMISSION + basis * pointvalue * ANNUAL_FINANCING / 365)

    def nearest_tick(value):
        return (value / tick + Decimal("0.5")).to_integral_value(rounding=ROUND_FLOOR) * tick

    def verify_price(trade_id, event, row, expected):
        difference = row["price"] - expected
        if abs(difference) <= TOLERANCE:
            return
        price_differences.append({
            "id": trade_id, "event": event, "utc": utc(row["entry_ms"] if event == "FILL" else row["exit_ms"]),
            "raw_open": row["open"], "native_price": row["price"], "nearest_tick_from_logged_quote": expected,
            "difference_ticks": difference / tick,
        })

    for trade_id, signal in signals.items():
        side = signal["side"]
        expected_id = "D20|" + ("L|" if side == 1 else "S|") + str(signal["time_ms"])
        compare(checks, "signal_id", trade_id, trade_id, expected_id)
        compare(checks, "signal_side", trade_id, abs(side), 1)
        compare(checks, "signal_cutoff", trade_id, signal["time_ms"] < CUTOFF, True)
        compare(checks, "warmup", trade_id, signal["bar"] >= meta["warmup_bars"], True)
        compare(checks, "entry_level_cross", trade_id, side * (signal["signal_close"] - signal["entry_level"]) > 0, True)
        ticks = (2 * signal["atr"] / tick).to_integral_value(rounding=ROUND_CEILING)
        compare(checks, "frozen_atr_ticks", trade_id, signal["stop_ticks"], ticks)
        equity = signal["equity"]
        budget = equity * RISK_FRACTION
        unit_risk = estimated_unit_risk(signal["signal_close"], signal["stop_ticks"])
        by_risk = budget / unit_risk
        by_capital = equity / ((signal["signal_close"] + 2 * tick) * pointvalue + COMMISSION)
        qty = (min(by_risk, by_capital) / step).to_integral_value(rounding=ROUND_FLOOR) * step
        compare(checks, "signal_budget", trade_id, signal["risk_budget"], budget)
        compare(checks, "signal_qty", trade_id, signal["qty"], qty)
        compare(checks, "signal_planned_risk", trade_id, signal["risk_usd"], qty * unit_risk)
        compare(checks, "signal_margin_capped", trade_id, signal["margin_capped"], by_capital < by_risk)

    for trade_id, fill in fills.items():
        signal = signals[trade_id]
        side, price, qty = fill["side"], fill["price"], fill["qty"]
        compare(checks, "fill_side", trade_id, side, signal["side"])
        compare(checks, "fill_qty", trade_id, qty, signal["qty"])
        compare(checks, "fill_bar", trade_id, fill["bar"], signal["bar"] + 1)
        compare(checks, "fill_chronology", trade_id, signal["time_ms"] <= fill["entry_ms"] < fill["seen_ms"], True)
        compare(checks, "fill_stop", trade_id, fill["sl"], price - side * signal["stop_ticks"] * tick)
        compare(checks, "fill_planned_risk", trade_id, fill["planned_risk_usd"], signal["risk_usd"])
        risk, capital = qty * estimated_unit_risk(price, signal["stop_ticks"]), qty * (price * pointvalue + COMMISSION)
        compare(checks, "fill_risk", trade_id, fill["fill_risk_usd"], risk)
        compare(checks, "fill_capital_used", trade_id, fill["fill_capital_used_usd"], capital)
        compare(checks, "fill_risk_flag", trade_id, fill["fill_risk_over_budget"], risk > signal["risk_budget"])
        compare(checks, "fill_capital_flag", trade_id, fill["fill_capital_over_budget"], capital > signal["equity"])
        verify_price(trade_id, "FILL", fill, nearest_tick(fill["open"] + side * 2 * tick))

    signal_bars = {signal["bar"] for signal in signal_rows}
    for trade_id, trade in exits.items():
        fill = fills[trade_id]
        side, qty, reason = trade["side"], trade["qty"], trade["reason"]
        compare(checks, "exit_side", trade_id, side, fill["side"])
        compare(checks, "exit_qty", trade_id, qty, fill["qty"])
        compare(checks, "exit_entry_time", trade_id, trade["entry_ms"], fill["entry_ms"])
        compare(checks, "exit_entry_price", trade_id, trade["entry"], fill["price"])
        compare(checks, "exit_chronology", trade_id, fill["entry_ms"] <= trade["exit_ms"] < trade["seen_ms"], True)
        compare(checks, "no_reentry_on_closure_bar", trade_id, trade["bar"] not in signal_bars, True)
        fee = 2 * COMMISSION * qty
        pnl = side * (trade["price"] - fill["price"]) * qty * pointvalue - fee
        compare(checks, "exit_commission", trade_id, trade["commission"], fee)
        compare(checks, "exit_net_pnl", trade_id, trade["pnl"], pnl)
        if reason == "CHANNEL":
            channel = channels[trade_id]
            compare(checks, "channel_exit_bar", trade_id, trade["bar"], channel["bar"] + 1)
            compare(checks, "channel_chronology", trade_id, channel["time_ms"] <= trade["exit_ms"], True)
            verify_price(trade_id, reason, trade, nearest_tick(trade["open"] - side * 2 * tick))
        elif reason == "SL":
            same_bar = trade["bar"] == fill["bar"]
            compare(checks, "entry_bar_stop_flag", trade_id, trade["same_bar_sl"], same_bar)
            gap = side * (trade["open"] - fill["sl"]) <= 0 and not same_bar
            gaps += gap
            raw = trade["open"] if gap else fill["sl"]
            verify_price(trade_id, reason, trade, nearest_tick(raw - side * 2 * tick))
            if same_bar:
                reached = trade["same_bar_low"] <= fill["sl"] if side == 1 else trade["same_bar_high"] >= fill["sl"]
                compare(checks, "entry_bar_stop_range", trade_id, reached, True)
                same_bar_stops.append({
                    "id": trade_id, "utc": utc(trade["exit_ms"]), "bar": trade["bar"],
                    "side": side, "frozen_ticks": signals[trade_id]["stop_ticks"], "fill": fill["price"],
                    "sl": fill["sl"], "exit": trade["price"], "bar_low": trade["same_bar_low"],
                    "bar_high": trade["same_bar_high"],
                })
        else:
            checks["unknown_exit_reason"].append({"id": trade_id, "native": reason})

    closed_through_cutoff = [row for row in exit_rows if row["seen_ms"] <= CUTOFF]
    fills_through_cutoff = [row for row in fill_rows if row["entry_ms"] < CUTOFF and row["seen_ms"] <= CUTOFF]
    closed_net = sum((row["pnl"] for row in closed_through_cutoff), Decimal(0))
    chart_net = sum((row["pnl"] for row in exit_rows), Decimal(0))
    open_ids = fills.keys() - exits.keys()
    open_entry_fee = sum((fills[trade_id]["qty"] * COMMISSION for trade_id in open_ids), Decimal(0))
    compare(checks, "end_closed_count", None, end["closed_trades"], len(exit_rows))
    compare(checks, "end_open_count", None, end["open_trades"], len(open_ids))
    compare(checks, "end_net_profit", None, end["net_profit"], chart_net - open_entry_fee)
    for previous, signal in zip(signal_rows, signal_rows[1:]):
        previous_id = previous["id"]
        if previous_id in fills:
            free = previous_id in exits and exits[previous_id]["bar"] < signal["bar"]
            compare(checks, "position_occupancy", signal["id"], free, True)

    return {
        "journal_sha256": sha256(data).hexdigest(),
        "status": "CHECKS_MATCH" if not checks else "RUNTIME_MISMATCH",
        "scope": "Observed native events and independent arithmetic checks, separate from OHLC replay",
        "meta": meta, "first_loaded_open_utc": utc(meta["first_time"]),
        "anchor": anchor, "end": end, "last_confirmed_close_utc": utc(end["time_close"]),
        "counts": {tag: len(records) for tag, records in sorted(rows.items())},
        "native_cutoff_snapshot": {
            "cutoff_utc": utc(CUTOFF), "fills_confirmed_by_cutoff": len(fills_through_cutoff),
            "closed_by_cutoff": len(closed_through_cutoff),
            "closed_net_usd": closed_net,
            "open_at_cutoff": len(fills_through_cutoff) - len(closed_through_cutoff),
            "closed_reasons": dict(Counter(row["reason"] for row in closed_through_cutoff)),
            "closed_by_side": dict(Counter("BUY" if row["side"] == 1 else "SELL" for row in closed_through_cutoff)),
        },
        "end_closed_ledger_net_usd": chart_net,
        "end_open_entry_commission_usd": open_entry_fee,
        "end_gross_open_mark_usd": end["equity"] - meta["initial_capital"] - end["net_profit"],
        "end_equity_pnl_usd": end["equity"] - meta["initial_capital"],
        "risk_budget_flags": sum(row["fill_risk_over_budget"] for row in fill_rows),
        "capital_budget_flags": sum(row["fill_capital_over_budget"] for row in fill_rows),
        "same_bar_stops": same_bar_stops, "stop_gaps_observed": gaps,
        "decimal_nearest_tick_differences": price_differences,
        "decimal_nearest_tick_note": (
            "Recomputed from printed native open or stop with decimal half-up; a printed half tick "
            "can hide source precision and the emulator's floating-point tie path."
        ),
        "differences": dict(checks),
    }


def run(source):
    names = ("D1", "H4")
    if source.is_dir():
        return {frame: audit((source / f"{frame}.csv").read_bytes(), frame) for frame in names}
    with ZipFile(source) as archive:
        output = {}
        for frame in names:
            matches = [name for name in archive.namelist() if Path(name).name == f"{frame}.csv"]
            if len(matches) != 1:
                raise ValueError(f"Expected exactly one {frame}.csv archive member")
            output[frame] = audit(archive.read(matches[0]), frame)
        return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="directory or ZIP with default-history D1.csv and H4.csv")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = run(args.source)
    args.output.write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")
    return any(record["status"] != "CHECKS_MATCH" for record in result.values())


if __name__ == "__main__":
    raise SystemExit(main())
