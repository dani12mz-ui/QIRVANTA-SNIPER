"""Closed-campaign accounting and dependent confidence intervals for research replays."""

from collections import Counter
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import math
import random

from research.validation.statistics import quantile, wilson


FLAT_EPSILON = Decimal("0.00000001")


def _day(value):
    if type(value) is date:
        return value
    if not isinstance(value, str):
        raise ValueError("Trading-day identifiers must be dates or ISO date strings")
    day = date.fromisoformat(value)
    if day.isoformat() != value:
        raise ValueError("Use YYYY-MM-DD for trading-day identifiers")
    return day


def _decimal(value):
    if isinstance(value, bool):
        raise ValueError("Expected a finite numeric metric")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("Expected a finite numeric metric") from exc
    if not result.is_finite() or not math.isfinite(float(result)):
        raise ValueError("Expected a finite numeric metric")
    return result


def _utc_day(ms):
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=ms)).date()


def marked_drawdown(marks, *, initial_equity=0):
    """Measure drawdown on ordered (UTC milliseconds, equity) marks."""
    peak = _decimal(initial_equity)
    peak_ms = previous = None
    max_drawdown, longest = Decimal(0), 0
    drawdown_start = last_recovery = None
    count = 0
    for stamp, raw_equity in marks:
        if type(stamp) is not int or previous is not None and stamp < previous:
            raise ValueError("Equity marks must have nondecreasing integer UTC timestamps")
        if peak_ms is None:
            peak_ms = stamp
        equity = _decimal(raw_equity)
        decline = peak - equity
        if decline > 0:
            if drawdown_start is None:
                drawdown_start = peak_ms
            max_drawdown = max(max_drawdown, decline)
            longest = max(longest, stamp - drawdown_start)
        else:
            if drawdown_start is not None:
                longest = max(longest, stamp - drawdown_start)
                last_recovery = stamp
                drawdown_start = None
            peak = equity
            peak_ms = stamp
        count += 1
        previous = stamp
    return {
        "marks": count,
        "max_drawdown": float(max_drawdown) if count else None,
        "longest_underwater_ms": longest if count else None,
        "still_underwater": drawdown_start is not None if count else None,
        "last_recovery_ms": last_recovery,
    }


def _bootstrap_days(days, *, block_days, repetitions, seed):
    """Circular blocks over supplied calendar positions, weighting campaigns equally."""
    n = len(days)
    active = sum(bool(day[0]) for day in days)
    report = {
        "method": "CIRCULAR_MOVING_TRADING_DAY_BLOCK_PERCENTILE",
        "day_count": n,
        "days_with_campaigns": active,
        "zero_trade_days": n - active,
        "block_days": block_days,
        "repetitions": repetitions,
        "seed": seed,
        "valid_draws": 0,
        "win_rate_ci95": [None, None],
        "expectancy_r_ci95": [None, None],
        "expectancy_usd_ci95": [None, None],
    }
    if n < 2 * block_days or active < 2:
        return {**report, "status": "INSUFFICIENT_DAY_BLOCKS"}
    rng = random.Random(seed)
    rates, expected_r, expected_cash = [], [], []
    doubled = days + days
    for _ in range(repetitions):
        sampled = []
        while len(sampled) < n:
            start = rng.randrange(n)
            take = min(block_days, n - len(sampled))
            sampled.extend(doubled[start:start + take])
        count, wins, net_cash, net_r = (sum(column) for column in zip(*sampled))
        if count:
            rates.append(wins / count)
            expected_r.append(float(net_r / count))
            expected_cash.append(float(net_cash / count))
    report["valid_draws"] = len(rates)
    if len(rates) < math.ceil(repetitions * 0.9):
        return {**report, "status": "INSUFFICIENT_NONEMPTY_RESAMPLES"}
    return {
        **report,
        "status": "ESTIMATED_DESCRIPTIVE",
        "win_rate_ci95": [quantile(rates, .025), quantile(rates, .975)],
        "expectancy_r_ci95": [quantile(expected_r, .025), quantile(expected_r, .975)],
        "expectancy_usd_ci95": [quantile(expected_cash, .025), quantile(expected_cash, .975)],
    }


def _mean(values):
    return float(sum(values) / len(values)) if values else None


def _ratio(numerator, denominator):
    return float(numerator / denominator) if denominator else None


def campaign_metrics(campaigns, trading_days, *, block_days=5, repetitions=2000, seed=20261114,
                     flat_epsilon=FLAT_EPSILON):
    """Summarize complete campaigns in observed close order.

    Input rows have unique id, close_ms, net_usd and net_r. An optional
    trading_day permits exchange-session dates if the caller supplies the
    matching trading-day calendar. Otherwise the close is grouped by UTC date.
    Pass all observed trading days in order, including those with no exits.
    Open, censored and unrealized positions belong to a separate ledger.
    """
    epsilon = _decimal(flat_epsilon)
    if epsilon < 0 or type(block_days) is not int or block_days < 1:
        raise ValueError("Flat tolerance must be nonnegative and block_days a positive integer")
    if type(repetitions) is not int or repetitions < 100 or type(seed) is not int:
        raise ValueError("Require at least 100 resamples and an integer seed")
    calendar = tuple(_day(day) for day in trading_days)
    if any(a >= b for a, b in zip(calendar, calendar[1:])):
        raise ValueError("Supply distinct trading days in chronological order")
    by_day = {day: [0, 0, Decimal(0), Decimal(0)] for day in calendar}
    ids, classes = set(), Counter()
    cash, returns = [], []
    winners, losers = [], []
    win_returns, loss_returns = [], []
    cash_marks, r_marks = [], []
    cumulative_cash = cumulative_r = Decimal(0)
    previous = previous_day = None
    for item in campaigns:
        identity, stamp = item["id"], item["close_ms"]
        if not isinstance(identity, str) or not identity or identity in ids:
            raise ValueError(f"Campaign ID must be unique and nonempty: {identity!r}")
        if type(stamp) is not int or previous is not None and stamp < previous:
            raise ValueError("Campaign close UTC milliseconds must be ordered")
        day = _utc_day(stamp) if item.get("trading_day") is None else _day(item["trading_day"])
        if day not in by_day or previous_day is not None and day < previous_day:
            raise ValueError(f"Missing or unordered trading day {day} for {identity}")
        net_cash, net_r = _decimal(item["net_usd"]), _decimal(item["net_r"])
        if (net_cash > 0) != (net_r > 0) or (net_cash < 0) != (net_r < 0):
            raise ValueError(f"Cash and R sign differ for {identity}")
        classification = "win" if net_cash > epsilon else "loss" if net_cash < -epsilon else "flat"
        if classification == "win":
            winners.append(net_cash)
            win_returns.append(net_r)
        elif classification == "loss":
            losers.append(net_cash)
            loss_returns.append(net_r)
        classes[classification] += 1
        daily = by_day[day]
        daily[0] += 1
        daily[1] += classification == "win"
        daily[2] += net_cash
        daily[3] += net_r
        ids.add(identity)
        cash.append(net_cash)
        returns.append(net_r)
        cumulative_cash += net_cash
        cumulative_r += net_r
        cash_marks.append((stamp, cumulative_cash))
        r_marks.append((stamp, cumulative_r))
        previous, previous_day = stamp, day

    dd_cash, dd_r = marked_drawdown(cash_marks), marked_drawdown(r_marks)
    return {
        "closed": len(cash),
        "flat_epsilon_usd": format(epsilon, "f"),
        "wins": classes["win"],
        "losses": classes["loss"],
        "flats": classes["flat"],
        "win_rate": _ratio(classes["win"], len(cash)),
        "loss_rate": _ratio(classes["loss"], len(cash)),
        "flat_rate": _ratio(classes["flat"], len(cash)),
        "net_usd": float(cumulative_cash),
        "net_r": float(cumulative_r),
        "expectancy_usd": _mean(cash),
        "expectancy_r": _mean(returns),
        "profit_factor_usd": _ratio(sum(winners), -sum(losers)),
        "profit_factor_r": _ratio(sum(win_returns), -sum(loss_returns)),
        "profit_factor_status": "DEFINED" if losers else "NO_LOSSES" if winners else "NO_DIRECTIONAL_OUTCOMES",
        "average_win_usd": _mean(winners),
        "average_loss_usd": _mean(losers),
        "average_win_r": _mean(win_returns),
        "average_loss_r": _mean(loss_returns),
        "worst_loss_usd": float(min(losers)) if losers else None,
        "worst_loss_r": float(min(loss_returns)) if loss_returns else None,
        "p05_net_r": quantile([float(value) for value in returns], .05),
        "mean_worst_five_percent_r": _mean(sorted(returns)[: (len(returns) + 19) // 20]),
        "realized_campaign_close_drawdown_usd": dd_cash["max_drawdown"],
        "realized_campaign_close_drawdown_r": dd_r["max_drawdown"],
        "realized_campaign_close_underwater_ms_usd": dd_cash["longest_underwater_ms"],
        "realized_campaign_close_underwater_ms_r": dd_r["longest_underwater_ms"],
        "wilson_iid_win_rate_95_descriptive": wilson(classes["win"], len(cash)),
        "day_block_bootstrap": _bootstrap_days(tuple(by_day.values()), block_days=block_days,
                                               repetitions=repetitions, seed=seed),
    }

