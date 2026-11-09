"""Event replay for predeclared setups on real OHLC. Fills are simulated."""
from collections import defaultdict
from dataclasses import dataclass
from math import isfinite
from research.strategies.library import stop_round


@dataclass(frozen=True)
class Setup:
    id: str
    index: int
    side: int
    reference: float
    stop: float
    atr: float
    boundary: float
    obstacle: float | None = None


COSTS = {"fee_per_unit_per_fill": .175, "market_stop_slippage_ticks": 2,
         "tick": .001, "point_value": 1, "quantity": 3}
EXIT_RULES = {
    "BASE": "Three equal 1/2/3R legs, tighten to actual fill + 0.05 actual R after TP1",
    "FIXED_2R": "Whole size exits at 2R or initial SL; no partial lock",
    "STRUCTURAL": "Whole size exits at known forward obstacle, only if room at least 1R; no partial lock",
    "ATR_STOP": "Initial stop exactly 1.40 signal ATR from reference; otherwise BASE",
    "PARTIAL_50_30_20": "Three 1/2/3R legs, 50/30/20 percent of three raw units; otherwise BASE",
    "BREAK_EVEN": "After TP1, stop tightens to actual entry with no buffer",
    "DELAYED_BREAK_EVEN": "After TP2, stop tightens to actual entry",
    "STRUCTURAL_BREAK_EVEN": "After TP1, tighten to frozen breakout boundary when on safe side of close",
    "ATR_TRAIL": "Tighten after each holding-bar close to close minus signed 2 ATR",
    "MOMENTUM_FAILURE": "BASE plus exit next open after two successive holding closes against reference and directional RSI below 50",
    "TIME_24": "BASE but exit remaining next open after 24 holding bars",
    "INVALIDATION": "BASE plus residual exit next open when holding close is at or beyond frozen boundary against side",
}


def replay(bars, features, setups, variant="BASE", *, fee_multiplier=1, extra_ticks=0):
    if variant not in EXIT_RULES or len(bars) != len(features):
        raise ValueError("unknown replay variant or misaligned feature rows")
    if not isfinite(fee_multiplier) or fee_multiplier < 0 or not isfinite(extra_ticks) or extra_ticks < 0:
        raise ValueError("invalid cost stress")
    by_bar = defaultdict(list)
    ids = set()
    for setup in setups:
        if (setup.id in ids or setup.side not in (-1, 1) or
                not 0 <= setup.index < len(bars) or setup.atr <= 0 or
                not all(isfinite(value) for value in (setup.reference, setup.stop, setup.atr, setup.boundary)) or
                (setup.obstacle is not None and not isfinite(setup.obstacle))):
            raise ValueError("invalid or duplicate setup")
        ids.add(setup.id)
        by_bar[setup.index].append(setup)
    fee = COSTS["fee_per_unit_per_fill"] * fee_multiplier
    tick = COSTS["tick"]
    slip = (COSTS["market_stop_slippage_ticks"] + extra_ticks) * tick
    quantity = COSTS["quantity"]
    plans, refusals, cash_events = [], [], []
    current = None

    def close_leg(plan, leg, price, stamp, reason, precision):
        size = leg["size"]
        gross = (price - plan["entry"]) * plan["side"] * size
        net = gross - 2 * fee * size
        cash_events.append((stamp, gross - fee * size))
        leg["closed"] = True
        plan["exits"].append({"part": leg["part"], "size": size, "price": price,
                              "time": stamp, "precision": precision, "reason": reason,
                              "gross": gross, "fees": 2 * fee * size, "net": net})

    for i, bar in enumerate(bars):
        if current is not None:
            side = current["side"]
            if current["state"] == "PENDING":
                fill = bar["open"] + side * slip
                risk = (fill - current["initial_stop"]) * side
                current.update(state="OPEN", entry=fill, entry_time=bar["time"], entry_index=i,
                               initial_risk_cash=risk * quantity if risk >= tick else None)
                invalid_fill = (risk < tick or abs(fill - current["reference"]) > .20 * current["atr"] or
                                any((leg["target"] - fill) * side <= 0 for leg in current["legs"]))
                current["invalid_fill_observed"] = invalid_fill
                cash_events.append((bar["time"], -fee * quantity))
                current["paid_entry_fee"] = fee * quantity
            live = [leg for leg in current["legs"] if not leg["closed"]]
            if current["scheduled_exit"]:
                for leg in live:
                    close_leg(current, leg, bar["open"] - side * slip, bar["time"],
                              current["scheduled_exit"], "OPEN")
            elif (bar["open"] - current["stop"]) * side <= 0:
                for leg in live:
                    close_leg(current, leg, bar["open"] - side * slip, bar["time"], "SL_GAP", "OPEN")
            else:
                for leg in live:
                    if (bar["open"] - leg["target"]) * side >= 0:
                        close_leg(current, leg, bar["open"], bar["time"], "TP" + str(leg["part"]), "OPEN")
                live = [leg for leg in live if not leg["closed"]]
                adverse = bar["low"] if side == 1 else bar["high"]
                favorable = bar["high"] if side == 1 else bar["low"]
                touched = [leg for leg in live if (favorable - leg["target"]) * side >= 0]
                if (adverse - current["stop"]) * side <= 0:
                    current["ambiguous"] |= bool(touched)
                    for leg in live:
                        close_leg(current, leg, current["stop"] - side * slip,
                                  bar["time_close"], "SL", "INTRABAR_BOUND")
                else:
                    for leg in touched:
                        close_leg(current, leg, leg["target"], bar["time_close"],
                                  "TP" + str(leg["part"]), "INTRABAR_BOUND")
            favorable = bar["high"] if side == 1 else bar["low"]
            adverse = bar["low"] if side == 1 else bar["high"]
            current["mfe_upper_price"] = max(current["mfe_upper_price"], (favorable - current["entry"]) * side)
            current["mae_upper_price"] = max(current["mae_upper_price"], (current["entry"] - adverse) * side)
            current["holding_bars"] = i - current["entry_index"] + 1
            if all(leg["closed"] for leg in current["legs"]):
                current["state"] = "COMPLETE"
                current = None
            else:
                targets = {fill["reason"] for fill in current["exits"]}
                proposed = current["stop"]
                actual_risk = (current["entry"] - current["initial_stop"]) * side
                if variant == "ATR_TRAIL" and features[i]["atr"]:
                    proposed = bar["close"] - side * 2 * features[i]["atr"]
                elif variant == "DELAYED_BREAK_EVEN" and "TP2" in targets:
                    proposed = current["entry"]
                elif variant == "STRUCTURAL_BREAK_EVEN" and "TP1" in targets:
                    if (bar["close"] - current["boundary"]) * side > 0:
                        proposed = current["boundary"]
                elif variant == "BREAK_EVEN" and "TP1" in targets:
                    proposed = current["entry"]
                elif variant not in {"FIXED_2R", "STRUCTURAL", "DELAYED_BREAK_EVEN",
                                    "STRUCTURAL_BREAK_EVEN", "BREAK_EVEN"} and "TP1" in targets:
                    proposed = current["entry"] + side * .05 * actual_risk
                if (proposed - current["stop"]) * side > 0:
                    rounded_stop = stop_round(proposed, side, tick)
                    if (rounded_stop - current["stop"]) * side > 0:
                        current.update(stop=rounded_stop, stop_updates=current["stop_updates"] + 1)
                against = (bar["close"] - current["reference"]) * side < 0
                current["against_streak"] = current["against_streak"] + 1 if against else 0
                rsi = features[i]["rsi"]
                momentum_failure = (variant == "MOMENTUM_FAILURE" and current["against_streak"] >= 2
                                    and rsi is not None and (rsi - 50) * side < 0)
                if current["invalid_fill_observed"]:
                    current["scheduled_exit"] = "INVALID_FILL"
                elif variant == "INVALIDATION" and (bar["close"] - current["boundary"]) * side <= 0:
                    current["scheduled_exit"] = "INVALIDATION"
                elif momentum_failure:
                    current["scheduled_exit"] = "MOMENTUM_FAILURE"
                elif current["holding_bars"] >= (24 if variant == "TIME_24" else 48):
                    current["scheduled_exit"] = "TIME_LIMIT"

        group = by_bar.get(i, ())
        if not group:
            continue
        ordered = sorted(group, key=lambda s: s.id)
        if current is not None or len({s.side for s in group}) > 1:
            reason = "PORTFOLIO_OCCUPIED" if current is not None else "OPPOSING_CONFIRMATIONS"
            refusals.extend({"id": setup.id, "time": bar["time_close"], "reason": reason}
                            for setup in ordered)
            continue
        for setup in ordered:
            if current is not None:
                refusals.append({"id": setup.id, "time": bar["time_close"], "reason": "SAME_BAR_PRIORITY"})
                continue
            stop = (stop_round(setup.reference - setup.side * 1.40 * setup.atr, setup.side, tick)
                    if variant == "ATR_STOP" else setup.stop)
            risk = (setup.reference - stop) * setup.side
            if risk < tick or (variant == "STRUCTURAL" and
                               (setup.obstacle is None or (setup.obstacle - setup.reference) * setup.side < risk)):
                refusals.append({"id": setup.id, "time": bar["time_close"], "reason": "INVALID_RISK_OR_ROOM"})
                continue
            if variant == "FIXED_2R":
                targets, shares = [setup.reference + setup.side * 2 * risk], [1.]
            elif variant == "STRUCTURAL":
                targets, shares = [setup.obstacle], [1.]
            else:
                targets = [setup.reference + setup.side * r * risk for r in (1, 2, 3)]
                shares = [.50, .30, .20] if variant == "PARTIAL_50_30_20" else [1 / 3] * 3
            current = {
                "id": setup.id, "side": setup.side, "decision_time": bar["time_close"],
                "reference": setup.reference, "initial_stop": stop, "stop": stop,
                "boundary": setup.boundary, "atr": setup.atr, "state": "PENDING",
                "entry": None, "entry_time": None, "initial_risk_cash": None, "paid_entry_fee": 0.,
                "scheduled_exit": None, "exits": [], "ambiguous": False, "holding_bars": 0,
                "stop_updates": 0, "against_streak": 0, "mfe_upper_price": 0., "mae_upper_price": 0.,
                "legs": [{"part": j + 1, "size": quantity * share, "closed": False,
                          "target": round(target / tick) * tick}
                         for j, (share, target) in enumerate(zip(shares, targets))],
            }
            plans.append(current)
    for plan in plans:
        filled = sum(fill["size"] for fill in plan["exits"])
        plan["remaining_quantity"] = max(0., quantity - filled)
        plan["net_cash"] = sum(fill["net"] for fill in plan["exits"])
        plan["paid_cash"] = sum(fill["gross"] - fill["fees"] / 2 for fill in plan["exits"]) - plan["paid_entry_fee"]
        plan["net_r"] = plan["net_cash"] / plan["initial_risk_cash"] if plan["initial_risk_cash"] else None
        plan["final_exit_time"] = plan["exits"][-1]["time"] if plan["state"] == "COMPLETE" else None
    return {"plans": plans, "refusals": refusals, "cash_events": cash_events,
            "paid_cash": sum(cash for _, cash in cash_events)}
