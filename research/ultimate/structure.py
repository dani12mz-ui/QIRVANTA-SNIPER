"""Additional causal price structures on top of the frozen research features."""
from collections import deque

from research.features.causal import build_features


def structural_features(bars):
    base = build_features(bars)
    result = []
    trend = 0
    session_day = None
    asian = None
    recent_sweeps = deque(maxlen=5)
    for i, (bar, current) in enumerate(zip(bars, base)):
        prior = base[i - 1] if i else {}
        previous = bars[i - 1] if i else None
        high, low, close = (bar[key] for key in ("high", "low", "close"))
        atr = current["atr"]
        swing_high, swing_low = prior.get("pivot_high"), prior.get("pivot_low")
        contiguous = previous is not None and not current["gap"]
        if current["gap"]:
            recent_sweeps.clear()
            trend = 0

        def crosses(level, side):
            return (contiguous and level is not None and
                    (close - level) * side > 0 >= (previous["close"] - level) * side)

        bos = 1 if crosses(swing_high, 1) else -1 if crosses(swing_low, -1) else 0
        choch = bos if trend and bos == -trend else 0
        displacement = (close - bar["open"]) / atr if atr else None
        mss = choch if choch and displacement is not None and displacement * choch >= .60 else 0
        sweep_up = bool(contiguous and swing_high is not None and high > swing_high > close)
        sweep_down = bool(contiguous and swing_low is not None and low < swing_low < close)
        recent_sweeps.append((sweep_up, sweep_down))
        if bos:
            trend = bos
        order_block = None
        if bos and displacement is not None and displacement * bos >= .60:
            for j in range(i - 1, max(-1, i - 4), -1):
                origin = bars[j]
                if origin["time_close"] != bars[j + 1]["time"]:
                    break
                if (origin["close"] - origin["open"]) * bos < 0:
                    order_block = {"side": bos, "low": origin["low"], "high": origin["high"],
                                   "origin_open": origin["time"], "available_at": bar["time_close"]}
                    break
        fvg = 0
        if i >= 2 and contiguous and not prior["gap"]:
            fvg = 1 if low > bars[i - 2]["high"] else -1 if high < bars[i - 2]["low"] else 0
        midpoint = None
        if swing_high is not None and swing_low is not None and swing_high > swing_low:
            midpoint = (swing_high + swing_low) / 2

        day = bar["time"] // 86_400_000
        start, cutoff = day * 86_400_000, day * 86_400_000 + 6 * 3_600_000
        if day != session_day:
            session_day = day
            asian = None
        if start <= bar["time"] < cutoff:
            if asian is None:
                asian = {"low": low, "high": high, "last": bar["time_close"],
                         "valid": bar["time"] == start}
            else:
                asian["valid"] = asian["valid"] and asian["last"] == bar["time"]
                asian.update(low=min(asian["low"], low), high=max(asian["high"], high),
                             last=bar["time_close"])
        asian_range = None
        if asian and asian["valid"] and asian["last"] == cutoff:
            asian_range = {"low": asian["low"], "high": asian["high"], "available_at": cutoff}

        previous_highs = (swing_high, current["range20_high"], current["prior_utc_day_high"],
                          current["prior_utc_week_high"])
        previous_lows = (swing_low, current["range20_low"], current["prior_utc_day_low"],
                         current["prior_utc_week_low"])
        result.append({
            **current,
            "structure_trend": trend,
            "previous_swing_high": swing_high,
            "previous_swing_low": swing_low,
            "bos": bos, "choch": choch, "mss": mss,
            "sweep_up": sweep_up, "sweep_down": sweep_down,
            "repeated_sweep_up": sum(up for up, _ in recent_sweeps) >= 2,
            "repeated_sweep_down": sum(down for _, down in recent_sweeps) >= 2,
            "signed_displacement_atr": displacement,
            "causal_fvg": fvg, "order_block_proxy": order_block,
            "swing_midpoint": midpoint,
            "swing_value_zone": None if midpoint is None else (
                "PREMIUM" if close > midpoint else "DISCOUNT" if close < midpoint else "MIDPOINT"),
            "asian_range_utc": asian_range,
            "buy_obstacle": nearest_forward(close, 1, previous_highs),
            "sell_obstacle": nearest_forward(close, -1, previous_lows),
            "trend_age_bars": ((result[-1]["trend_age_bars"] + 1) if result and
                               trend and trend == result[-1]["structure_trend"] and contiguous
                               else int(bool(trend))),
            "swing_pullback_fraction": ((swing_high - close if trend > 0 else close - swing_low)
                                       / (swing_high - swing_low)) if trend and midpoint is not None else None,
        })
    return result


def nearest_forward(reference, side, levels):
    distances = [(level - reference) * side for level in levels if level is not None]
    ahead = [distance for distance in distances if distance > 0]
    return reference + side * min(ahead) if ahead else None
