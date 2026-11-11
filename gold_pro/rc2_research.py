"""Audit archived S5 signals and isolated RC2 exit policies on observed OHLC."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from math import isclose, isfinite
from pathlib import Path
from statistics import mean, median
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from research.features.causal import Smooth
from research.validation.statistics import cash_metrics
from unified.tools.evaluate_v21_impulse import hourly_events

from .rc1_qa import BASELINE, BASELINE_SHA, FRAMES, ROOT, compare_exports, digest, read_export
from .replay import Costs, round_away


ARCHIVE = Path("releases/gold-pro-rc1/evidence/native-logs.zip")
SEAL = Path("docs/gold_pro_evidence/forward_seal_m5.json")
RC1_AUDIT = Path("releases/gold-pro-rc1/QIRVANTA_GOLD_PRO_RC1_AUDIT.pine")
EVENT_RULES = Path("docs/v21_evidence/event_evaluation_registration.json")
PINNED = {
    BASELINE: BASELINE_SHA,
    Path("releases/gold-pro-rc1/QIRVANTA_GOLD_PRO_RC1.pine"): "e51308df4be72827dfdf4dc875a3dbea5b679a6bb859fe179909ba24c85e975f",
    RC1_AUDIT: "dfb154e407d32cbfc90ac957788b54cfa5b4e2034421b4b3ac26abf039d59278",
    ARCHIVE: "b9119809625326e5e9111f8b9adb77d30908981f2d901b8ddac16833020cee97",
    SEAL: "09181ecaf9e30cde62c5203b524cb458d808a07c24029e961e32bb25c474afd1",
    EVENT_RULES: "dc023d3c15e75327924bb3852cdcb620a079eee99a16edee7472fcdf9e8f04a7",
}
POLICIES = {"FIXED": None, "BE": 0.0, "LOCK_025R": 0.25, "S5_LOCK_005R_SENSITIVITY": 0.05}
COST = Costs()
UNITS = 3
HOUR = 3_600_000
QUALITY_FILTER = 1.75
NATIVE_MODES = {"RC2 Fixed SL", "S5 RAW Parity", "Experimental BE", "Experimental +0.25R"}
PART_FIELDS = {"plan_id", "source_id", "mode", "exit_order_id", "entry_ms", "exit_ms", "entry_price",
               "exit_price", "signed_qty", "commission", "max_runup_cash", "max_drawdown_cash"}
SUMMARY_FIELDS = {"mode", "symbol", "timeframe", "last_close_ms", "first_open_ms", "currency", "initial_capital",
                  "netprofit", "openprofit", "grossprofit", "grossloss", "max_drawdown", "position_size",
                  "closed_parts", "win_parts", "loss_parts", "campaign_count"}


@dataclass(frozen=True)
class Offer:
    id: str
    side: int
    at: int
    index: int
    reference: float
    stop: float
    targets: tuple[float, float, float]
    atr: float
    max_risk_atr: float
    modeled_plan_cost: float
    obstacle: float | None
    required_room_r: float
    max_hold: int
    quality: str


@dataclass(frozen=True)
class Part:
    at: int
    price: float
    units: float
    commission: float
    net_before_spread: float
    reason: str


@dataclass(frozen=True)
class Campaign:
    id: str
    side: int
    decision_at: int
    filled_at: int
    entry: float
    initial_sl: float
    exits: tuple[Part, ...]
    closed: bool

    @property
    def net(self):
        return sum(x.net_before_spread - x.units * COST.spread * COST.pointvalue for x in self.exits)

    @property
    def risk(self):
        return self.side * (self.entry - self.initial_sl) * UNITS * COST.pointvalue


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def chart_bars(export):
    return [
        {"time": x.opening, "time_close": x.closing, **dict(zip(("open", "high", "low", "close"), map(float, x.ohlc)))}
        for x in export.context
    ]


def offers_from_ready(export, frame):
    close_to_index = {bar.closing: i for i, bar in enumerate(export.context)}
    confirmations = {x["event_id"]: x for x in export.core if x["event"] == "CONFIRMED"}
    offers = []
    for ready in export.ready:
        signal = confirmations[ready["source_id"]]
        offers.append(Offer(
            signal["plan_id"], signal["side"], signal["known_time"], close_to_index[signal["known_time"]],
            float(signal["reference"]), float(signal["initial_sl"]),
            tuple(float(signal[f"tp{i}"]) for i in (1, 2, 3)),
            float(signal["atr_at_signal"]), float(signal["max_risk_atr"]),
            float(signal["roundtrip_cost_price"]), float(signal["obstacle"]) if signal["obstacle"] is not None else None,
            float(signal["required_room_r"]), 48 if frame <= 15 else 60, signal["quality"],
        ))
    return offers


def native_campaigns(export):
    """One campaign per native ID, retaining blocked, open and incomplete counts."""
    grouped = defaultdict(list)
    for event in export.native:
        grouped[event["plan_id"]].append(event)
    campaigns, counts, same_bar = [], Counter(), 0
    for plan_id, tape in grouped.items():
        kinds = Counter(row["event"] for row in tape)
        for kind in ("NATIVE_ENTRY", "NATIVE_SUBMITTED", "NATIVE_CLOSED"):
            if kinds[kind] > 1:
                raise ValueError(f"Duplicate {kind} for {plan_id}")
        entry = next((row for row in tape if row["event"] == "NATIVE_ENTRY"), None)
        close = next((row for row in tape if row["event"] == "NATIVE_CLOSED"), None)
        exits = [row for row in tape if row["event"].removeprefix("NATIVE_EXIT_").isdigit()]
        if entry is None:
            if close or exits:
                raise ValueError(f"Exit without a recorded native entry for {plan_id}")
            counts["blocked"] += kinds["NATIVE_BLOCKED"]
            counts["not_filled"] += kinds["NATIVE_NOT_FILLED"]
            counts["pending_at_cutoff"] += int(kinds["NATIVE_SUBMITTED"] > kinds["NATIVE_NOT_FILLED"])
            continue
        submission = next((row for row in tape if row["event"] == "NATIVE_SUBMITTED"), None)
        if submission is None or entry["effective_time"] < submission["known_time"]:
            raise ValueError(f"Missing or noncausal native submission for {plan_id}")
        native_side = entry["side"]
        price = float(entry["price"])
        native_exit = []
        stop_time = None
        for row in sorted(exits, key=lambda event: event["effective_time"]):
            at, px, reason = row["effective_time"], float(row["price"]), row["reason"]
            fraction_units = UNITS * float(row["quantity_fraction"])
            units = round(fraction_units)
            net = float(row["cash_pnl"])
            commission = (px - price) * native_side * units * COST.pointvalue - net
            expected_fee = 2 * COST.commission * units
            if (at < entry["effective_time"] or not 1 <= units <= UNITS
                    or not isclose(fraction_units, units, abs_tol=1e-6)
                    or not isclose(commission, expected_fee, abs_tol=0.001)):
                raise ValueError(f"Exit chronology, quantity or commissioned cash does not reconcile for {plan_id}")
            native_exit.append(Part(at, px, units, expected_fee, net, reason))
            if reason == "SL" and stop_time is None:
                stop_time = at
        if stop_time is not None:
            if any(part.reason.startswith("TP") and part.at > stop_time for part in native_exit):
                raise ValueError(f"Native target occurred on a later bar than stop for {plan_id}")
            same_bar += int(any(part.reason.startswith("TP") and part.at == stop_time for part in native_exit))
        if close and (not native_exit or not isclose(sum(part.units for part in native_exit), UNITS, abs_tol=1e-6)
                      or not isclose(sum(part.net_before_spread for part in native_exit), float(close["cash_pnl"]), abs_tol=0.001)):
            raise ValueError(f"Closed native campaign does not reconcile for {plan_id}")
        counts["closed" if close else "open_at_cutoff"] += 1
        counts["invalid_open_gap_observed"] += kinds["NATIVE_DIVERGENCE"]
        counts["stop_update_observed"] += kinds["NATIVE_STOP_SCHEDULED"]
        campaigns.append(Campaign(plan_id, native_side, submission["known_time"], entry["effective_time"],
                                  price, float(entry["initial_sl"]), tuple(native_exit), close is not None))
    counts["campaigns_with_same_timestamp_stop_and_target_order_unverified"] = same_bar
    return campaigns, dict(counts)


def native_properties(path, export, timeframe):
    """Reconcile optional actual TradingView API rows with the original event tape."""
    properties = defaultdict(list)
    tags = {"RC2_NATIVE_PART", "RC2_NATIVE_ANOMALY", "RC2_AUDIT_SUMMARY"}
    with path.open(encoding="utf-8-sig", newline="") as csv_file:
        for row in csv.DictReader(csv_file, strict=True):
            for line in row["Message"].splitlines():
                tag, sep, payload = line.partition("|")
                if tag in tags and sep:
                    record = json.loads(payload)
                    if not isinstance(record, dict):
                        raise ValueError(f"{tag}: expected JSON object")
                    properties[tag].append(record)
    if not properties:
        return {"status": "NO_RC2_STRATEGY_PROPERTY_EXPORT"}
    summaries = properties["RC2_AUDIT_SUMMARY"]
    parts = properties["RC2_NATIVE_PART"]
    anomalies = properties["RC2_NATIVE_ANOMALY"]
    if len(summaries) != 1 or set(summaries[0]) != SUMMARY_FIELDS:
        raise ValueError("Expected one complete RC2_AUDIT_SUMMARY")
    summary = summaries[0]
    mode = summary["mode"]
    if (mode not in NATIVE_MODES or summary["symbol"] != "OANDA:XAUUSD" or summary["timeframe"] != str(timeframe)
            or summary["first_open_ms"] != export.context[0].first_open or summary["last_close_ms"] != export.context[-1].closing
            or summary["currency"] != "USD" or summary["initial_capital"] != 100000):
        raise ValueError("RC2 strategy summary context or account properties do not match")
    for key in ("netprofit", "openprofit", "grossprofit", "grossloss", "max_drawdown", "position_size"):
        if type(summary[key]) not in (float, int) or not isfinite(summary[key]):
            raise ValueError(f"RC2 strategy summary {key} is not a finite number")
    for key in ("closed_parts", "win_parts", "loss_parts", "campaign_count"):
        if type(summary[key]) is not int or summary[key] < 0:
            raise ValueError(f"RC2 strategy summary {key} is not a nonnegative count")
    if summary["win_parts"] + summary["loss_parts"] > summary["closed_parts"]:
        raise ValueError("RC2 strategy summary outcome counts are inconsistent")
    native_exits = {row["event_id"]: row for row in export.native if row["event"].removeprefix("NATIVE_EXIT_").isdigit()}
    entries = {row["plan_id"]: row for row in export.native if row["event"] == "NATIVE_ENTRY"}
    seen = set()
    for part in parts:
        if set(part) != PART_FIELDS or part["mode"] != mode or part["source_id"] in seen:
            raise ValueError("RC2 native partial event has incompatible schema, mode or duplicate ID")
        for key in ("entry_price", "exit_price", "signed_qty", "commission", "max_runup_cash", "max_drawdown_cash"):
            if type(part[key]) not in (float, int) or not isfinite(part[key]):
                raise ValueError(f"RC2 native partial {key} is not a finite number")
        source = native_exits.get(part["source_id"])
        entry = entries.get(part["plan_id"])
        if (source is None or entry is None or source["plan_id"] != part["plan_id"]
                or part["entry_ms"] != entry["effective_time"] or part["exit_ms"] != source["effective_time"]
                or part["entry_price"] != float(entry["price"]) or part["exit_price"] != float(source["price"])
                or not isclose(part["signed_qty"], source["side"] * UNITS * float(source["quantity_fraction"]), abs_tol=1e-6)
                or not isclose(part["commission"], 2 * COST.commission * abs(part["signed_qty"]), abs_tol=1e-6)
                or part["max_runup_cash"] < 0 or part["max_drawdown_cash"] < 0):
            raise ValueError(f"RC2 native partial does not reconcile: {part['source_id']}")
        seen.add(part["source_id"])
    if seen != native_exits.keys():
        raise ValueError("RC2 native API rows do not cover all journaled exits")
    if export.meta["starts_at_first_loaded_bar"]:
        closed = sum(row["event"] == "NATIVE_CLOSED" for row in export.native)
        if len(parts) != summary["closed_parts"] or closed != summary["campaign_count"]:
            raise ValueError("RC2 native API summary disagrees with complete journal counts")
    return {
        "status": "NATIVE_LIFECYCLE_ANOMALY" if anomalies else "RECONCILED",
        "native_strategy_properties": summary,
        "actual_partial_commission_usd": sum(part["commission"] for part in parts),
        "mean_emulator_partial_runup_usd": mean(part["max_runup_cash"] for part in parts) if parts else None,
        "mean_emulator_partial_drawdown_usd": mean(part["max_drawdown_cash"] for part in parts) if parts else None,
        "native_lifecycle_anomalies": anomalies,
    }


def invalid_open(offer, fill):
    side = offer.side
    risk = side * (fill - offer.stop)
    wrong_geometry = risk < COST.tick or side * (offer.targets[0] - fill) <= 0
    outside_risk = not 0.8 * offer.atr <= risk <= offer.max_risk_atr * offer.atr
    bad_room = offer.required_room_r > 0 and (
        offer.obstacle is None or (offer.obstacle - fill) * side + COST.tick * 1e-6 <
        max(risk * offer.required_room_r, (offer.targets[0] - fill) * side)
    )
    return (wrong_geometry or abs(fill - offer.reference) > 0.2 * offer.atr or outside_risk
            or offer.modeled_plan_cost > 0.18 * risk or bad_room)


def simulate(offer, bars, lock_r):
    """Stop first after known open prices, with stop updates usable next bar."""
    entry_index = offer.index + 1
    if entry_index >= len(bars):
        return None, len(bars)
    side, stop = offer.side, offer.stop
    fill = bars[entry_index]["open"] + side * COST.slip
    initial_risk = side * (fill - stop)
    invalid = invalid_open(offer, fill)
    active = list(enumerate(offer.targets, 1))
    parts = []
    queued_exit = None
    first_target = False
    for index in range(entry_index, len(bars)):
        bar = bars[index]
        opening = bar["open"]
        adverse, favorable = (bar["low"], bar["high"]) if side > 0 else (bar["high"], bar["low"])

        def settle(legs, price, reason):
            for leg in legs:
                units = UNITS / 3
                commission = 2 * COST.commission * units
                net = side * (price - fill) * units * COST.pointvalue - commission
                parts.append(Part(bar["time"], price, units, commission, net, reason))
                active.remove(leg)

        if queued_exit or side * (opening - stop) <= 0:
            settle(active[:], opening - side * COST.slip, queued_exit or "SL_GAP")
        else:
            can_target = index > entry_index or not invalid
            if can_target:
                for leg in active[:]:
                    if side * (opening - leg[1]) >= 0:
                        settle([leg], leg[1], f"TP{leg[0]}_AT_OPEN")
                        first_target |= leg[0] == 1
            if side * (adverse - stop) <= 0:
                settle(active[:], stop - side * COST.slip, "SL")
            elif can_target:
                for leg in active[:]:
                    if side * (favorable - leg[1]) >= 0:
                        settle([leg], leg[1], f"TP{leg[0]}")
                        first_target |= leg[0] == 1
        if not active:
            return Campaign(offer.id, side, offer.at, bars[entry_index]["time"], fill, offer.stop, tuple(parts), True), index
        if index == entry_index and invalid:
            queued_exit = "INVALID_OPEN_EXIT"
        elif index - entry_index >= offer.max_hold:
            queued_exit = "TIME_EXIT"
        elif first_target and lock_r is not None and initial_risk >= COST.tick:
            proposed = round_away(fill + side * initial_risk * lock_r, COST.tick, -side)
            if side * (proposed - stop) >= COST.tick / 2 and side * (bar["close"] - proposed) >= COST.tick / 2:
                stop = proposed
    return Campaign(offer.id, side, offer.at, bars[entry_index]["time"], fill, offer.stop, tuple(parts), False), len(bars)


def replay(offers, bars, lock_r, *, range_filter=False):
    campaigns, states = [], Counter()
    occupied_through = -1
    accepted_at = -1
    for offer in offers:
        candle = bars[offer.index]
        if range_filter and max(candle["high"] - candle["low"], COST.tick) > QUALITY_FILTER * offer.atr:
            states["rejected_signal_range"] += 1
            continue
        if offer.index < occupied_through or offer.index == accepted_at:
            states["blocked_occupied"] += 1
            continue
        campaign, occupied_through = simulate(offer, bars, lock_r)
        accepted_at = offer.index
        if campaign is None:
            states["pending_at_cutoff"] += 1
            continue
        states["closed" if campaign.closed else "open_at_cutoff"] += 1
        campaigns.append(campaign)
    return campaigns, dict(states)


def excursion(campaign, bars, end_at=None):
    """Use interior bars and observed part prices, excluding unordered boundary extrema."""
    parts = [part for part in campaign.exits if end_at is None or part.at < end_at]
    final_at = campaign.exits[-1].at if campaign.closed else bars[-1]["time_close"]
    if end_at is not None:
        final_at = min(final_at, end_at)
    prices = [campaign.entry, *(part.price for part in parts)]
    for bar in bars:
        if campaign.filled_at < bar["time"] < final_at and bar["time_close"] <= final_at:
            prices.extend((bar["high"], bar["low"]))
    moves = [campaign.side * (price - campaign.entry) for price in prices]
    return -min(moves), max(moves)


def session(at):
    hour = datetime.fromtimestamp(at / 1000, timezone.utc).hour
    return next(name for upper, name in ((7, "ASIA"), (13, "LONDON"), (16, "OVERLAP"), (21, "NEW YORK"), (24, "OFF HOURS"))
                if hour < upper)


def metrics(campaigns, bars):
    complete = sorted((c for c in campaigns if c.closed), key=lambda c: (c.exits[-1].at, c.id))
    results = cash_metrics([c.net for c in complete])
    r = [c.net / c.risk for c in complete if c.risk > 0]
    movement = [excursion(c, bars) for c in complete]
    closed_parts = [part for c in complete for part in c.exits]
    observed = [part for c in campaigns for part in c.exits]
    realized_at = defaultdict(float)
    for part in observed:
        realized_at[part.at] += part.net_before_spread - part.units * COST.spread * COST.pointvalue
    realized = cash_metrics([realized_at[at] for at in sorted(realized_at)])
    results.update({
        "mean_net_r": mean(r) if r else None,
        "risk_scored_campaigns": len(r),
        "mean_mae_price": mean(x[0] for x in movement) if movement else None,
        "mean_mfe_price": mean(x[1] for x in movement) if movement else None,
        "closed_partial_exits": len(closed_parts),
        "closed_commission_usd": sum(part.commission for part in closed_parts),
        "closed_spread_sensitivity_usd": sum(part.units * COST.spread * COST.pointvalue for part in closed_parts),
        "closed_net_before_spread_usd": sum(part.net_before_spread for part in closed_parts),
        "open_campaigns_censored": len(campaigns) - len(complete),
        "realized_partial_exit_drawdown_usd": realized["campaign_close_drawdown"],
        "native_mark_to_market_max_drawdown_usd": None,
    })
    return results


def performance(campaigns, bars, counts):
    split = bars[int(len(bars) * 0.6)]["time"]
    first, second = [], []
    crossing = 0
    by_session = defaultdict(list)
    for c in campaigns:
        by_session[session(c.filled_at)].append(c)
        if c.decision_at >= split:
            second.append(c)
        elif c.closed and c.exits[-1].at < split:
            first.append(c)
        else:
            crossing += 1
    return {
        "all": metrics(campaigns, bars),
        "lifecycle": counts,
        "by_entry_session": {key: metrics(rows, bars) for key, rows in sorted(by_session.items())},
        "chronological_archive": {
            "validation_start_utc": utc(split), "rule": "first 60% of bars then 40%; cross-boundary campaigns retained only in full sample",
            "in_sample": metrics(first, bars), "validation": metrics(second, bars),
            "crossing_or_open_in_sample": crossing, "untouched_holdout": "NOT_AVAILABLE",
        },
    }


def decision_diagnostics(export, bars, frame):
    by_close = {row["time_close"]: i for i, row in enumerate(bars)}
    raw = []
    groups = defaultdict(list)
    for row in export.core:
        if row["event"] not in {"CANCELLED", "REJECTED"}:
            continue
        state = "INVALIDATED" if row["event"] == "CANCELLED" else "RISK_REJECTED"
        at, side = row["known_time"], row["side"]
        i = by_close[at]
        future = bars[i + 1:i + 1 + 60 // frame]
        full = (len(future) == 60 // frame and all(x["time"] == at + j * frame * 60_000
                and x["time_close"] == at + (j + 1) * frame * 60_000 for j, x in enumerate(future)))
        values = None
        if full:
            observed = bars[i]["close"]
            extremes = [side * (x[key] - observed) for x in future for key in ("high", "low")]
            values = {"favorable_price": max(0, *extremes), "adverse_price": -min(0, *extremes),
                      "directional_close_change": side * (future[-1]["close"] - observed)}
        record = {"state": state, "reason": row["reason"], "event_id": row["event_id"], "side": side,
                  "known_utc": utc(at), "quality": row["quality"], "subsequent_60m": values}
        groups[(state, row["reason"])].append(record)
        raw.append(record)
    aggregate = []
    for (state, reason), records in sorted(groups.items()):
        observed = [r["subsequent_60m"] for r in records if r["subsequent_60m"] is not None]
        aggregate.append({"state": state, "reason": reason, "count": len(records), "with_complete_60m": len(observed),
                          "median_favorable_price": median(x["favorable_price"] for x in observed) if observed else None,
                          "median_adverse_price": median(x["adverse_price"] for x in observed) if observed else None})
    return {"summary": aggregate, "observations": raw, "core_states": dict(Counter(x["event"] for x in export.core)),
            "confirmed_quality": dict(Counter(x["quality"] for x in export.core if x["event"] == "CONFIRMED"))}


def early_observations(bars):
    """Independent reconstruction from archived closed bars, not native delivery."""
    observations = []
    smooth = Smooth(14, wilder=True)
    previous_side = 0
    for i, row in enumerate(bars):
        prior_atr = smooth.value
        prior = bars[i - 1] if i else row
        high, low = row["high"], row["low"]
        smooth.add(max(high - low, abs(high - prior["close"]), abs(low - prior["close"])))
        current_side = 0
        if i >= 12 and prior_atr is not None and max(high - low, COST.tick) >= 1.15 * prior_atr:
            window = bars[i - 12:i]
            body = row["close"] - row["open"]
            if row["close"] > max(b["high"] for b in window) and body >= 0.50 * prior_atr:
                current_side = 1
            elif row["close"] < min(b["low"] for b in window) and body <= -0.50 * prior_atr:
                current_side = -1
        if current_side and current_side != previous_side and row["time"] == prior["time_close"]:
            observations.append({"known_ms": row["time_close"], "side": current_side, "price": row["close"]})
        previous_side = current_side
    return observations


def missed_moves(export, bars, frame, offers, campaigns, event_rules):
    events, windows = hourly_events(bars, frame, event_rules)
    early = early_observations(bars)
    by_hour, offers_by_hour, fills_by_hour, early_by_hour = (defaultdict(list) for _ in range(4))
    for row in export.core:
        by_hour[(row["known_time"] // HOUR * HOUR, row["side"])].append(row)
    for offer in offers:
        offers_by_hour[(offer.at // HOUR * HOUR, offer.side)].append(offer)
    for c in campaigns:
        fills_by_hour[(c.filled_at // HOUR * HOUR, c.side)].append(c)
    for observed in early:
        early_by_hour[(observed["known_ms"] // HOUR * HOUR, observed["side"])].append(observed)
    detail = []
    for event in events:
        at, side = event["start"], event["side"]
        group = at, side
        candidates = offers_by_hour[group]
        matches = fills_by_hour[group]
        decisions = by_hour[group]
        early_watch = early_by_hour[group]
        first = min(matches, key=lambda c: c.filled_at) if matches else None
        capture = excursion(first, bars, event["end"])[1] / event["directional_extreme_distance"] if first else None
        why = None
        if not first:
            if candidates:
                why = "READY_WITHOUT_SAME_HOUR_FILL"
            elif any(row["event"] == "REJECTED" for row in decisions):
                why = "RISK_REJECTED"
            elif decisions:
                why = "OBSERVATION_WITHOUT_READY"
            else:
                why = "NO_NEW_SAME_DIRECTION_SIGNAL"
        detail.append({
            "start_utc": utc(at), "side": side, "directional_extreme_price_distance": event["directional_extreme_distance"],
            "decision_counts": dict(Counter(row["event"] for row in decisions)),
            "modeled_early_watch_count": len(early_watch),
            "ready_count": len(candidates), "native_fill_count": len(matches), "no_fill_reason": why,
            "capture_fraction_with_boundary_extrema_excluded": capture,
            "first_watch_delay_minutes": min(((r["known_time"] - at) / 60_000 for r in decisions if r["event"] == "WATCH"), default=None),
            "first_modeled_early_watch_delay_minutes": min(((r["known_ms"] - at) / 60_000 for r in early_watch), default=None),
            "first_ready_delay_minutes": min(((o.at - at) / 60_000 for o in candidates), default=None),
            "first_fill_delay_minutes": (first.filled_at - at) / 60_000 if first else None,
        })
    return {
        "windows": windows, "directional_hours": len(events),
        "missed_same_direction_fill": sum(row["no_fill_reason"] is not None for row in detail),
        "experimental_early_watch": {
            "mode": "OBSERVATIONAL_ARCHIVE_MODEL",
            "total": len(early),
            "by_direction": dict(Counter("BUY" if row["side"] > 0 else "SELL" for row in early)),
            "observations_in_directional_hours": sum(row["modeled_early_watch_count"] for row in detail),
            "missed_fill_hours_with_early_observation": sum(
                row["no_fill_reason"] is not None and row["modeled_early_watch_count"] > 0 for row in detail
            ),
            "observations": early,
        },
        "no_fill_reasons": dict(Counter(row["no_fill_reason"] for row in detail if row["no_fill_reason"])),
        "examples": detail,
    }


def load_reference(frame, folder):
    reference = read_export(folder / f"reference_M{frame}.csv", frame, "reference")
    rc1 = read_export(folder / f"audit_M{frame}.csv", frame, "audit")
    match = compare_exports(reference, rc1, ("core", "ready", "native"))
    if match["status"] != "MATCH":
        raise ValueError(f"Archived reference and RC1 M{frame} do not match: {match}")
    return reference, rc1, match


def shared_bar_comparison(left, right, streams):
    """Report the common live export bars separately from full warmup parity."""
    first = max(left.context[0].opening, right.context[0].opening)
    last = min(left.context[-1].closing, right.context[-1].closing)
    left_bars, right_bars = (
        [(bar.opening, bar.closing, bar.ohlc, bar.htf_closes) for bar in export.context
         if first <= bar.opening and bar.closing <= last] for export in (left, right)
    )
    if not left_bars or left_bars != right_bars:
        return {"status": "NO_EXACT_COMMON_CONTEXT"}
    results = {}
    closes = {bar[1] for bar in left_bars}
    for stream in streams:
        key = "confirmed_ms" if stream == "ready" else "known_time"
        lrows, rrows = ([r for r in getattr(x, stream) if r[key] in closes] for x in (left, right))
        results[stream] = {"equal": lrows == rrows, "archive_rows": len(lrows), "other_rows": len(rrows)}
    return {"status": "MATCH" if all(x["equal"] for x in results.values()) else "MISMATCH",
            "common_bars": len(left_bars), "first_common_open_utc": utc(first), "last_common_close_utc": utc(last),
            "streams": results}


def native_experiment(path, timeframe, cutoff):
    if not path or not path.is_file():
        return {"status": "NO_NATIVE_EXPERIMENTAL_EXPORT"}
    export = read_export(path, timeframe, "indicator")
    if export.context[-1].closing >= cutoff:
        raise ValueError("RC2 research diagnostic must end before the sealed holdout")
    bars = chart_bars(export)
    observed, filters = [], []
    with path.open(encoding="utf-8-sig", newline="") as csv_file:
        for row in csv.DictReader(csv_file, strict=True):
            for line in row["Message"].splitlines():
                fields = line.split("|")
                if fields[0] == "RC2_EARLY":
                    if len(fields) != 7 or fields[1:4] != ["1", "OANDA:XAUUSD", str(timeframe)] or fields[5] not in {"BUY", "SELL"}:
                        raise ValueError("Malformed RC2 EARLY WATCH diagnostic")
                    observed.append({"known_ms": int(fields[4]), "side": 1 if fields[5] == "BUY" else -1, "price": float(fields[6])})
                elif fields[0] == "RC2_FILTER":
                    if len(fields) < 6 or fields[1] != "1" or fields[-2] not in {"PASS", "REJECTED"}:
                        raise ValueError("Malformed RC2 range filter diagnostic")
                    filters.append({"plan_id": "|".join(fields[2:-3]), "known_ms": int(fields[-3]),
                                    "state": fields[-2], "range_atr": float(fields[-1])})
    closes = {bar["time_close"]: bar["close"] for bar in bars}
    if (len({row["known_ms"] for row in observed}) != len(observed)
            or any(closes.get(row["known_ms"]) != row["price"] for row in observed)):
        raise ValueError("RC2 EARLY WATCH did not reconcile to an observed candle close")
    offers = offers_from_ready(export, timeframe)
    expected = {o.id: (o, max(bars[o.index]["high"] - bars[o.index]["low"], COST.tick) / o.atr) for o in offers}
    if filters and (len(filters) != len(expected) or len({row["plan_id"] for row in filters}) != len(filters)):
        raise ValueError("RC2 range filter diagnostics do not cover the canonical READY plans")
    for row in filters:
        result = expected.get(row["plan_id"])
        if (result is None or result[0].at != row["known_ms"] or not isclose(result[1], row["range_atr"], abs_tol=1e-6)
                or (result[1] > QUALITY_FILTER) != (row["state"] == "REJECTED")):
            raise ValueError("RC2 range filter diagnostic used incompatible price or ATR inputs")
    model = early_observations(bars)
    native_keys = {(x["known_ms"], x["side"]) for x in observed}
    model_keys = {(x["known_ms"], x["side"]) for x in model}
    status = "RECONCILED_CAUSAL_NATIVE_INPUTS" if native_keys == model_keys else "NATIVE_CANDLES_MATCH_MODEL_DIFFERS"
    return {"status": status, "source": export.meta,
            "early_watch_total": len(observed), "early_by_direction": dict(Counter("BUY" if r["side"] > 0 else "SELL" for r in observed)),
            "modeled_same_window_watch_total": len(model), "early_watch_shared_ids": len(native_keys & model_keys),
            "range_filter_states": dict(Counter(r["state"] for r in filters)), "range_filter_rows": filters}


def analyze_frame(frame, archive, event_rules, native_dirs, sealed_start, experiments):
    ref, rc1, match = load_reference(frame, archive)
    bars = chart_bars(ref)
    offers = offers_from_ready(ref, frame)
    original, counts = native_campaigns(ref)
    lanes = {}
    for policy, lock_r in POLICIES.items():
        tape, outcomes = replay(offers, bars, lock_r)
        lanes[policy] = performance(tape, bars, outcomes)
    filtered, outcomes = replay(offers, bars, None, range_filter=True)
    lanes["FIXED_RANGE_FILTER_175_ATR"] = performance(filtered, bars, outcomes)
    rc2_native = {}
    candidate_exports = {}
    for name, (path, prefix) in native_dirs.items():
        file = path / f"{prefix}_M{frame}.csv"
        if not file.exists():
            rc2_native[name] = {"status": "MISSING_NATIVE_EXPORT"}
            continue
        try:
            candidate = read_export(file, frame, "audit")
            if candidate.context[-1].closing >= sealed_start:
                raise ValueError("RC2 descriptive inputs must end strictly before the sealed holdout")
            tape, outcomes = native_campaigns(candidate)
            properties = native_properties(file, candidate, frame)
        except (OSError, ValueError) as exc:
            rc2_native[name] = {"status": "NATIVE_EVIDENCE_REJECTED", "reason": str(exc)}
        else:
            candidate_exports[name] = candidate
            context = compare_exports(ref, candidate, ())
            rc2_native[name] = {
                "status": "IDENTICAL_ARCHIVE_CONTEXT" if context["status"] == "MATCH" else "NONMATCHING_ARCHIVE_CONTEXT",
                "full_native_reference_parity": compare_exports(ref, candidate, ("core", "ready", "native")),
                "shared_bar_archive_comparison": shared_bar_comparison(ref, candidate, ("core", "ready", "native")),
                "source": candidate.meta, "descriptive": performance(tape, chart_bars(candidate), outcomes),
                "strategy_api": properties,
            }
    controls = [name for name, result in rc2_native.items()
                if result.get("strategy_api", {}).get("native_strategy_properties", {}).get("mode") == "S5 RAW Parity"]
    if len(controls) == 1:
        control = candidate_exports[controls[0]]
        for name, export in candidate_exports.items():
            if name != controls[0]:
                rc2_native[name]["same_window_rc2_parity_mode"] = compare_exports(control, export, ("core", "ready"))
    try:
        experiment = native_experiment(experiments / f"research_M{frame}.csv" if experiments else None, frame, sealed_start)
    except (OSError, ValueError) as exc:
        experiment = {"status": "NATIVE_EXPERIMENT_EVIDENCE_REJECTED", "reason": str(exc)}
    return {
        "native_provenance": {"reference": ref.meta, "rc1": rc1.meta},
        "native_reference_vs_rc1": match,
        "native_s5_raw_and_rc1_identical": performance(original, bars, counts),
        "native_rc2": rc2_native or {"status": "MISSING_NATIVE_EXPORT"},
        "native_experimental_observations": experiment,
        "ohlc_model_lanes": lanes,
        "invalidated_and_risk_rejected": decision_diagnostics(ref, bars, frame),
        "missed_directional_hours": missed_moves(ref, bars, frame, offers, original, event_rules),
    }


def report(native_dirs=None, root=ROOT, *, experiments=None):
    pins = {path.as_posix(): digest((root / path).read_bytes()) for path in PINNED}
    for path, sha in PINNED.items():
        if pins[path.as_posix()] != sha:
            raise ValueError(f"Protected archive or source changed: {path}")
    event_rules = json.loads((root / EVENT_RULES).read_bytes())["event"]
    seal = json.loads((root / SEAL).read_bytes())
    with TemporaryDirectory() as temp, ZipFile(root / ARCHIVE) as packed:
        folder = Path(temp)
        for frame in FRAMES:
            for role in ("reference", "audit"):
                name = f"{role}_M{frame}.csv"
                (folder / name).write_bytes(packed.read(f"strict/{name}"))
        frames = {f"M{frame}": analyze_frame(frame, folder, event_rules, native_dirs or {}, seal["forward_start_ms"], experiments)
                  for frame in FRAMES}
    return {
        "schema": "qirvanta.gold-pro.rc2.research.v1", "status": "ARCHIVE_DESCRIPTIVE_ONLY",
        "symbol": "OANDA:XAUUSD", "pinned_sha256": pins,
        "unchanged_forward_seal": {"start_utc": utc(seal["forward_start_ms"]), "end_utc": utc(seal["forward_end_ms"]),
                                   "holdout_status": "NOT_AVAILABLE", "forward_scoring_performed": False},
        "cost_model": {"tick": COST.tick, "pointvalue": COST.pointvalue, "units": UNITS, "starting_capital_usd": 100000,
                       "commission_per_fill_per_unit": COST.commission, "market_and_stop_adverse_ticks": COST.ticks,
                       "native_slippage_already_in_fill_prices": True, "round_trip_spread_price": COST.spread,
                       "spread_status": "FIXED_SENSITIVITY_NOT_OBSERVED_BID_ASK"},
        "interpretation": {
            "native": "Actual archived emulator fills, equal fees and fixed additional spread sensitivity; exchange fills and mark-to-market equity are unavailable.",
            "ohlc": "Same causal READY tape. Next available open with adverse ticks; known open crosses precede stop-first unordered touches; 3 equal target parts; protection becomes effective next bar.",
            "session": "RC2 display's fixed UTC bands, assigned at the actual or modeled entry timestamp.",
            "mae_mfe": "Quote-unit excursion from actual or modeled fill, observed parts and full interior candles. Entry and final exit bar extrema are excluded.",
            "missed_moves": "Post-close qualifying UTC hours and forward ranges are diagnostic price observations. They provide no evidence that a missed position would have made money.",
            "partitions": "Both chronological archive partitions were already inspected. No promotion inference or selection based on this archive.",
        },
        "frames": {**frames, "H1": {"status": "MISSING_NATIVE_EVIDENCE"}},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--native", action="append", default=[], metavar="LANE=DIRECTORY",
                        help="Optional RC2 native Pine export folders containing audit_M5.csv etc.")
    parser.add_argument("--native-flat", type=Path, metavar="DIRECTORY",
                        help="Optional native export bundle with fixed/parity/be/lock_M5.csv etc.")
    args = parser.parse_args()
    lanes = {name: (args.native_flat, name) for name in ("fixed", "parity", "be", "lock")} if args.native_flat else {}
    for value in args.native:
        lane, sep, directory = value.partition("=")
        if not sep or not lane or not directory or lane in lanes:
            parser.error("--native requires unique LANE=DIRECTORY values")
        lanes[lane] = (Path(directory), "audit")
    result = report(lanes, experiments=args.native_flat)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes((json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False, default=str) + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
