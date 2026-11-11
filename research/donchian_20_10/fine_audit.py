"""Audit coarse fills on verified, complete M30 children without rebuilding equity."""

import argparse
from bisect import bisect_left
from collections import Counter
from datetime import datetime, timezone
import json
from math import isclose
from pathlib import Path

from .data import PROTOCOL, aggregate, load_m30, read_bars
from .replay import BASE, SCENARIOS, Instrument, Replay, _price_with_cost, run, verify_prefixes


MINUTE_MS = 60_000


def _utc(stamp):
    return datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def audit_finer(result: Replay, coarse_bars, m30_bars):
    """Track market-open availability and the first M30 stop interval per campaign.

    Coarse trading decisions, size, funding and P&L stay fixed. Intra-M30 ordering
    remains unknown. Return a candidate execution price on the first triggering
    child to expose any finer-resolution gap priced differently by coarse OHLC.
    """
    coarse_bars, m30_bars = tuple(coarse_bars), tuple(m30_bars)
    starts = [bar.time_ms for bar in m30_bars]
    costs, symbol = result.costs, result.instrument
    half_spread = 0 if result.native else costs.spread / 2
    funded = {(event.data["id"], event.data["time_ms"]): event.data["usd"]
              for event in result.events if event.tag == "D20_FINANCING"}

    def children(index):
        parent = coarse_bars[index]
        a = bisect_left(starts, parent.time_ms)
        b = bisect_left(starts, parent.close_ms, lo=a)
        rows = m30_bars[a:b]
        if not rows or any(row.close_ms > parent.close_ms for row in rows):
            raise ValueError(f"Missing or overlapping M30 children at {_utc(parent.time_ms)}")
        if (rows[0].open, max(x.high for x in rows), min(x.low for x in rows), rows[-1].close) != (
                parent.open, parent.high, parent.low, parent.close):
            raise ValueError(f"M30 OHLC does not reconstruct parent at {_utc(parent.time_ms)}")
        return rows

    def entry_row(id_, index, open_ms):
        first = children(index)[0]
        return {"id": id_, "entry_chart_open_utc": _utc(open_ms), "entry_first_m30_open_utc": _utc(first.time_ms),
                "entry_delay_minutes_from_chart_stamp": (first.time_ms - open_ms) / MINUTE_MS,
                "charged_rollover_at_nominal_entry_usd": funded.get((id_, open_ms), 0.0)}

    rows = []
    for trade in result.trades:
        result_row = entry_row(trade.id, trade.entry_bar, trade.entry_ms)
        exit_children = children(trade.exit_bar)
        side = trade.side
        first = exit_children[0]
        at_open = trade.reason == "CHANNEL"
        if trade.reason in {"SL", "SL_GAP"}:
            adverse = (lambda bar: bar.low) if side == 1 else (lambda bar: bar.high)
            first = next((b for b in exit_children
                          if side * (adverse(b) - side * half_spread - trade.stop) <= 1e-10), None)
            if first is None:
                raise ValueError(f"Coarse SL has no M30 trigger for {trade.id}")
            at_open = side * (first.open - side * half_spread - trade.stop) <= 1e-10
        elif not at_open:
            raise ValueError(f"Unsupported exit reason {trade.reason}")
        reference_mid = first.open if at_open else trade.stop + side * half_spread
        finer_price = _price_with_cost(reference_mid, -side, symbol.tick,
                                       costs.adverse_ticks, costs.spread, result.native)
        result_row.update({
            "reason": trade.reason,
            "exit_chart_bar_open_utc": _utc(trade.exit_ms),
            "exit_first_triggering_m30_open_utc": _utc(first.time_ms),
            "exit_time_upper_bound_utc": _utc(first.time_ms if at_open else first.close_ms),
            "exit_exact_m30_open": at_open,
            "exit_delay_minutes_from_chart_stamp": (first.time_ms - trade.exit_ms) / MINUTE_MS,
            "charged_rollover_at_nominal_exit_usd": funded.get((trade.id, trade.exit_ms), 0.0),
            "coarse_exit_price": trade.exit,
            "finer_candidate_exit_price": finer_price,
            "same_exit_price": isclose(finer_price, trade.exit, abs_tol=1e-9, rel_tol=0),
            "finer_cash_delta_if_applied_to_fixed_qty_usd": (
                side * (finer_price - trade.exit) * trade.qty * symbol.pointvalue
            ),
            "same_coarse_bar_sl": trade.entry_bar == trade.exit_bar and trade.reason.startswith("SL"),
        })
        rows.append(result_row)
    open_position = None
    if result.open_trade:
        position = result.open_trade
        open_position = entry_row(position.signal.id, position.entry_bar, position.entry_ms)
    price_differences = [row for row in rows if not row["same_exit_price"]]
    return {
        "scope": "Separate M30 timing and price audit with fixed coarse fills, orders and quantities. "
                 "No reconstruction of intra-M30 ticks, account equity, funding or Bar Magnifier.",
        "closed_campaigns_checked": len(rows),
        "open_position_entry": open_position,
        "exit_counts": dict(Counter(row["reason"] for row in rows)),
        "same_exit_price": len(rows) - len(price_differences),
        "first_price_difference": price_differences[0] if price_differences else None,
        "same_coarse_bar_sl_confirmed_in_m30": sum(row["same_coarse_bar_sl"] for row in rows),
        "delayed_entry_beyond_nominal_bar_stamp": sum(row["entry_delay_minutes_from_chart_stamp"] > 0 for row in rows),
        "funded_nominal_entry_count": sum(bool(row["charged_rollover_at_nominal_entry_usd"]) for row in rows),
        "funded_channel_exit_with_m30_delay_count": sum(
            row["reason"] == "CHANNEL" and row["exit_delay_minutes_from_chart_stamp"] > 0
            and bool(row["charged_rollover_at_nominal_exit_usd"]) for row in rows
        ),
        "rows": rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--local-m30", required=True, type=Path)
    parser.add_argument("--reconstructed", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    registration = json.loads(PROTOCOL.read_text(encoding="utf-8"))
    m30, _ = load_m30(args.local_m30, registration)
    # OANDA:XAUUSD TradingView metadata from both archived native strategy runs.
    symbol = Instrument(tick=0.001, quantity_step=0.01, pointvalue=1.0)
    evidence = {}
    for frame in ("D1", "H4"):
        bars = read_bars(args.reconstructed / f"{frame}.csv")
        rebuilt = aggregate(m30, frame)[0]
        if tuple(bars) != tuple(rebuilt):
            raise ValueError(f"{frame} replay bars differ from the pinned M30 reconstruction.")
        evidence[frame] = {}
        for costs in SCENARIOS:
            evidence[frame][costs.name] = audit_finer(run(bars, symbol, costs), bars, m30)
        evidence[frame]["native_parity_costs"] = audit_finer(run(bars, symbol, BASE, native=True), bars, m30)
        evidence[frame]["prefix_consistency"] = {
            "baseline": verify_prefixes(bars, symbol),
            "native_parity_costs": verify_prefixes(bars, symbol, native=True),
        }
    args.output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
