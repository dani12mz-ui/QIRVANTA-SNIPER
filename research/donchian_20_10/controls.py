"""Reproduce the published RC2 controls and re-size their causal offers for comparison."""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from hashlib import sha256
from io import BytesIO
import json
from math import floor, isclose
from pathlib import Path
from statistics import mean
import subprocess
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from gold_pro.rc1_qa import FRAMES, ROOT, compare_exports, read_export
from gold_pro.rc2_research import chart_bars, invalid_open, offers_from_ready, report, utc
from gold_pro.replay import round_away
from research.validation.statistics import cash_metrics

from .replay import CUTOFF, HOUR_MS, INITIAL_CAPITAL, RISK_FRACTION, SCENARIOS, rollovers


CONTROL_COMMIT = "cb848e1e4d6d2686c524ac1a9136804748e9f577"
CONTROL_ROOT = "releases/gold-pro-rc2/evidence"
CONTROL_ZIP_SHA256 = "a058bd9789bf9f5d258a08316217f3bebc8b9951460ee0c499aae848c761813a"
TICK = 0.001
POINTVALUE = 1.0
MIN_CONTRACT = 1.0  # Archived controls verify whole-unit legs; finer increments are unused.
MODES = {"parity": "S5 RAW Parity", "fixed": "RC2 Fixed SL"}


@dataclass
class Position:
    order: dict
    fill_at: int
    fill_mid: float
    fill_price: float
    stop: float
    entry_bar: int
    invalid_open: bool
    active_legs: list[int] = field(default_factory=lambda: [0, 1, 2])
    parts: list[dict] = field(default_factory=list)
    financing: list[dict] = field(default_factory=list)
    exit_pending: str | None = None

    @property
    def units_per_leg(self):
        return self.order["qty"] / 3

    @property
    def remaining(self):
        return self.units_per_leg * len(self.active_legs)

    @property
    def offer(self):
        return self.order["offer"]


def timestamp(value):
    if isinstance(value, int):
        return value
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.utcoffset() is None or result.utcoffset().total_seconds() != 0:
        raise ValueError("Use UTC timestamps with an explicit offset")
    return int(result.timestamp() * 1000)


def pinned_file(name):
    return subprocess.check_output(["git", "show", f"{CONTROL_COMMIT}:{CONTROL_ROOT}/{name}"], cwd=ROOT)


def position_size(offer, cash, costs, minimum=MIN_CONTRACT):
    """Floor to three equal tradable legs after risk and 100% margin caps."""
    reference = offer.reference
    risk_budget = cash * RISK_FRACTION
    stop_distance = (reference - offer.stop) * offer.side
    risk_per_unit = (
        (stop_distance + costs.spread + 2 * (costs.adverse_ticks * TICK)) * POINTVALUE
        + 2 * costs.commission
        + reference * POINTVALUE * costs.annual_financing / 365
    )
    if stop_distance <= 0 or cash <= 0:
        return 0.0, risk_budget, risk_per_unit, False
    risk_limit = risk_budget / risk_per_unit
    margin_per_unit = ((reference + costs.spread / 2 + (costs.adverse_ticks * TICK)) * POINTVALUE
                       + costs.commission)
    margin_limit = cash / margin_per_unit
    step = 3 * minimum
    quantity = floor(min(risk_limit, margin_limit) / step + 1e-10) * step
    return quantity, risk_budget, risk_per_unit, margin_limit < risk_limit


def _position_row(position, costs):
    offer = position.offer
    qty = position.order["qty"]
    planned_risk = qty * position.order["risk_per_unit"]
    initial_distance = (position.fill_price - offer.stop) * offer.side
    fill_risk = (qty * (max(0, initial_distance) * POINTVALUE + (costs.adverse_ticks * TICK) * POINTVALUE
                        + 2 * costs.commission))
    funding = sum(entry["cost_usd"] for entry in position.financing)
    net = sum(part["net_usd"] for part in position.parts) - funding
    fully_closed = not position.active_legs
    spread_half = costs.spread / 2
    slip = costs.adverse_ticks * TICK
    exit_qty = qty - position.remaining
    market_exit_qty = sum(part["qty"] for part in position.parts if not part["reason"].startswith("TP"))
    gross_mid = sum(offer.side * (part["reference_mid"] - position.fill_mid) * part["qty"] * POINTVALUE
                    for part in position.parts)
    spread_closed = 2 * spread_half * exit_qty * POINTVALUE
    slip_closed = (exit_qty + market_exit_qty) * slip * POINTVALUE
    commission_closed = 2 * exit_qty * costs.commission
    if not isclose(gross_mid - spread_closed - slip_closed - commission_closed - funding, net, abs_tol=1e-7):
        raise ValueError(f"Quote-side cost attribution does not reconcile for {offer.id}")
    return {
        "id": offer.id,
        "side": "BUY" if offer.side == 1 else "SELL",
        "signal_utc": utc(offer.at),
        "entry_utc": utc(position.fill_at),
        "entry_mid": position.fill_mid,
        "entry_price": position.fill_price,
        "qty": qty,
        "remaining": position.remaining,
        "stop_initial": offer.stop,
        "stop_last": position.stop,
        "targets": offer.targets,
        "risk_budget_usd": position.order["risk_budget"],
        "planned_risk_usd": planned_risk,
        "planned_risk_fraction": planned_risk / position.order["cash"],
        "fill_stop_risk_before_financing_usd": fill_risk,
        "fill_cash_risk_above_signal_budget": fill_risk > position.order["risk_budget"] + 1e-8,
        "signal_margin_cap_applied": position.order["margin_cap"],
        "invalid_original_open_geometry": position.invalid_open,
        "fully_closed": fully_closed,
        "exit_utc": position.parts[-1]["time_utc"] if fully_closed else None,
        "hold_hours": (position.parts[-1]["time_ms"] - position.fill_at) / HOUR_MS if fully_closed else None,
        "modeled_realized_net_usd": net,
        "realized_net_r": net / planned_risk if fully_closed else None,
        "commission_usd": costs.commission * (qty + qty - position.remaining),
        "spread_usd": spread_half * (qty + exit_qty) * POINTVALUE,
        "slippage_usd": slip * (qty + market_exit_qty) * POINTVALUE,
        "realized_gross_mid_usd": gross_mid,
        "funding_usd": funding,
        "financing_rows": position.financing,
        "parts": position.parts,
    }


def summarize(positions):
    closed = [position for position in positions if position["fully_closed"]]
    gains = [x["modeled_realized_net_usd"] for x in closed]
    wins = [x for x in gains if x > 0]
    losses = [x for x in gains if x < 0]
    return {
        **cash_metrics(gains),
        "mean_net_r": mean(x["realized_net_r"] for x in closed) if closed else None,
        "mean_win_usd": mean(wins) if wins else None,
        "mean_loss_usd": mean(losses) if losses else None,
        "worst_trade_usd": min(gains) if gains else None,
        "mean_hold_hours": mean(x["hold_hours"] for x in closed) if closed else None,
        "closed_funding_usd": sum(x["funding_usd"] for x in closed),
        "closed_commission_usd": sum(x["commission_usd"] for x in closed),
        "closed_spread_usd": sum(x["spread_usd"] for x in closed),
        "closed_slippage_usd": sum(x["slippage_usd"] for x in closed),
        "open_campaigns_censored": len(positions) - len(closed),
    }


def normalized_replay(offers, bars, mode, costs, start, end, minimum=MIN_CONTRACT):
    """Causal portfolio replay of archived READY offers with new sizes and quote-side cash."""
    by_index = {}
    for offer in offers:
        if start <= offer.at < end:
            if offer.index in by_index:
                raise ValueError("Archive contains concurrent READY offers; source priority not established")
            by_index[offer.index] = offer
    cash = peak = INITIAL_CAPITAL
    max_dd = 0.0
    pending = position = previous = None
    positions, decisions, equity = [], [], []
    lifecycle = Counter()
    spread_half = costs.spread / 2
    fee = costs.commission
    market_hours = exposure_hours = 0.0

    def charge(after, through, mark):
        nonlocal cash
        if position is not None and position.active_legs and costs.annual_financing:
            for at, multiplier in rollovers(after, through):
                paid = position.remaining * mark * POINTVALUE * costs.annual_financing * multiplier / 365
                cash -= paid
                position.financing.append({"utc": utc(at), "multiplier": multiplier, "mark": mark, "cost_usd": paid})

    def close_leg(index, px, px_mid, reason, stamp):
        nonlocal cash
        units = position.units_per_leg
        side = position.offer.side
        gross = side * (px - position.fill_price) * units * POINTVALUE
        cash += gross - fee * units
        position.active_legs.remove(index)
        position.parts.append({
            "leg": index + 1,
            "time_ms": stamp,
            "time_utc": utc(stamp),
            "reason": reason,
            "qty": units,
            "price": px,
            "reference_mid": px_mid,
            "net_usd": gross - 2 * fee * units,
        })

    for i, bar in enumerate(bars):
        opening, closing = bar["time"], bar["time_close"]
        if closing < start:
            continue
        if closing > end:
            break
        if opening < start:
            market_hours += (closing - start) / HOUR_MS
        else:
            market_hours += (closing - opening) / HOUR_MS
        if position is not None and previous is not None:
            charge(previous["time_close"], opening, previous["close"])
        if pending is not None:
            offer = pending["offer"]
            fill = bar["open"] + offer.side * (spread_half + (costs.adverse_ticks * TICK))
            if pending["qty"] * (fill * POINTVALUE + fee) > cash + 1e-8:
                lifecycle["NO_CASH_AT_OPEN"] += 1
                decisions.append({"id": offer.id, "utc": utc(opening), "state": "NO_CASH_AT_OPEN", "fill_quote": fill})
            else:
                cash -= fee * pending["qty"]
                position = Position(pending, opening, bar["open"], fill, offer.stop, i, invalid_open(offer, fill))
                lifecycle["FILLED"] += 1
                decisions.append({"id": offer.id, "utc": utc(opening), "state": "FILLED", "qty": pending["qty"]})
            pending = None
        if position is not None:
            side = position.offer.side
            opening_quote = bar["open"] - side * spread_half
            adverse_quote = (bar["low"] if side == 1 else bar["high"]) - side * spread_half
            favorable_quote = (bar["high"] if side == 1 else bar["low"]) - side * spread_half
            first_target = False
            if position.exit_pending or side * (opening_quote - position.stop) <= 0:
                reason = position.exit_pending or "SL_GAP"
                for leg in position.active_legs[:]:
                    close_leg(leg, opening_quote - side * (costs.adverse_ticks * TICK), bar["open"], reason, opening)
            else:
                can_target = i > position.entry_bar or not position.invalid_open
                if can_target:
                    for leg in position.active_legs[:]:
                        target = position.offer.targets[leg]
                        if side * (opening_quote - target) >= 0:
                            close_leg(leg, target, target + side * spread_half, f"TP{leg + 1}_AT_OPEN", opening)
                            first_target |= leg == 0
                # If an exit could occur before or after an in-bar funding boundary,
                # include that charge on the remaining units using the known open.
                charge(opening, closing - 1, bar["open"])
                if side * (adverse_quote - position.stop) <= 0:
                    for leg in position.active_legs[:]:
                        px = position.stop - side * (costs.adverse_ticks * TICK)
                        close_leg(leg, px, position.stop + side * spread_half, "SL", opening)
                elif can_target:
                    for leg in position.active_legs[:]:
                        target = position.offer.targets[leg]
                        if side * (favorable_quote - target) >= 0:
                            close_leg(leg, target, target + side * spread_half, f"TP{leg + 1}", opening)
                            first_target |= leg == 0
                charge(closing - 1, closing, bar["close"])
                if position.active_legs:
                    risk = side * (position.fill_price - position.offer.stop)
                    if i == position.entry_bar and position.invalid_open:
                        position.exit_pending = "INVALID_OPEN_EXIT"
                    elif i - position.entry_bar >= position.offer.max_hold:
                        position.exit_pending = "TIME_EXIT"
                    elif mode == "parity" and first_target and risk >= TICK:
                        protective = round_away(position.fill_price + side * risk * 0.05, TICK, -side)
                        if side * (protective - position.stop) > 0:
                            position.stop = protective
            # At OHLC resolution an intrabar exit is counted as a touched bar.
            if position.active_legs or any(part["reason"] == "SL" or part["reason"] in {"TP1", "TP2", "TP3"}
                                           for part in position.parts if part["time_ms"] == opening):
                exposure_hours += (closing - max(start, opening)) / HOUR_MS
        if position is not None and not position.active_legs:
            positions.append(_position_row(position, costs))
            lifecycle["CLOSED"] += 1
            position = None
        offer = by_index.get(i)
        if offer is not None:
            outcome = {"id": offer.id, "utc": utc(offer.at)}
            if position is not None:
                outcome["state"] = "POSITION_OCCUPIED"
                outcome["blocked_by"] = position.offer.id
            else:
                quantity, budget, risk_per_unit, margin_cap = position_size(offer, cash, costs, minimum)
                outcome.update(qty=quantity, risk_budget=budget, risk_per_unit=risk_per_unit, margin_cap=margin_cap)
                if not quantity:
                    outcome["state"] = "BELOW_THREE_MINIMUM_LEGS"
                else:
                    outcome["state"] = "QUEUED"
                    pending = dict(outcome, offer=offer, cash=cash)
            lifecycle[outcome["state"]] += 1
            decisions.append(outcome)
        marked = cash
        if position is not None:
            side = position.offer.side
            marked += side * (bar["close"] - side * spread_half - position.fill_price) * position.remaining * POINTVALUE
        equity.append({"utc": utc(closing), "cash": cash, "equity": marked})
        peak = max(peak, marked)
        max_dd = max(max_dd, peak - marked)
        previous = bar
    if position is not None:
        positions.append(_position_row(position, costs))
        lifecycle["OPEN_AT_CUTOFF"] += 1
    if pending is not None:
        lifecycle["PENDING_AT_CUTOFF"] += 1
    realized_cash = sum(x["modeled_realized_net_usd"] - x["remaining"] * fee for x in positions)
    if not isclose(realized_cash, cash - INITIAL_CAPITAL, abs_tol=1e-7):
        raise ValueError("Portfolio cash does not reconcile to the campaign ledger")
    final_equity = equity[-1]["equity"] if equity else INITIAL_CAPITAL
    return {
        "observed": {"closed_bars": len(equity), "first_close_utc": equity[0]["utc"] if equity else None,
                     "last_close_utc": equity[-1]["utc"] if equity else None},
        "metrics": {
            **summarize(positions),
            "mark_to_market_drawdown_usd": max_dd,
            "marked_net_return": final_equity / INITIAL_CAPITAL - 1,
            "marked_final_equity_usd": final_equity,
            "realized_cash_including_unclosed_campaigns_usd": cash - INITIAL_CAPITAL,
            "market_time_hours": market_hours,
            "exposure_hours_bar_resolution": exposure_hours,
            "exposure_fraction_bar_resolution": exposure_hours / market_hours if market_hours else None,
            "all_funding_usd": sum(p["funding_usd"] for p in positions),
        },
        "by_direction": {side: summarize([p for p in positions if p["side"] == side]) for side in ("BUY", "SELL")},
        "lifecycle": dict(lifecycle),
        "decisions": decisions,
        "campaigns": positions,
    }


def run(start, end, common_history_proven=False):
    if not start < end <= CUTOFF:
        raise ValueError("Control comparison window must be before the frozen Donchian cutoff")
    raw_zip = pinned_file("native-logs.zip")
    if sha256(raw_zip).hexdigest() != CONTROL_ZIP_SHA256:
        raise ValueError("Unexpected RC2 native archive")
    published = json.loads(pinned_file("research.json"))
    frames = {}
    with TemporaryDirectory() as temp, ZipFile(BytesIO(raw_zip)) as archive:
        folder = Path(temp)
        for mode in MODES:
            for frame in FRAMES:
                name = f"{mode}_M{frame}.csv"
                (folder / name).write_bytes(archive.read(name))
        reproduced = report({mode: (folder, mode) for mode in MODES})
        for frame in FRAMES:
            key = f"M{frame}"
            published_frame = published["frames"][key]
            replayed_frame = reproduced["frames"][key]
            original_s5 = replayed_frame["native_s5_raw_and_rc1_identical"]
            if original_s5 != published_frame["native_s5_raw_and_rc1_identical"]:
                raise ValueError(f"Published S5 RAW reference failed exact reproduction on {key}")
            tapes = {mode: read_export(folder / f"{mode}_{key}.csv", frame, "audit") for mode in MODES}
            offer_parity = compare_exports(tapes["parity"], tapes["fixed"], ("core", "ready"))
            if offer_parity["status"] != "MATCH":
                raise ValueError(f"RC2 and S5 do not share the published READY tape on {key}")
            for mode in MODES:
                original = published_frame["native_rc2"][mode]
                recalculated = replayed_frame["native_rc2"][mode]
                if any(original[item] != recalculated[item] for item in ("source", "descriptive", "strategy_api")):
                    raise ValueError(f"Published native control failed exact reproduction on {key} {mode}")
            bars = chart_bars(tapes["fixed"])
            if start < bars[0]["time"] or end > bars[-1]["time_close"]:
                raise ValueError(f"Control data do not cover the registered comparison window on {key}")
            offers = offers_from_ready(tapes["fixed"], frame)
            frames[key] = {
                "source_coverage": {"first_open_utc": utc(bars[0]["time"]), "last_close_utc": utc(bars[-1]["time_close"]),
                                    "bars": len(bars)},
                "shared_core_and_ready": offer_parity,
                "original_s5_raw_and_rc1_identical": original_s5["all"],
                "published_controls_reproduced": {
                    title: {"source": replayed_frame["native_rc2"][mode]["source"],
                            "all": replayed_frame["native_rc2"][mode]["descriptive"]["all"],
                            "native_properties": replayed_frame["native_rc2"][mode]["strategy_api"]["native_strategy_properties"]}
                    for mode, title in MODES.items()
                },
                "risk_normalized": {
                    title: {costs.name: normalized_replay(offers, bars, mode, costs, start, end)
                            for costs in SCENARIOS}
                    for mode, title in MODES.items()
                },
            }
    return {
        "schema": "qirvanta.donchian-20-10.controls.v1",
        "comparison_status": "INCONCLUSIVE",
        "common_history_proven": common_history_proven,
        "comparison_scope": "Original-frame controls over the requested calendar window. Donchian coverage is evaluated separately. Small samples and no untouched result prevent ranking.",
        "comparison_window_utc": {"start": utc(start), "end": utc(end)},
        "control_commit": CONTROL_COMMIT,
        "control_archive_sha256": CONTROL_ZIP_SHA256,
        "original_reproduction": "Published native campaign, cost and Strategy API aggregates match exactly; original outputs keep their full historical archive including six hours after the Donchian cutoff.",
        "risk_normalization": {
            "starting_equity_usd": INITIAL_CAPITAL,
            "risk_fraction": RISK_FRACTION,
            "pointvalue": POINTVALUE,
            "tick": TICK,
            "minimum_leg_units": MIN_CONTRACT,
            "simulated_margin_fraction": 1.0,
            "financing": "Annual scenario charged at NY 17:00 weekdays, Wednesday triple, using known mark and remaining size. At uncertain intrabar rollover include charge until an exit is observed.",
            "policy": "Use all archived causal READY offers, recompute sizes from current cash and one-position occupancy, and reject before fill if the quote is unaffordable. Preserve the three target legs, close after invalid open, time exit and next-bar S5 +0.05R lock after TP1. RC2 Fixed SL preserves its initial stop. Standard OHLC uses stop first after opening orders and conservative limit fill prices. This model is separate from the frozen native baseline.",
            "cost_scenarios": {costs.name: asdict(costs) for costs in SCENARIOS},
        },
        "frames": frames,
        "additional_controls": {"H1": "NO_ARCHIVED_NATIVE_TAPE", "H4": "NO_ARCHIVED_NATIVE_TAPE"},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2026-11-06T03:00:00Z", type=timestamp)
    parser.add_argument("--end", default="2026-11-11T00:00:00Z", type=timestamp)
    parser.add_argument("--common-history-proven", action="store_true",
                        help="Set only after separately verifying both Donchian frames have this common coverage")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = run(args.start, args.end, args.common_history_proven)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes((json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
