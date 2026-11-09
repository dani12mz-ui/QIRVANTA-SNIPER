"""Fixed experimental rule families built from observable closed-bar prices."""
from research.strategies.library import stop_round
from research.ultimate.replay import Setup

RULES = {
    "D_OR_FAILURE": "Previous close outside completed London/NY opening range, current close reclaims inside",
    "E_MICRO_PULLBACK": "Direction+EMA20 hold, one small counter candle, then close through its extreme",
    "F_FAILED_AUCTION": "Wick sweeps known 20-bar extreme and reclaims, rejection wick >= 35% of range",
    "G_RANGE_REVERSION": "RANGE regime, touch outer 15% of prior 20-bar envelope, then close back inside it",
    "H_STRUCTURAL_RETEST": "Prior BOS within 1–3 bars, retest its old swing level within 0.15 ATR, trend agrees",
    "I_EXHAUSTION": "Prior RSI <=30/ >=70, favorable RSI change >= 3, reversal body >= 0.25 ATR",
    "J_EXPANSION_ACCEPTANCE": "Prior close broke a preceding 20-bar extreme, current close >=0.1 ATR farther than old level, ATR ratio >=1.35",
    "K_SESSION_ADAPTATION": "LONDON trend-aligned four-bar break, otherwise NEW_YORK opening-range failure",
    "L_TARGET_FEASIBILITY": "Four-bar breakout with known forward obstacle >=1 planned R",
    "L_NO_ROOM_ABLATION": "Identical L setup and risk, omit only the forward-obstacle test",
}


def discover(bars, features):
    output = {name: [] for name in RULES}
    rejection = {name: 0 for name in RULES}
    for i in range(20, len(bars)):
        bar, prev = bars[i], bars[i - 1]
        f, pf = features[i], features[i - 1]
        if not f["ready"] or not pf["ready"] or any(features[j]["gap"] for j in range(i - 3, i + 1)):
            continue
        close, atr, width = bar["close"], f["atr"], f["range"]
        lo20, hi20 = f["range20_low"], f["range20_high"]
        lo4, hi4 = min(x["low"] for x in bars[i - 4:i]), max(x["high"] for x in bars[i - 4:i])
        for side in (1, -1):
            low_side = bar["low"] if side == 1 else bar["high"]
            adverse_prev = prev["low"] if side == 1 else prev["high"]
            obstacle = f["buy_obstacle" if side == 1 else "sell_obstacle"]
            boundary4 = hi4 if side == 1 else lo4
            breakout = (close - boundary4) * side > 0 >= (prev["close"] - boundary4) * side
            trend = f["direction"] == side
            body = (close - bar["open"]) * side
            stop_anchor = min(low_side, adverse_prev) if side == 1 else max(low_side, adverse_prev)
            stop = stop_round(stop_anchor - side * .12 * atr, side, .001)
            risk = (close - stop) * side
            openings = []
            for name, opening in sorted(f["opening_ranges"].items()):
                preceding = pf["opening_ranges"].get(name)
                if preceding and preceding["date"] == opening["date"]:
                    edge = opening["low" if side == 1 else "high"]
                    if (prev["close"] - edge) * side < 0 and opening["low"] < close < opening["high"]:
                        openings.append((name, edge))
            ema_recover = (close - f["ema20"]) * side > 0 and (low_side - f["ema20"]) * side >= -.15 * atr
            micro = (trend and ema_recover and -.50 * pf["atr"] <=
                     (prev["close"] - prev["open"]) * side < 0 and
                     (close - prev["high" if side == 1 else "low"]) * side > 0)
            adverse_level = lo20 if side == 1 else hi20
            wick = f["lower_wick" if side == 1 else "upper_wick"]
            auction = (low_side - adverse_level) * side < 0 < (close - adverse_level) * side and wick >= .35 * width
            inner_edge = adverse_level + side * .15 * (hi20 - lo20)
            range_reversion = (f["regime"] == "RANGE" and (low_side - inner_edge) * side <= 0
                               < (close - inner_edge) * side and lo20 < close < hi20)
            retest = None
            for j in range(i - 1, i - 4, -1):
                if features[j]["bos"] == side:
                    level = features[j]["previous_swing_high" if side == 1 else "previous_swing_low"]
                    if trend and abs(low_side - level) <= .15 * atr and (close - level) * side > 0:
                        retest = level
                    break
            exhausted = ((pf["rsi"] - 50) * side <= -20 and
                         (f["rsi"] - pf["rsi"]) * side >= 3 and body >= .25 * atr)
            old_level = pf["range20_high" if side == 1 else "range20_low"]
            expansion = (old_level is not None and (prev["close"] - old_level) * side > 0
                         >= (bars[i - 2]["close"] - old_level) * side and
                         (close - old_level) * side >= .10 * atr and
                         f["atr_ratio"] >= 1.35)
            ny = next((edge for name, edge in openings if name == "NEW_YORK"), None)
            london = "LONDON" in f["sessions"]
            room = obstacle is not None and (obstacle - close) * side >= risk
            candidates = {
                "D_OR_FAILURE": openings[0][1] if openings else None,
                "E_MICRO_PULLBACK": prev["high" if side == 1 else "low"] if micro else None,
                "F_FAILED_AUCTION": adverse_level if auction else None,
                "G_RANGE_REVERSION": inner_edge if range_reversion else None,
                "H_STRUCTURAL_RETEST": retest,
                "I_EXHAUSTION": adverse_prev if exhausted else None,
                "J_EXPANSION_ACCEPTANCE": old_level if expansion else None,
                "K_SESSION_ADAPTATION": boundary4 if london and trend and breakout else ny if not london else None,
                "L_TARGET_FEASIBILITY": boundary4 if breakout and room else None,
                "L_NO_ROOM_ABLATION": boundary4 if breakout else None,
            }
            if breakout and not room:
                rejection["L_TARGET_FEASIBILITY"] += 1
            for name, boundary in candidates.items():
                if boundary is None:
                    continue
                if not .50 * atr <= risk <= 2.50 * atr:
                    rejection[name] += 1
                    continue
                output[name].append(Setup(f"{name}|{side}|{bar['time_close']}", i, side, close,
                                          stop, atr, boundary, obstacle))
    return output, rejection
