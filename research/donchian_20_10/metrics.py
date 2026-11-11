"""Readouts for the preregistered Donchian replay, with mark-to-market equity carry."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from math import sqrt
from random import Random
from statistics import mean

from .replay import CUTOFF, HOUR_MS, INITIAL_CAPITAL, START, Replay, utc_ms


PARTITIONS = (
    ("2016–2020", START, utc_ms("2021-01-01T00:00:00Z")),
    ("2021–2023", utc_ms("2021-01-01T00:00:00Z"), utc_ms("2024-01-01T00:00:00Z")),
    ("2024–2026-11-11", utc_ms("2024-01-01T00:00:00Z"), CUTOFF),
)
MONTH_BLOCK = 3
BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_SEED = 2010


def _mean(values):
    return mean(values) if values else None


def _utc(stamp: int | None) -> str | None:
    if stamp is None:
        return None
    return datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def _datetime(stamp: int) -> datetime:
    return datetime.fromtimestamp(stamp / 1000, timezone.utc)


def _wilson(wins, n):
    if n == 0:
        return None
    z = 1.959963984540054
    p = wins / n
    center = p + z * z / (2 * n)
    radius = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    denominator = 1 + z * z / n
    return [max(0.0, (center - radius) / denominator), min(1.0, (center + radius) / denominator)]


def _closed_stats(trades):
    net = [t.net_usd for t in trades]
    wins = [v for v in net if v > 0]
    losses = [v for v in net if v < 0]
    streak = longest = 0
    for v in net:
        streak = streak + 1 if v < 0 else 0
        longest = max(streak, longest)
    ranked_wins = sorted(wins, reverse=True)
    positive = sum(wins)
    concentration = {f"top_{n}_share_of_positive_pnl": sum(ranked_wins[:n]) / positive if positive else None
                     for n in (1, 3, 5)}
    return {
        "closed_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "flat": len(net) - len(wins) - len(losses),
        "win_rate": len(wins) / len(net) if net else None,
        "win_rate_wilson95_independence_assumption": _wilson(len(wins), len(net)),
        "mean_win_usd": _mean(wins),
        "mean_loss_usd": _mean(losses),
        "mean_net_r": _mean([t.net_r for t in trades]),
        "profit_factor": positive / -sum(losses) if losses else None,
        "closed_pnl_usd": sum(net),
        "worst_trade_usd": min(net) if net else None,
        "worst_trade_r": min((t.net_r for t in trades), default=None),
        "longest_losing_streak": longest,
        "mean_holding_hours": _mean([t.holding_hours for t in trades]),
        "mean_holding_hours_upper": _mean([t.max_holding_hours for t in trades]),
        "mean_planned_risk_pct": _mean([100 * t.planned_risk_usd / t.signal_equity_usd for t in trades]),
        "closed_financing_usd": sum(t.financing_usd for t in trades),
        "gross_usd": sum(t.gross_usd for t in trades),
        "spread_usd": sum(t.spread_usd for t in trades),
        "slippage_usd": sum(t.slippage_usd for t in trades),
        "commission_usd": sum(t.commission_usd for t in trades),
        "by_exit": dict(Counter(t.reason for t in trades)),
        "profit_concentration": concentration,
    }


def _mark_at_or_before(marks, stamp):
    return next((m for m in reversed(marks) if m.time_ms <= stamp), None)


def _drawdown(start_ms, initial, marks):
    high = initial
    high_ms = start_ms
    maximum = maximum_pct = 0.0
    peak_ms = trough_ms = None
    peak = None
    for mark in marks:
        value = mark.equity_usd
        if value >= high:
            high, high_ms = value, mark.time_ms
        dd = high - value
        maximum_pct = max(maximum_pct, 100 * dd / high if high > 0 else 0.0)
        if dd > maximum:
            maximum, peak, peak_ms, trough_ms = dd, high, high_ms, mark.time_ms
    recovery = next((m.time_ms for m in marks if trough_ms is not None and m.time_ms > trough_ms
                     and m.equity_usd >= peak), None)
    return {
        "max_mark_to_market_drawdown_usd": maximum,
        "max_mark_to_market_drawdown_pct": maximum_pct,
        "max_drawdown_peak_utc": _utc(peak_ms),
        "max_drawdown_trough_utc": _utc(trough_ms),
        "max_drawdown_recovery_utc": _utc(recovery),
        "max_drawdown_peak_to_recovery_hours": (recovery - peak_ms) / HOUR_MS if recovery else None,
        "max_drawdown_trough_to_recovery_hours": (recovery - trough_ms) / HOUR_MS if recovery else None,
        "max_drawdown_unrecovered": maximum > 0 and recovery is None,
        "underwater_at_end": bool(marks and marks[-1].equity_usd < high),
    }


def _range_overlap(start, end, window_start, window_end):
    return max(0, min(end, window_end) - max(start, window_start))


def _exposure(result, start, end, marks):
    lower = upper = 0
    for t in result.trades:
        lower += _range_overlap(t.entry_ms, t.exit_ms, start, end)
        exit_upper = t.entry_ms + round(t.max_holding_hours * HOUR_MS)
        upper += _range_overlap(t.entry_ms, exit_upper, start, end)
    if result.open_trade:
        dur = _range_overlap(result.open_trade.entry_ms, result.last_score_ms, start, end)
        lower += dur
        upper += dur
    return {
        "scored_bars": len(marks),
        "exposed_bars": sum(mark.exposed for mark in marks),
        "bar_exposure_pct": 100 * sum(mark.exposed for mark in marks) / len(marks) if marks else None,
        "elapsed_exposure_pct_lower": 100 * lower / (end - start) if end > start else None,
        "elapsed_exposure_pct_upper": 100 * upper / (end - start) if end > start else None,
    }


def _slice(result: Replay, from_ms: int, until_ms: int, *, full=False):
    if result.first_score_ms is None or result.last_score_ms is None:
        return {"status": "NO_OBSERVED_COMPLETE_BARS"}
    start, end = max(from_ms, result.first_score_ms), min(until_ms, result.last_score_ms)
    marks = tuple(m for m in result.equity if start < m.time_ms <= end)
    if end <= start or not marks:
        return {"status": "NO_OBSERVED_COMPLETE_BARS"}
    prior = _mark_at_or_before(result.equity, start)
    initial = prior.equity_usd if prior else INITIAL_CAPITAL
    final = marks[-1].equity_usd
    trades = tuple(t for t in result.trades if start <= t.exit_ms < until_ms)
    fees = [e.data["usd"] for e in result.events if e.tag == "D20_FINANCING"
            and (full or start <= e.data["time_ms"] < until_ms)]
    return {
        "status": "OBSERVED",
        "from_utc": _utc(start),
        "until_utc": _utc(end),
        "left_truncated": start > from_ms,
        "right_truncated": end < until_ms,
        "equity_start_usd": initial,
        "equity_end_usd": final,
        "net_mark_to_market_usd": final - initial,
        "net_return_pct": 100 * (final / initial - 1) if initial else None,
        "charged_financing_usd": sum(fees),
        "rollover_events": len(fees),
        **_closed_stats(trades),
        **_drawdown(start, initial, marks),
        **_exposure(result, start, end, marks),
        "direction": {label: _closed_stats([t for t in trades if t.side == side])
                      for label, side in (("BUY", 1), ("SELL", -1))},
    }


def _month_boundary(stamp: int) -> int:
    at = _datetime(stamp)
    return int(datetime(at.year, at.month, 1, tzinfo=timezone.utc).timestamp() * 1000)


def _following_month(stamp: int) -> int:
    at = _datetime(stamp)
    year = at.year + (at.month == 12)
    month = at.month % 12 + 1
    return int(datetime(year, month, 1, tzinfo=timezone.utc).timestamp() * 1000)


def _bootstrap(result: Replay):
    returns = []
    if result.first_score_ms is not None:
        start = _month_boundary(result.first_score_ms)
        if start < result.first_score_ms:
            start = _following_month(start)
        end = _following_month(start)
        while end <= result.last_score_ms:
            prior, after = (_mark_at_or_before(result.equity, stamp) for stamp in (start, end))
            if prior and after and after.time_ms > start and prior.equity_usd > 0:
                returns.append(after.equity_usd / prior.equity_usd - 1)
            else:
                returns.append(None)
            start, end = end, _following_month(end)
    blocks = [returns[i:i + MONTH_BLOCK] for i in range(len(returns) - MONTH_BLOCK + 1)]
    blocks = [x for x in blocks if None not in x]
    observed = [r for r in returns if r is not None]
    base = {
        "quantity": "Mean monthly mark-to-market return, decimal",
        "observed_months": len(observed),
        "partial_edge_months_excluded": True,
        "block_months": MONTH_BLOCK,
        "interval90": None,
        "status": "INSUFFICIENT_COMPLETE_MONTHS",
    }
    if len(observed) < 12 or len(blocks) < 10:
        return base
    random = Random(BOOTSTRAP_SEED)
    samples = []
    for _ in range(BOOTSTRAP_REPLICATES):
        path = []
        while len(path) < len(observed):
            path.extend(random.choice(blocks))
        samples.append(mean(path[:len(observed)]))
    samples.sort()
    return {**base, "status": "DESCRIPTIVE_HISTORICAL_BLOCK_BOOTSTRAP",
            "mean": mean(observed), "seed": BOOTSTRAP_SEED, "replicates": BOOTSTRAP_REPLICATES,
            "interval90": [samples[int(.05 * len(samples))], samples[int(.95 * len(samples)) - 1]]}


def summarize(result: Replay) -> dict:
    """Report complete history, fixed partitions, and calendar years without resetting positions."""
    full = _slice(result, START, result.cutoff_ms, full=True)
    partitions = {name: _slice(result, start, end) for name, start, end in PARTITIONS}
    years = {}
    if result.first_score_ms is not None:
        first, last = _datetime(result.first_score_ms).year, _datetime(result.last_score_ms).year
        for year in range(first, last + 1):
            start, end = (utc_ms(f"{y}-01-01T00:00:00Z") for y in (year, year + 1))
            years[str(year)] = _slice(result, start, end)
    fill_events = [e.data for e in result.events if e.tag == "D20_FILL"]
    signal_events = [e.data for e in result.events if e.tag == "D20_SIGNAL"]
    return {
        "full": full,
        "fixed_partitions_utc": partitions,
        "calendar_years_utc": years,
        "monthly_uncertainty": _bootstrap(result),
        "campaign_lifecycle": {
            "signal_count": len(signal_events),
            "margin_capped_signal_count": sum(row["margin_capped"] for row in signal_events),
            "min_margin_capped_planned_risk_pct": min(
                (100 * row["risk_usd"] / row["equity"] for row in signal_events if row["margin_capped"]),
                default=None,
            ),
            "rejections_by_reason": dict(Counter(e.data["reason"] for e in result.events
                                                if e.tag in {"D20_REJECT", "D20_CAPITAL_REJECT_AT_OPEN"})),
            "fill_count": len(fill_events),
            "risk_over_at_fill_count": sum(row["fill_risk_over_budget"] for row in fill_events),
            "capital_over_at_fill_count": sum(row["fill_capital_over_budget"] for row in fill_events),
            "open_position_id": result.open_trade.signal.id if result.open_trade else None,
            "pending_entry_id": result.pending.id if result.pending else None,
            "pending_exit_at_cutoff": bool(result.open_trade and result.open_trade.channel_exit_ms),
        },
        "costs_actually_charged_including_open": {
            "spread_usd": result.equity[-1].cumulative_spread_usd if result.equity else 0.0,
            "slippage_usd": result.equity[-1].cumulative_slippage_usd if result.equity else 0.0,
            "commission_usd": result.equity[-1].cumulative_commission_usd if result.equity else 0.0,
            "financing_usd": result.equity[-1].financing_usd if result.equity else 0.0,
        },
    }
