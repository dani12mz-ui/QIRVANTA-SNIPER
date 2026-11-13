"""Causal M5 lead observations and separate, quote-side research execution."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
from zipfile import ZipFile

from gold_pro.direct_breakout_research import BASE, STRESS
from gold_pro.replay import round_away
from research.professional_signal_selector.native_qa import log_messages


HERE = Path(__file__).resolve().parent
PROTOCOL_PATH = HERE / "FROZEN_PROTOCOL.json"
PROTOCOL_SHA = "e7adbaedc1e9519256729439e59b09156aab8e9876901e40aac18ff5b564462c"
RULES = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
SYMBOL = RULES["symbol"]
TICK = RULES["tick"]
STEP = RULES["bar_minutes"] * 60_000
SIDES = (1, -1)
EPSILON = 1e-8
WATCH = RULES["watch"]
BREAK = RULES["break"]
RETEST = RULES["retest"]
ENTRY = RULES["entry"]


@dataclass(frozen=True)
class Setup:
    side: int
    level: float
    origin: int
    bar: int
    atr: float | None = None
    opposite: float | None = None
    stop: float | None = None
    obstacle: float | None = None

    @property
    def id(self):
        return f"{SYMBOL}|5|{self.side}|{self.origin}|{self.level:.3f}"


def contiguous(bar, previous):
    return bool(previous and bar["time"] == previous["time_close"]
                and bar["time_close"] - bar["time"] == STEP
                and previous["time_close"] - previous["time"] == STEP)


def edge(bars, i, length, side):
    if i < length:
        return None
    field, extreme = ("high", max) if side == 1 else ("low", min)
    return extreme(b[field] for b in bars[i-length:i])


def atr_before(bars):
    total = 0.0
    atr = previous = None
    length = RULES["atr"]["length"]
    for i, bar in enumerate(bars):
        yield atr
        high, low = bar["high"], bar["low"]
        true_range = high - low if previous is None else max(high - low, abs(high - previous), abs(low - previous))
        previous = bar["close"]
        if i < length:
            total += true_range
            if i == length - 1:
                atr = total / length
        else:
            atr += (true_range - atr) / length


def candle(bar, side, prior_atr):
    span = bar["high"] - bar["low"]
    body = side * (bar["close"] - bar["open"])
    favorable = bar["close"] - bar["low"] if side == 1 else bar["high"] - bar["close"]
    return (span > 0 and body > 0 and favorable / span >= BREAK["close_location_min"]
            and (not prior_atr or body >= BREAK["body_atr_min"] * prior_atr))


def plan(bars, i, origin, prior_atr):
    side = origin.side
    opposite = edge(bars, i, BREAK["lookback"], -side)
    stop = round_away(opposite - side * ENTRY["stop_buffer_atr"] * prior_atr, TICK, -side)
    obstacle = edge(bars, i, ENTRY["obstacle_lookback"], side)
    if obstacle is not None and side * (obstacle - origin.level) <= EPSILON:
        obstacle = None
    return Setup(side, origin.level, origin.origin, i, prior_atr, stop=stop, obstacle=obstacle)


def gate(bars, i, pending, costs):
    bar = bars[i]
    if not contiguous(bar, bars[i-1]):
        return "ENTRY_GAP", {}
    side, midpoint = pending.side, bar["open"]
    risk = side * (midpoint - pending.stop)
    friction = costs.spread + 2 * (costs.slip + costs.commission)
    room = None if pending.obstacle is None or risk <= 0 else (side * (pending.obstacle - midpoint) - friction) / risk
    cost_r = friction / risk if risk > 0 else None
    fields = {"entry_ms": bar["time"], "entry": midpoint, "room_r": room, "cost_r": cost_r}
    if risk - costs.spread / 2 <= EPSILON:
        return "OPEN_THROUGH_STOP", fields
    distance = side * (midpoint - pending.level)
    if distance <= EPSILON:
        return "OPEN_BACK_INSIDE", fields
    if distance > BREAK["max_extension_atr"] * pending.atr + EPSILON:
        return "OPEN_EXTENDED", fields
    if risk < ENTRY["minimum_stop_atr"] * pending.atr - EPSILON:
        return "STOP_TOO_SMALL", fields
    if risk > ENTRY["maximum_stop_atr"] * pending.atr + EPSILON:
        return "STOP_TOO_WIDE", fields
    if pending.obstacle is None:
        return "OBSTACLE_UNKNOWN", fields
    if side * (pending.obstacle - midpoint) <= EPSILON:
        return "OBSTACLE_CROSSED", fields
    if room < ENTRY["min_net_room_r"] - EPSILON:
        return "INSUFFICIENT_NET_ROOM", fields
    if cost_r > ENTRY["max_cost_r"] + EPSILON:
        return "COST_RISK", fields
    return "GEOMETRY_VALID", fields


def replay(bars, costs=BASE):
    """Return ordered events for a strictly chronological tape with timestamps in ms."""
    if any(b["time"] <= a["time"] or b["time"] < a["time_close"] for a, b in zip(bars, bars[1:])):
        raise ValueError("Overlapping or unordered market data")
    if any(not all(isfinite(b[k]) for k in ("open", "high", "low", "close"))
           or not b["low"] <= min(b["open"], b["close"]) <= max(b["open"], b["close"]) <= b["high"]
           or b["time_close"] <= b["time"] for b in bars):
        raise ValueError("Invalid OHLC or duration")

    events = []
    watch, retest, prev_qualified = {}, {}, {}
    pending = None
    segment = 0

    for i, (bar, prior_atr) in enumerate(zip(bars, atr_before(bars))):
        prior_atr = prior_atr if prior_atr is not None and prior_atr > 0 else None
        stamp = bar["time_close"]
        gap = i > 0 and not contiguous(bar, bars[i-1])
        full = stamp - bar["time"] == STEP
        segment = segment + 1 if full and not gap else int(full)

        def emit(event, setup, reason, *, atr=None, stop=None, obstacle=None, **fields):
            events.append(dict(event=event, id=setup.id, known_ms=stamp, side=setup.side, level=setup.level,
                               reason=reason, entry_ms=None, entry=None, stop=stop, obstacle=obstacle,
                               atr=atr, room_r=None, cost_r=None,
                               m15=bar.get("m15"), h1=bar.get("h1"), h4=bar.get("h4")) | fields)

        if pending:
            reason, fields = gate(bars, i, pending, costs)
            emit("ENTRY_CANDIDATE" if reason == "GEOMETRY_VALID" else "REJECTED",
                 pending, reason, atr=pending.atr, stop=pending.stop, obstacle=pending.obstacle, **fields)
            pending = None

        levels3 = {s: edge(bars, i, BREAK["lookback"], s) for s in SIDES}
        breaks = {s: level for s, level in levels3.items()
                  if level is not None and s * (bar["close"] - level) > EPSILON
                  and s * (bars[i-1]["close"] - level) <= EPSILON and candle(bar, s, prior_atr)}

        def break_reason(side, level, *, fresh=False):
            if segment <= BREAK["lookback"]:
                return "DATA_GAP"
            if prior_atr is None:
                return "ATR_UNAVAILABLE"
            if not fresh and len(breaks) == 2:
                return "TWO_SIDED"
            if side * (bar["close"] - level) > BREAK["max_extension_atr"] * prior_atr + EPSILON:
                return "EXTENDED"
            return None

        watch_at_start = set(watch)
        for side in SIDES:
            item = watch.get(side)
            if not item:
                continue
            if gap:
                event, reason = "WATCH_EXPIRED", "DATA_GAP"
            elif side * (bar["close"] - item.opposite) < -EPSILON:
                event, reason = "WATCH_INVALIDATED", "STRUCTURE_LOST"
            elif side in breaks and not break_reason(side, breaks[side]) and side * (bar["close"] - item.level) > EPSILON:
                event, reason = "WATCH_COMPLETED", "BREAK_CONFIRMED"
            elif i - item.bar >= WATCH["ttl_bars"]:
                event, reason = "WATCH_EXPIRED", "WATCH_TTL"
            else:
                continue
            emit(event, item, reason)
            del watch[side]

        for side in SIDES:
            item = retest.get(side)
            if item:
                adverse = bar["low"] if side == 1 else bar["high"]
                tolerance = RETEST["tolerance_atr"] * item.atr
                distance = side * (bar["close"] - item.level)
                if gap:
                    emit("RETEST_EXPIRED", item, "DATA_GAP")
                elif distance < -tolerance - EPSILON:
                    emit("RETEST_INVALIDATED", item, "STRUCTURE_LOST")
                elif (segment > BREAK["lookback"] and prior_atr and candle(bar, side, prior_atr)
                      and side * (adverse - item.level) <= tolerance + EPSILON and distance > EPSILON):
                    reason = break_reason(side, item.level, fresh=True)
                    if reason:
                        emit("REJECTED", item, reason, atr=prior_atr)
                    else:
                        pending = plan(bars, i, item, prior_atr)
                        emit("SECOND_CHANCE", pending, "RETEST_CONFIRMED",
                             atr=pending.atr, stop=pending.stop, obstacle=pending.obstacle)
                elif i - item.bar >= RETEST["ttl_bars"]:
                    emit("RETEST_EXPIRED", item, "RETEST_TTL")
                else:
                    continue
                del retest[side]
            elif side in breaks:
                level = breaks[side]
                item = Setup(side, level, stamp, i, prior_atr)
                reason = break_reason(side, level)
                if reason:
                    emit("REJECTED", item, reason, atr=prior_atr)
                    if reason == "EXTENDED":
                        retest[side] = item
                else:
                    pending = plan(bars, i, item, prior_atr)
                    emit("MICRO_BREAK", pending, "CONFIRMED", atr=pending.atr,
                         stop=pending.stop, obstacle=pending.obstacle)

        for side in SIDES:
            level = edge(bars, i, WATCH["lookback"], side) if segment > WATCH["lookback"] and prior_atr else None
            qualified = None
            if level is not None:
                distance = side * (level - bar["close"])
                field = "low" if side == 1 else "high"
                if (-EPSILON <= distance <= WATCH["distance_atr_max"] * prior_atr + EPSILON
                        and side * (bar[field] - bars[i-1][field]) > EPSILON
                        and side * (bars[i-1][field] - bars[i-2][field]) > EPSILON):
                    qualified = level
            if qualified is not None and qualified != prev_qualified.get(side) and side not in watch_at_start:
                item = Setup(side, level, stamp, i, prior_atr, edge(bars, i, WATCH["lookback"], -side))
                emit("EARLY_SETUP", item, "NEAR_STRUCTURE", atr=prior_atr)
                watch[side] = item
            prev_qualified[side] = qualified

    return events


def execute(event, bars, costs, priority_close=None, *, index_by_open=None):
    """Quote-side stop-first replay, starting at the conditional open modeled by C."""
    result = {key: event[key] for key in ("id", "side", "entry_ms")}
    result.update(status="UNEXECUTABLE", reason="UNKNOWN_GEOMETRY", exit_bar=None,
                  exit_ms=None, fill=None, exit_price=None, initial_risk=None,
                  gross_r=None, net_r=None, cost_r=None, adverse=None, favorable=None)
    midpoint, stop, target = (event.get(k) for k in ("entry", "stop", "obstacle"))
    if None in (midpoint, stop, target, event["entry_ms"]):
        return result
    side = event["side"]
    risk = side * (midpoint - stop)
    half_spread = costs.spread / 2
    if risk <= half_spread + EPSILON or side * (target - midpoint) <= EPSILON:
        return result
    if index_by_open is None:
        index_by_open = {b["time"]: i for i, b in enumerate(bars)}
    start = index_by_open[event["entry_ms"]]
    fill = midpoint + side * (half_spread + costs.slip)
    adverse = favorable = 0.0
    result.update(status="CENSORED", reason="END_OF_DATA", fill=fill, initial_risk=risk)
    for i in range(start, len(bars)):
        bar = bars[i]
        opening = bar["open"] - side * half_spread
        low_quote = bar["low"] - side * half_spread
        high_quote = bar["high"] - side * half_spread
        worse, better = (low_quote, high_quote) if side == 1 else (high_quote, low_quote)
        reason, price, mid_exit = None, None, None
        if side * (opening - stop) <= EPSILON:
            reason, price, mid_exit = "SL_GAP", opening - side * costs.slip, bar["open"]
        elif side * (opening - target) >= -EPSILON:
            reason, price, mid_exit = "TARGET_AT_OPEN", target, target + side * half_spread
        elif priority_close is not None and bar["time"] >= priority_close:
            reason, price, mid_exit = "S5_PRIORITY", opening - side * costs.slip, bar["open"]
        elif i > start and not contiguous(bar, bars[i-1]):
            reason, price, mid_exit = "SESSION_GAP_EXIT", opening - side * costs.slip, bar["open"]
        elif i - start >= RULES["execution"]["max_hold_bars"]:
            reason, price, mid_exit = "TIME_EXIT", opening - side * costs.slip, bar["open"]
        elif side * (worse - stop) <= EPSILON:
            reason = "SL_FIRST_BOTH" if side * (better - target) >= -EPSILON else "SL"
            price, mid_exit = stop - side * costs.slip, stop + side * half_spread
        elif side * (better - target) >= -EPSILON:
            reason, price, mid_exit = "TARGET", target, target + side * half_spread

        if reason:
            # On an exit bar only the opening quote and the exit are chronologically known.
            adverse = max(adverse, side * (fill - opening), side * (fill - price))
            favorable = max(favorable, side * (opening - fill), side * (price - fill))
            gross_r = side * (mid_exit - midpoint) / risk
            net_r = (side * (price - fill) - 2 * costs.commission) / risk
            return result | dict(status="CLOSED", reason=reason, exit_bar=i, exit_ms=bar["time_close"],
                                 exit_price=price, gross_r=gross_r, net_r=net_r, cost_r=gross_r - net_r,
                                 adverse=adverse, favorable=favorable)
        adverse = max(adverse, side * (fill - worse))
        favorable = max(favorable, side * (better - fill))
    return result | dict(adverse=adverse, favorable=favorable)


def market_rows(messages):
    bars = []
    for message in messages:
        if message.startswith("DT_END|"):
            break
        if message.startswith("DT_MARKET|"):
            row = json.loads(message.partition("|")[2])
            if row["symbol"] != SYMBOL or row["timeframe"] != 5 or row["index"] != len(bars):
                raise ValueError("Unexpected symbol, timeframe or missing market row")
            bars.append(row)
    return bars


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, nargs="?", default=HERE.parent / "luminous_visual_v2/evidence/native-pairs.zip")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stress", action="store_true")
    args = parser.parse_args()
    digest = sha256(PROTOCOL_PATH.read_bytes()).hexdigest()
    if digest != PROTOCOL_SHA:
        raise ValueError("Frozen protocol checksum changed")
    if args.source.suffix == ".zip":
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as scratch, ZipFile(args.source) as archive:
            source = Path(archive.extract("candidate-M5.csv", scratch))
            messages = log_messages(source)
    else:
        messages = log_messages(args.source)
    bars = market_rows(messages)
    if not bars:
        raise ValueError("No native market rows")
    payload = {"source_sha256": sha256(args.source.read_bytes()).hexdigest(),
               "protocol_sha256": digest, "bars": len(bars),
               "events": replay(bars, STRESS if args.stress else BASE)}
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
