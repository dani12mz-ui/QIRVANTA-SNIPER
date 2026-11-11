"""Reconstruct the full archived TradingView ledger, without replaying absent candles."""

from __future__ import annotations

import argparse
from calendar import monthrange
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from gzip import GzipFile
from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZipFile

from ..native_runtime import COMMISSION, CUTOFF, TOLERANCE, audit, read_events, utc


ARCHIVE = Path(__file__).resolve().parents[1] / "evidence/native-logs.zip"
ARCHIVE_SHA = "58d829a2cc5b299835618c7f5af159b0126d064ef9433219a147ca1537fc325f"
HOUR = 3_600_000
ZERO = Decimal(0)


def _sum(values):
    return sum(values, ZERO)


def _price(value):
    return float(round(value, 8))


def _ratio(numerator, denominator):
    return _price(numerator / denominator) if denominator else None


def _clip_duration(start, end, window_start, window_end):
    return max(0, min(end, window_end) - max(start, window_start))


def _timestamp(stamp):
    return int(stamp.timestamp() * 1000)


def _year_month(epoch):
    stamp = datetime.fromtimestamp(epoch / 1000, UTC)
    return stamp.year, stamp.month


def _month_offset(epoch, months):
    stamp = datetime.fromtimestamp(epoch / 1000, UTC)
    y, m = divmod(stamp.year * 12 + stamp.month - 1 + months, 12)
    day = min(stamp.day, monthrange(y, m + 1)[1])
    return _timestamp(stamp.replace(year=y, month=m + 1, day=day))


def _monthly_ends(start, end):
    y, m = _year_month(start)
    current = _timestamp(datetime(y, m, 1, tzinfo=UTC))
    current = _month_offset(current, 12)
    while current < end:
        if _month_offset(current, -12) >= start:
            yield current
        current = _month_offset(current, 1)
    if _month_offset(end, -12) >= start:
        yield end


def _close_fee(row):
    return row["commission"] - row["qty"] * COMMISSION


def _campaigns(records, score_end):
    meta = records["D20_META"][0]
    fills = {row["id"]: row for row in records["D20_FILL"] if row["seen_ms"] <= score_end}
    exits = {row["id"]: row for row in records["D20_EXIT"] if row["seen_ms"] <= score_end}
    channels = {row["id"]: row for row in records["D20_CHANNEL"] if row["time_ms"] <= score_end}
    pointvalue = Decimal(meta["pointvalue"])
    campaigns = []
    for signal in records["D20_SIGNAL"]:
        if signal["time_ms"] > score_end or signal["time_ms"] >= CUTOFF:
            continue
        id_ = signal["id"]
        fill, exit_ = fills.get(id_), exits.get(id_)
        channel = channels.get(id_)
        side = signal["side"]
        campaign = {
            "id": id_, "direction": "BUY" if side == 1 else "SELL", "signal": signal,
            "entry": fill, "channel": channel, "exit": exit_,
            "status": "CLOSED" if exit_ else "OPEN_CENSORED" if fill else "SUBMITTED_UNFILLED",
        }
        if fill:
            campaign["entry_commission_usd"] = fill["qty"] * COMMISSION
            campaign["signal_to_nominal_fill_hours"] = (fill["entry_ms"] - signal["time_ms"]) / HOUR
        if exit_:
            gross = side * (exit_["price"] - fill["price"]) * fill["qty"] * pointvalue
            net = gross - exit_["commission"]
            if abs(net - exit_["pnl"]) > TOLERANCE:
                raise ValueError(f"Unexpected archived native P&L for {id_}")
            is_gap = (
                exit_["reason"] == "SL" and not exit_["same_bar_sl"]
                and side * (exit_["open"] - fill["sl"]) <= 0
            )
            intrabar = exit_["reason"] == "SL" and not is_gap
            exit_reference = fill["sl"] if intrabar else exit_["open"]
            reference_gross = side * (exit_reference - fill["open"]) * fill["qty"] * pointvalue
            campaign.update({
                "exit_commission_usd": _close_fee(exit_),
                "native_fill_gross_usd": gross,
                "reference_price_gross_usd": reference_gross,
                "native_price_adjustment_usd": reference_gross - gross,
                "native_net_usd": net,
                "stop_gap_at_nominal_open": is_gap,
                "exit_nominal_ms_lower": exit_["exit_ms"],
                "exit_nominal_ms_upper": exit_["seen_ms"] if intrabar else exit_["exit_ms"],
            })
        campaigns.append(campaign)
    return campaigns


def _closed_summary(campaigns):
    values = [c["native_net_usd"] for c in campaigns]
    wins = [p for p in values if p > 0]
    losses = [p for p in values if p < 0]
    fees = _sum(c["exit"]["commission"] for c in campaigns)
    pfs = (_sum(wins), -_sum(losses))
    return {
        "campaigns": len(values),
        "winners": len(wins),
        "losers": len(losses),
        "flat": len(values) - len(wins) - len(losses),
        "profit_factor": _ratio(*pfs),
        "profit_factor_status": "OBSERVED" if losses else "NO_LOSS_DENOMINATOR",
        "native_fill_gross_usd": _price(_sum(c["native_fill_gross_usd"] for c in campaigns)),
        "reference_price_gross_usd": _price(_sum(c["reference_price_gross_usd"] for c in campaigns)),
        "native_price_adjustment_usd": _price(_sum(c["native_price_adjustment_usd"] for c in campaigns)),
        "commission_usd": _price(fees),
        "closed_net_usd": _price(_sum(values)),
        "mean_holding_nominal_hours_lower": _ratio(
            sum(c["exit_nominal_ms_lower"] - c["entry"]["entry_ms"] for c in campaigns),
            len(values) * HOUR,
        ),
        "mean_holding_nominal_hours_upper": _ratio(
            sum(c["exit_nominal_ms_upper"] - c["entry"]["entry_ms"] for c in campaigns),
            len(values) * HOUR,
        ),
        "exit_reasons": dict(sorted(Counter(c["exit"]["reason"] for c in campaigns).items())),
    }


def _realized(campaigns, start, end, initial):
    events = []
    for c in campaigns:
        if c["entry"]:
            events.append((c["entry"]["entry_ms"], -c["entry_commission_usd"]))
        if c["exit"]:
            events.append((c["exit"]["exit_ms"], c["native_fill_gross_usd"] - c["exit_commission_usd"]))
    # Campaign order preserves entry fee before the exit when the timestamps coincide.
    balance = initial + _sum(delta for at, delta in events if at < start)
    beginning = peak = balance
    drawdown = ZERO
    for at, delta in events:
        if start <= at < end:
            balance += delta
            peak = max(peak, balance)
            drawdown = max(drawdown, peak - balance)
    return {
        "start_realized_balance_usd": _price(beginning),
        "end_realized_balance_usd": _price(balance),
        "realized_cash_change_usd": _price(balance - beginning),
        "max_realized_only_drawdown_usd": _price(drawdown),
        "mark_to_market_drawdown": "UNAVAILABLE_FROM_NATIVE_EVENTS",
    }


def _cohort(campaigns, start, end, initial, observed_end):
    closed = [c for c in campaigns if c["exit"] and start <= c["exit"]["exit_ms"] < end]
    exposure = [0, 0]
    for c in campaigns:
        if not c["entry"]:
            continue
        beginning = c["entry"]["entry_ms"]
        lower = c.get("exit_nominal_ms_lower", observed_end)
        upper = c.get("exit_nominal_ms_upper", observed_end)
        exposure[0] += _clip_duration(beginning, lower, start, end)
        exposure[1] += _clip_duration(beginning, upper, start, end)
    return {
        "from_utc": utc(start),
        "until_utc_exclusive": utc(end),
        **_closed_summary(closed),
        **_realized(campaigns, start, end, initial),
        "calendar_exposure_nominal_pct_lower": _ratio(100 * exposure[0], end - start),
        "calendar_exposure_nominal_pct_upper": _ratio(100 * exposure[1], end - start),
        "side": {side: _closed_summary([c for c in closed if c["direction"] == side])
                 for side in ("BUY", "SELL")},
    }


def reconstruct(data, frame):
    qa = audit(data, frame)
    if qa["status"] != "CHECKS_MATCH":
        raise ValueError(f"Unexpected ledger discrepancies in {frame}: {qa['differences']}")
    records = read_events(data)
    meta, end = qa["meta"], qa["end"]
    initial = Decimal(meta["initial_capital"])
    start = end["first_scoring_ms"]
    stop = min(CUTOFF, end["time_close"])
    campaigns = _campaigns(records, stop)
    equity_at_signal = initial
    for campaign in campaigns:
        if abs(campaign["signal"]["equity"] - equity_at_signal) > TOLERANCE:
            raise ValueError(f"Native sizing equity mismatch at {campaign['id']}")
        if campaign["exit"]:
            equity_at_signal += campaign["native_net_usd"]
    full = _cohort(campaigns, start, stop, initial, stop)
    if full["campaigns"] != qa["native_cutoff_snapshot"]["closed_by_cutoff"]:
        raise ValueError("Unexpected archive/cutoff trade count")
    annual = []
    for year in range(_year_month(start)[0], _year_month(stop - 1)[0] + 1):
        y0 = _timestamp(datetime(year, 1, 1, tzinfo=UTC))
        y1 = _timestamp(datetime(year + 1, 1, 1, tzinfo=UTC))
        annual.append({"year": year, "partial": start > y0 or stop < y1,
                       **_cohort(campaigns, max(start, y0), min(stop, y1), initial, stop)})
    rolling = [_cohort(campaigns, _month_offset(m, -12), m, initial, stop)
               for m in _monthly_ends(start, stop)]
    at_end = end["time_close"] <= CUTOFF
    return {
        "frame": frame,
        "archive_journal_sha256": qa["journal_sha256"],
        "chart_first_loaded_open_utc": qa["first_loaded_open_utc"],
        "chart_last_confirmed_close_utc": qa["last_confirmed_close_utc"],
        "scoring": full,
        "annual": annual,
        "rolling_12_month": rolling,
        "status_counts": dict(sorted(Counter(c["status"] for c in campaigns).items())),
        "rejected_orders_through_cutoff": [row for row in records["D20_REJECT"] if row["time_ms"] <= stop],
        "censored_ids": [c["id"] for c in campaigns if not c["exit"]],
        "last_chart_equity_usd": end["equity"],
        "last_chart_equity_at_or_before_cutoff": at_end,
        "last_chart_gross_open_mark_usd": qa["end_gross_open_mark_usd"],
        "last_chart_open_entry_fee_usd": qa["end_open_entry_commission_usd"],
        "decimal_nearest_tick_differences": qa["decimal_nearest_tick_differences"],
        "native_entry_bar_stops": qa["same_bar_stops"],
        "campaigns": campaigns,
    }


def run(source=ARCHIVE):
    source = Path(source)
    raw_sha = sha256(source.read_bytes()).hexdigest()
    if raw_sha != ARCHIVE_SHA:
        raise ValueError(f"Native archive hash differs: {raw_sha}")
    with ZipFile(source) as archive:
        frames = {frame: reconstruct(archive.read(f"{frame}.csv"), frame) for frame in ("D1", "H4")}
    return {
        "scope": "TradingView archived native event reconstruction. No OHLC-generated decisions or intrabar ticks.",
        "native_archive_sha256": raw_sha,
        "native_pnl_model": ("Observed fill-to-fill P&L includes the emulator price adjustment, with 0.175 USD "
                             "per unit per side commission. No spread or financing deduction."),
        "cohort_policy": ("UTC calendar windows and trailing 12 calendar months at month starts plus the final "
                          "boundary, allocated by nominal exit-bar timestamp. Realized balance books entry and "
                          "exit cash separately. Exposure includes calendar closures; SL interval bounds use "
                          "bar open and confirmed close. True executable times can differ at session reopening."),
        "cutoff_utc": utc(CUTOFF),
        "frames": frames,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ARCHIVE)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = run(args.source)
    if args.output.suffix == ".gz":
        with args.output.open("wb") as output, GzipFile(filename="", fileobj=output, mode="wb", mtime=0) as zipped:
            zipped.write((json.dumps(report, separators=(",", ":"), default=str) + "\n").encode("utf-8"))
    else:
        args.output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
