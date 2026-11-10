"""Evaluate frozen V2.1 decisions against registered events and native S5 archives."""

import argparse
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
from decimal import Decimal
import gzip
import json
from math import ceil, floor
from pathlib import Path
from statistics import mean, median

from diagnostics.accounting.audit import closed_drawdown, json_default, stats
from research.data.loader import load_bars
from research.features.causal import Smooth
from research.validation.native_report import utc
from research.validation.statistics import block_bootstrap
from unified.tools.audit_premium_s5 import verify_bars
from unified.tools.audit_premium_v2_quant import read_pinned
from unified.tools.audit_tv_observations import inspect
from unified.tools.build_tradingview_final import ROOT, sha

REGISTRATION = ROOT / "docs/v21_evidence/event_evaluation_registration.json"
HOUR = 3_600_000
SIDES = {"BUY": 1, "SELL": -1}
ZERO = Decimal(0)


@dataclass(frozen=True)
class Watch:
    side: int
    index: int
    level: float
    close: float


@dataclass(frozen=True)
class Decision:
    bar: int
    time: int
    side: int
    state: str
    reason: str
    watch: Watch
    reference: float
    prior_atr: float
    stop: float | None = None
    target: float | None = None


@dataclass(frozen=True)
class Exit:
    bar: int
    time: int
    price: float
    units: Decimal
    commission: Decimal
    net: Decimal
    reason: str


@dataclass(frozen=True)
class Offer:
    id: str
    engine: str
    side: int
    decision_bar: int
    decision_time: int
    entry_bar: int
    entry_time: int | None
    entry_price: float | None
    stop: float
    target: float | None
    units: Decimal
    initial_risk: Decimal | None
    exits: tuple[Exit, ...]

    @property
    def release(self):
        return self.exits[-1].bar if self.exits else float("inf")


def known_pivot(bars, i, radius, key, better):
    """Return a pivot disclosed at this close. Ties disqualify."""
    if i < 2 * radius:
        return None
    center = i - radius
    price = bars[center][key]
    return (center, price) if all(better(price, bars[j][key])
                                 for j in range(i - 2 * radius, i + 1) if j != center) else None


def round_tick(price, tick, direction):
    ticks = price / tick
    return round((ceil(ticks - 1e-8) if direction > 0 else floor(ticks + 1e-8)) * tick, 9)


def scan(bars, cfg, tick):
    """Build decision tape before calculating any fill or outcome."""
    atr = Smooth(14, wilder=True)
    fast, slow = [], None
    radius, span = cfg["pivot_left_and_right_bars"], cfg["range_lookback"]
    prior_pivots = {}
    watch = None
    decisions = []
    for i, bar in enumerate(bars):
        op, high, low, close = (bar[k] for k in ("open", "high", "low", "close"))
        previous_close = bars[i - 1]["close"] if i else close
        prior_atr = atr.value
        atr.add(max(high - low, abs(high - previous_close), abs(low - previous_close)))
        fast.append(close if not fast else 2 / 22 * close + 20 / 22 * fast[-1])
        slow = close if slow is None else 2 / 56 * close + 54 / 56 * slow
        disclosed = {
            -1: known_pivot(bars, i, radius, "high", lambda a, b: a > b),
            1: known_pivot(bars, i, radius, "low", lambda a, b: a < b),
        }
        if i >= cfg["warmup_bars"] and prior_atr is not None and prior_atr > 0:
            consumed = False
            if watch:
                side = watch.side
                def record(state, reason, stop=None, target=None):
                    return Decision(i, bar["time_close"], side, state, reason, watch, close,
                                    prior_atr, stop, target)

                age = i - watch.index
                if age > cfg["watch_ttl_bars"] or side * (close - watch.level) < 0:
                    reason = "TTL" if age > cfg["watch_ttl_bars"] else "LOST_BREAK_LEVEL"
                    decisions.append(record("EXPIRED", reason))
                    watch = None
                elif age > 0:
                    candle_range = high - low
                    body = side * (close - op)
                    favorable_distance = high - close if side == 1 else close - low
                    displacement = (candle_range > 0 and body >= cfg["body_atr"] * prior_atr
                                    and body >= cfg["body_range_fraction"] * candle_range
                                    and favorable_distance <= cfg["close_favorable_range_fraction"] * candle_range)
                    momentum = side * (close - bars[i - 3]["close"]) >= cfg["three_bar_momentum_atr"] * prior_atr
                    trend = min(side * (close - fast[-1]), side * (fast[-1] - slow),
                                side * (fast[-1] - fast[-4])) > 0
                    pivot, preceding = disclosed[side], prior_pivots.get(side)
                    retest = (pivot is not None and preceding is not None and pivot[0] >= watch.index
                              and side * (pivot[1] - preceding[1]) > 0
                              and abs(pivot[1] - watch.level) <= cfg["retest_tolerance_atr"] * prior_atr
                              and side * (close - watch.level) > 0)
                    continuation = side * (close - watch.close) > 0
                    if trend and (displacement or momentum) and (continuation or retest):
                        reason = "RETEST" if retest else "CONTINUATION"
                        decisions.append(record("CONFIRMED", reason))
                        extreme = min if side == 1 else max
                        key = "low" if side == 1 else "high"
                        base = extreme(watch.level, *(b[key] for b in bars[i - 2:i + 1]))
                        stop = round_tick(base - side * cfg["stop_buffer_atr"] * prior_atr, tick, -side)
                        risk = side * (close - stop)
                        target = round_tick(close + side * cfg["target_planned_r"] * risk, tick, side)
                        failed = []
                        if side * (close - fast[-1]) > cfg["maximum_extension_atr"] * prior_atr:
                            failed.append("OVEREXTENSION")
                        if risk < cfg["minimum_stop_atr"] * prior_atr:
                            failed.append("STOP_TOO_SMALL")
                        if risk > cfg["maximum_stop_atr"] * prior_atr:
                            failed.append("STOP_TOO_WIDE")
                        decisions.append(record("REJECTED" if failed else "RISK_VALID",
                                                "+".join(failed) if failed else reason, stop, target))
                        watch = None
                        consumed = True
            if watch is None and not consumed:
                prior = bars[i - span:i]
                upper, lower = max(b["high"] for b in prior), min(b["low"] for b in prior)
                direction = 1 if close > upper else -1 if close < lower else 0
                if direction:
                    watch = Watch(direction, i, upper if direction == 1 else lower, close)
                    decisions.append(Decision(i, bar["time_close"], direction, "WATCH", "STRUCTURAL_BREAK",
                                              watch, close, prior_atr))
        prior_pivots.update({side: pivot for side, pivot in disclosed.items() if pivot is not None})
    return decisions


def model_offer(decision, bars, cfg, tick, pointvalue, maximum_hold):
    """Counterfactual next-open market fill with pre-attached bracket."""
    i, side = decision.bar + 1, decision.side
    units = Decimal(str(cfg["position_units"]))
    slip = cfg["market_or_stop_adverse_slippage_ticks"] * tick
    fee = 2 * units * Decimal(str(cfg["commission_usd_per_unit_per_fill"]))
    price = round(bars[i]["open"] + side * slip, 9) if i < len(bars) else None
    risk = None if price is None or side * (price - decision.stop) < tick - 1e-9 else (
        Decimal(str(round(side * (price - decision.stop), 9))) * units * pointvalue)
    exits = ()
    for j in range(i, min(i + maximum_hold + 1, len(bars))):
        b = bars[j]
        op, hi, lo = b["open"], b["high"], b["low"]
        hostile, favorable = (lo, hi) if side == 1 else (hi, lo)
        if j == i + maximum_hold:
            out, reason = op - side * slip, "TIME"
        elif side * (op - decision.stop) <= 0:
            out, reason = op - side * slip, "STOP_GAP"
        elif side * (op - decision.target) >= 0:
            out, reason = op, "TARGET_GAP"
        elif side * (hostile - decision.stop) <= 0:
            out, reason = decision.stop - side * slip, "STOP"
        elif side * (favorable - decision.target) >= 0:
            out, reason = decision.target, "TARGET"
        else:
            continue
        gross = Decimal(str(round(side * (out - price), 9))) * units * pointvalue
        exits = (Exit(j, b["time"], round(out, 9), units, fee, gross - fee, reason),)
        break
    return Offer(f"IMPULSE|{side}|{decision.time}", "IMPULSE", side, decision.bar, decision.time,
                 i, bars[i]["time"] if price is not None else None, price, decision.stop,
                 decision.target, units, risk, exits)


def observed_offers(campaigns):
    """Preserve the archived S5 plan, actual quantities, timestamps and costs."""
    for c in campaigns:
        meta = c["submitted_metadata"]
        exits = tuple(Exit(p["exit_bar_index"], p["exit_time"], float(p["exit_price"]), abs(p["size"]),
                           p["commission"], p["net_cash"], p["exit_comment"]) for p in c["exit_rows"])
        yield Offer(c["plan_id"], "S5", c["side"], meta["submitted_bar"], meta["submitted_time"],
                    c["entry_bar_index"], c["entry_time"], float(c["actual_entry"]), float(meta["initial_sl"]),
                    None, c["submitted_quantity"], c["initial_risk_cash"], exits)


def occupy(offers, first):
    """One slot, reserving entry until the known close of the exit bar."""
    admitted, blocked = [], []
    current = None
    for order in sorted(offers, key=lambda x: (x.decision_bar, x.engine != first, x.id)):
        if current is not None and order.decision_bar < current.release:
            blocked.append({"id": order.id, "engine": order.engine, "side": order.side,
                            "time": order.decision_time, "state": "ENTRY_REJECTED",
                            "reason": "OCCUPIED", "occupied_by": current.engine, "occupant": current.id})
        else:
            admitted.append(order)
            current = order
    return admitted, blocked


def hourly_events(bars, minutes, cfg):
    groups = defaultdict(list)
    for i, bar in enumerate(bars):
        groups[(bar["time"] // HOUR) * HOUR].append(i)
    prior = deque(maxlen=24)
    events = []
    full = incomplete = eligible = 0
    for start, indices in groups.items():
        if [bars[i]["time"] for i in indices] != list(range(start, start + HOUR, minutes * 60_000)):
            incomplete += 1
            continue
        first, last = indices[0], indices[-1]
        if bars[last]["time_close"] != start + HOUR:
            raise ValueError("Incomplete final chart interval in UTC event bucket")
        full += 1
        high = max(bars[i]["high"] for i in indices)
        low = min(bars[i]["low"] for i in indices)
        op, close = bars[first]["open"], bars[last]["close"]
        span, displacement = high - low, close - op
        if first >= 250 and len(prior) == 24 and median(prior) > 0:
            eligible += 1
            scale = median(prior)
            if (span > 0 and abs(displacement) >= cfg["minimum_absolute_open_to_close_multiple"] * scale
                    and abs(displacement) / span >= cfg["minimum_absolute_open_to_close_over_hour_range"]):
                side = 1 if displacement > 0 else -1
                events.append({"start": start, "end": start + HOUR, "first_bar": first, "last_bar": last,
                               "side": side, "open": op, "close": close, "high": high, "low": low,
                               "scale": scale, "directional_extreme_distance": (high - op if side == 1 else op - low)})
        prior.append(span)
    return events, {"complete_hours": full, "partial_observed_hours": incomplete, "eligible_hours": eligible}


def excursion(trade, event, bars):
    """Price-space excursion while a campaign remains open, before final exit-bar extrema."""
    low = high = trade.entry_price
    for j in range(trade.entry_bar, min(event["last_bar"] + 1, trade.release)):
        low, high = min(low, bars[j]["low"]), max(high, bars[j]["high"])
    for part in trade.exits:
        if part.bar <= event["last_bar"]:
            low, high = min(low, part.price), max(high, part.price)
    favorable, adverse = ((high - trade.entry_price, trade.entry_price - low) if trade.side == 1 else
                          (trade.entry_price - low, high - trade.entry_price))
    return {"favorable": favorable, "adverse": adverse,
            "capture_fraction": favorable / event["directional_extreme_distance"]}


def mark_events(events, bars, decisions, impulse, s5, combined):
    by_hour = defaultdict(list)
    for d in decisions:
        by_hour[(d.time // HOUR) * HOUR].append(d)
    for ev in events:
        start, end, side = ev["start"], ev["end"], ev["side"]
        same_decisions = [d for d in by_hour[start] if d.side == side]
        matched = {}
        for name, trades in (("S5", s5), ("IMPULSE", impulse), ("COMBINED", combined)):
            def delay(times):
                return (min(times) - start) / 60_000 if times else None
            def decision_delay(state):
                return delay([d.time for d in same_decisions if d.state == state])
            same_offers = [t for t in trades if t.side == side]
            admitted = [t for t in same_offers if start <= t.decision_time < end]
            fills = sorted((t for t in same_offers if t.entry_time is not None and start <= t.entry_time < end),
                           key=lambda t: (t.entry_time, t.id))
            picked = fills[0] if fills else None
            missed = None
            if not picked:
                if name == "S5":
                    missed = "ADMITTED_WITHOUT_EVENT_FILL" if admitted else "NO_OBSERVED_ADMISSION"
                elif admitted:
                    missed = "ADMITTED_WITHOUT_EVENT_FILL"
                elif any(d.state == "RISK_VALID" for d in same_decisions):
                    missed = "OCCUPIED"
                elif any(d.state == "REJECTED" for d in same_decisions):
                    missed = "RISK_REJECTED"
                elif any(d.state == "WATCH" for d in same_decisions):
                    missed = "WATCH_WITHOUT_CONFIRMATION"
                else:
                    missed = "NO_NEW_WATCH_OR_CONFIRMATION"
            matched[name] = {
                "watch_minutes": decision_delay("WATCH") if name != "S5" else None,
                "confirmation_minutes": (delay([t.decision_time for t in admitted]) if name == "S5"
                                         else decision_delay("CONFIRMED")),
                "geometry_valid_minutes": decision_delay("RISK_VALID") if name != "S5" else None,
                "admission_minutes": delay([t.decision_time for t in admitted]),
                "fill_minutes": delay([t.entry_time for t in fills]),
                "fills": len(fills), "first_fill_id": picked.id if picked else None,
                "first_fill_engine": picked.engine if picked else None,
                "already_in_position": any(t.entry_time is not None and t.entry_time < start
                                           and t.release >= ev["first_bar"] for t in same_offers),
                "opposite_side_fills": sum(t.side != side and t.entry_time is not None and start <= t.entry_time < end
                                          for t in trades),
                "miss": missed, **(excursion(picked, ev, bars) if picked else {}),
            }
        ev["matches"] = matched


def describe_events(events):
    report = {}
    for name in ("S5", "IMPULSE", "COMBINED"):
        rows = [e["matches"][name] for e in events]
        filled = [r for r in rows if r["fills"]]
        def med(key, records):
            values = [r[key] for r in records if r[key] is not None]
            return median(values) if values else None
        report[name] = {
            "events": len(rows), "known_new_watch": (sum(r["watch_minutes"] is not None for r in rows)
                                                    if name != "S5" else None),
            "known_confirmation": sum(r["confirmation_minutes"] is not None for r in rows),
            "risk_valid": (sum(r["geometry_valid_minutes"] is not None for r in rows) if name != "S5" else None),
            "admitted": sum(r["admission_minutes"] is not None for r in rows),
            "filled": len(filled), "carry_in": sum(r["already_in_position"] for r in rows),
            "opposite_side_fills": sum(r["opposite_side_fills"] for r in rows),
            "median_latency_minutes": {key.removesuffix("_minutes"): med(key, rows) for key in
                                       ("watch_minutes", "confirmation_minutes", "geometry_valid_minutes",
                                        "admission_minutes", "fill_minutes")},
            "median_capture_fraction": med("capture_fraction", filled),
            "median_favorable_price_excursion": med("favorable", filled),
            "median_adverse_price_excursion": med("adverse", filled),
            "misses": dict(Counter(r["miss"] for r in rows if r["miss"])),
        }
    return report


def net(trade, extra_per_unit, extra_commission):
    cash = sum((p.net for p in trade.exits), ZERO)
    fees = sum((p.commission for p in trade.exits), ZERO)
    return cash - extra_per_unit * trade.units - extra_commission * fees


def economics(trades, events, cfg, pointvalue):
    closed = [t for t in trades if t.exits]
    spread = Decimal(str(cfg["stress_incremental_round_trip_spread_price"]))
    slip = Decimal(str(cfg["stress_incremental_adverse_slip_price_per_fill"]))
    extra = (spread + 2 * slip) * pointvalue
    fee_multiplier = Decimal(str(cfg["stress_incremental_commission_fraction"]))
    sides_by_hour = {e["start"]: e["side"] for e in events}
    inside, outside = [], []
    for trade in closed:
        event_side = sides_by_hour.get((trade.entry_time // HOUR) * HOUR)
        (inside if event_side == trade.side else outside).append(trade)
    payouts = [net(t, ZERO, ZERO) for t in closed]
    r_values = [float(cash / t.initial_risk) for t, cash in zip(closed, payouts) if t.initial_risk is not None]
    reason = Counter(p.reason for t in closed for p in t.exits)
    return {
        "closed_campaigns": stats(payouts),
        "stressed_campaigns": stats(net(t, extra, fee_multiplier) for t in closed),
        "campaign_close_drawdown_usd": closed_drawdown((t.exits[-1].time, i, cash)
                                                      for i, (t, cash) in enumerate(zip(closed, payouts)))["max_drawdown_cash"],
        "stressed_campaign_close_drawdown_usd": closed_drawdown((t.exits[-1].time, i, net(t, extra, fee_multiplier))
                                                                for i, t in enumerate(closed))["max_drawdown_cash"],
        "net_expectancy_r": mean(r_values) if r_values else None,
        "net_expectancy_r_block_ci": block_bootstrap(r_values),
        "undefined_actual_risk_count": len(closed) - len(r_values),
        "filled_open_or_entry_pending_at_cutoff": len(trades) - len(closed),
        "fee_usd": sum((p.commission for t in closed for p in t.exits), ZERO),
        "exit_part_reasons": dict(sorted(reason.items())),
        "same_side_event_fills": len(inside),
        "outside_same_side_event": stats(net(t, ZERO, ZERO) for t in outside),
        "by_side": {side_name: stats(net(t, ZERO, ZERO) for t in closed if t.side == side)
                    for side_name, side in SIDES.items()},
    }


def evaluate_frame(frame, data, bars_root, logs_dir, reg):
    bars_path = bars_root / Path(data["local_bars"]).parts[-2] / "bars.csv"
    logs_path = logs_dir / data["log_name"]
    read_pinned(bars_path, data["bars_sha256"])
    read_pinned(logs_path, data["log_sha256"])
    bars = load_bars(bars_path)
    observed = inspect(logs_path)
    run = observed["run"]
    if (observed["mode"] != "S5" or run["timeframe"] != frame or observed["unfilled"]
            or any(c["state"] != "COMPLETE" for c in observed["campaigns"])):
        raise ValueError("S5 execution tape has incomplete or unfilled exposure")
    verify_bars(bars, run)
    tick, pointvalue = float(run["mintick"]), run["pointvalue"]
    model, execution = reg["impulse_model"], reg["execution"]
    cut_index = int(.60 * len(bars))
    cut = bars[cut_index]["time"]
    decisions = scan(bars, model, tick)
    for index in (cut_index, len(bars) - 30):
        if scan(bars[:index], model, tick) != [d for d in decisions if d.bar < index]:
            raise ValueError("Decision prefix differs on the same observed prefix")
    s5 = list(observed_offers(observed["campaigns"]))
    impulses = [model_offer(d, bars, execution, tick, pointvalue, model["maximum_hold_bars"])
                for d in decisions if d.state == "RISK_VALID"]
    standalone, standalone_blocked = occupy(impulses, "IMPULSE")
    reproduced_s5, blocked_s5 = occupy(s5, "S5")
    if blocked_s5 or {t.id for t in reproduced_s5} != {t.id for t in s5}:
        raise ValueError("Portfolio rule does not reproduce single-lane observed S5 occupancy")
    combined, combined_blocked = occupy(s5 + impulses, "S5")
    reverse, reverse_blocked = occupy(s5 + impulses, "IMPULSE")
    events, coverage = hourly_events(bars, int(frame), reg["event"])
    mark_events(events, bars, decisions, standalone, s5, combined)
    lanes = {"S5": s5, "IMPULSE": standalone, "COMBINED": combined, "REVERSE_TIE": reverse}
    def summary(trades, candidate_events):
        return economics(trades, candidate_events, execution, pointvalue)
    metrics = {name: summary(orders, events) for name, orders in lanes.items()}
    partitions = {
        "earlier": ([e for e in events if e["end"] <= cut],
                    lambda t: t.exits and t.exits[-1].time < cut),
        "later": ([e for e in events if e["start"] >= cut],
                  lambda t: t.entry_time is not None and t.entry_time >= cut),
    }
    gate_rows = {
        "S5": metrics["S5"]["closed_campaigns"]["net_cash"],
        "delta_combined_net_usd": metrics["COMBINED"]["closed_campaigns"]["net_cash"] -
        metrics["S5"]["closed_campaigns"]["net_cash"],
        "delta_stressed_combined_net_usd": metrics["COMBINED"]["stressed_campaigns"]["net_cash"] -
        metrics["S5"]["stressed_campaigns"]["net_cash"],
        "delta_combined_campaign_close_drawdown_usd": metrics["COMBINED"]["campaign_close_drawdown_usd"] -
        metrics["S5"]["campaign_close_drawdown_usd"],
    }
    out = {
        "provenance": {"market_sha256": data["bars_sha256"], "s5_log_sha256": data["log_sha256"],
                       "native_source_sha256": run["source_sha256"], "bars": len(bars),
                       "from_utc": utc(bars[0]["time"]).isoformat(), "through_utc": utc(bars[-1]["time_close"]).isoformat(),
                       "native_market_fingerprints": [run["checksum1"], run["checksum2"]]},
        "calendar": coverage,
        "trade_metrics": metrics,
        "portfolio_delta": gate_rows,
        "events_by_side": {k: describe_events([e for e in events if e["side"] == side]) for k, side in SIDES.items()},
        "decision_counts": {k: dict(Counter(d.state for d in decisions if d.side == side)) for k, side in SIDES.items()},
        "reasons": {state: dict(Counter(d.reason for d in decisions if d.state == state))
                    for state in ("CONFIRMED", "REJECTED", "EXPIRED")},
        "occupancy": {name: dict(Counter(f"{item['engine']}_blocked_by_{item['occupied_by']}" for item in blocked))
                      for name, blocked in (("IMPULSE", standalone_blocked), ("COMBINED", combined_blocked),
                                            ("REVERSE_TIE", reverse_blocked))},
        "chronology": {
            "split_utc": utc(cut).isoformat(),
            "events_straddling_split": sum(e["start"] < cut < e["end"] for e in events),
            "crossing_or_censored_trade_campaigns": {name: sum(not any(keep(t) and t.exits for _, keep in partitions.values())
                                                             for t in orders) for name, orders in lanes.items()},
            **{label: {"events": describe_events(bucket),
                       "economics": {name: summary([t for t in orders if keep(t) and t.exits], bucket)
                                     for name, orders in lanes.items()}}
               for label, (bucket, keep) in partitions.items()},
        },
        "prefix_replays": [cut_index, len(bars) - 30],
    }
    tapes = {
        "events": events,
        "impulse_decisions": [asdict(d) for d in decisions],
        "occupancy_rejections": {"IMPULSE": standalone_blocked, "COMBINED": combined_blocked, "REVERSE_TIE": reverse_blocked},
        "orders": {name: [asdict(t) for t in orders] for name, orders in lanes.items()},
    }
    return out, tapes


def write_json(path, payload):
    with path.open("x", encoding="utf-8", newline="\n") as out:
        json.dump(payload, out, default=json_default, ensure_ascii=False, indent=2, allow_nan=False)
        out.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--logs-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--tapes", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.tapes.exists():
        parser.error("Output files already exist")
    registration = json.loads(REGISTRATION.read_bytes())
    original = json.loads((ROOT / registration["data_registration"]).read_bytes())
    summary, tapes = {}, {}
    for frame in registration["frames_minutes"]:
        key = str(frame)
        summary[key], tapes[key] = evaluate_frame(key, original["frames"][key], args.data_root,
                                                  args.logs_dir, registration)
    with args.tapes.open("xb") as stream:
        payload = json.dumps(tapes, default=json_default, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode("utf-8")
        stream.write(gzip.compress(payload, compresslevel=9, mtime=0))
    write_json(args.output, {"schema": "qirvanta.v21.event-evaluation.v1",
                             "status": "EXPERIMENTAL_ARCHIVAL_OHLC_AND_NATIVE_S5_TAPE",
                             "registration_sha256": sha(REGISTRATION.read_bytes()),
                             "tapes_sha256": sha(args.tapes.read_bytes()),
                             "frames": summary})
    print(f"Evaluated {len(summary)} frames; impulse execution and combined portfolio use a chart-bar model.")


if __name__ == "__main__":
    main()
