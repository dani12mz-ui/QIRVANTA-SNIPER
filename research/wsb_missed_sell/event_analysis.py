"""Reconcile the reported WSB chart with closed OANDA M5 and M15 candles."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from pathlib import Path


D = Decimal
BAR_MS = 5 * 60_000
FRAME_MS = {5: BAR_MS, 15: 15 * 60_000}
SYMBOL = "OANDA:XAUUSD"
SCREENSHOT_SHA256 = "eb7456c22e8bc9985c5c5af1fc09d06d4ab77ee30f5756c19fe80d2da5c32e28"
ENTRY = D("4049.570")
STOP = D("4058.231")
TARGETS = (D("4040.909"), D("4032.248"), D("4023.588"))
SCREEN_PRICE = D("4001.910")
SCREENSHOT_AT = datetime(2026, 11, 11, 15, 55, 30, tzinfo=timezone.utc)
DISPLAYED_COUNTDOWN_SECONDS = 272
VISIBLE_INTERVALS = 26
# Candle bodies identifiable in the source image. The open final candle is excluded.
BODY_DIRECTIONS = {
    0: -1, 1: 1, 2: 1, 5: -1, 6: 1, 7: 1, 9: -1, 10: -1, 11: 1, 12: -1,
    13: -1, 14: 1, 16: -1, 17: -1, 18: 1, 19: -1, 21: -1, 22: -1, 23: 1,
    24: -1, 25: -1,
}
BAR_LAYOUTS = {
    "RC1_CTX": (6, 7, 8),
    "WICK_CTX": (4, 5, 6),
    "GOLD_CTX": (5, 6, 7),
    "WSB_OHLC": (4, 5, 6),
}
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
DISPLAY_TICK = D("0.001")


def milliseconds(time: datetime) -> int:
    return (time - EPOCH) // timedelta(milliseconds=1)


def utc(stamp: int) -> str:
    return (EPOCH + timedelta(milliseconds=stamp)).isoformat(timespec="seconds").replace("+00:00", "Z")


def screenshot_bar_open(minutes: int) -> int:
    return milliseconds(SCREENSHOT_AT) // FRAME_MS[minutes] * FRAME_MS[minutes]


def visual_signal_open(minutes: int) -> int:
    return screenshot_bar_open(minutes) - VISIBLE_INTERVALS * FRAME_MS[minutes]


SCREEN_BAR_OPEN = screenshot_bar_open(5)
DAY_OPEN = milliseconds(SCREENSHOT_AT.replace(hour=0, minute=0, second=0))


@dataclass(frozen=True)
class Bar:
    opening: int
    closing: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self):
        prices = (self.open, self.high, self.low, self.close)
        duration = self.closing - self.opening
        if (
            duration not in FRAME_MS.values() or self.opening % duration != 0
            or not all(price.is_finite() and price > 0 for price in prices)
            or self.low > min(self.open, self.close) or self.high < max(self.open, self.close)
        ):
            raise ValueError(f"Invalid closed OANDA candle at {self.opening}")

    @property
    def minutes(self) -> int:
        return (self.closing - self.opening) // 60_000

    def direction(self) -> int:
        return (self.close > self.open) - (self.close < self.open)

    def evidence(self) -> dict:
        return {
            "open_utc": utc(self.opening), "close_utc": utc(self.closing),
            "timeframe_minutes": self.minutes,
            "open": self.open, "high": self.high, "low": self.low, "close": self.close,
        }


def records(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file, strict=True)
        if "Message" not in (reader.fieldnames or []):
            raise ValueError(f"{path}: expected a TradingView Pine Logs Message column")
        for row in reader:
            for line in row["Message"].splitlines():
                yield reader.line_num, line


def read_context(path: Path) -> dict[int, dict[int, Bar]]:
    """Read closed native candles and fail on conflicting duplicate values."""
    groups = {}
    for line_number, line in records(path):
        tag, _, raw = line.partition("|")
        try:
            if tag == "WSB_ORIGINAL_CTX":
                item = json.loads(raw, parse_float=D)
                if item["symbol"] != SYMBOL or str(item["timeframe"]) not in {"5", "15"}:
                    continue
                minutes = int(item["timeframe"])
                prices = (D(item[key]) for key in ("open", "high", "low", "close"))
                bar = Bar(int(item["open_ms"]), int(item["close_ms"]), *prices)
            elif tag in BAR_LAYOUTS:
                fields = line.split("|")
                if len(fields) < 4 or fields[2] != SYMBOL or fields[3] not in {"5", "15"}:
                    continue
                minutes = int(fields[3])
                opening, closing, price = BAR_LAYOUTS[tag]
                bar = Bar(int(fields[opening]), int(fields[closing]), *(D(v) for v in fields[price:price + 4]))
            else:
                continue
            if bar.minutes != minutes:
                raise ValueError("Candle duration differs from the exported timeframe")
        except (IndexError, KeyError, TypeError, InvalidOperation, ValueError) as error:
            raise ValueError(f"{path}:{line_number}: invalid {tag} record") from error
        previous = groups.setdefault(minutes, {}).setdefault(bar.opening, bar)
        if bar != previous:
            raise ValueError(f"{path}: conflicting candles at {utc(bar.opening)}")
    if not groups:
        raise ValueError(f"{path}: no closed {SYMBOL} M5/M15 context records")
    return groups


def align_sources(paths: list[Path]) -> tuple[dict[int, dict[int, Bar]], list[dict]]:
    aligned = {}
    evidence = []
    for path in paths:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        for minutes, bars in read_context(path).items():
            same_frame = aligned.setdefault(minutes, {})
            shared = same_frame.keys() & bars.keys()
            for stamp in sorted(shared):
                if same_frame[stamp] != bars[stamp]:
                    raise ValueError(f"{path}: OANDA M{minutes} price conflict at {utc(stamp)}")
            same_frame.update(bars)
            evidence.append({
                "file": path.name, "sha256": digest, "bars": len(bars),
                "timeframe_minutes": minutes, "first_open_utc": utc(min(bars)),
                "last_close_utc": utc(bars[max(bars)].closing),
                "exact_ohlc_overlap_with_previous_same_frame_sources": len(shared),
            })
    return aligned, evidence


def aggregate_m15(m5: dict[int, Bar]) -> dict[int, Bar]:
    result = {}
    for stamp, first in m5.items():
        if stamp % FRAME_MS[15] or stamp + BAR_MS not in m5 or stamp + 2 * BAR_MS not in m5:
            continue
        second, third = m5[stamp + BAR_MS], m5[stamp + 2 * BAR_MS]
        result[stamp] = Bar(stamp, third.closing, first.open, max(first.high, second.high, third.high),
                            min(first.low, second.low, third.low), third.close)
    return result


def candidate(bars: dict[int, Bar], bar: Bar) -> dict:
    matches, conflicts, missing = [], [], []
    minutes = bar.minutes
    for offset, direction in BODY_DIRECTIONS.items():
        stamp = bar.opening + offset * FRAME_MS[minutes]
        visible = bars.get(stamp)
        if stamp >= screenshot_bar_open(minutes) or visible is None:
            missing.append(offset)
        elif visible.direction() == direction:
            matches.append(offset)
        else:
            conflicts.append(offset)
    exact = bar.opening == visual_signal_open(minutes) and bar.close == ENTRY and not conflicts and not missing
    return {
        **bar.evidence(),
        "price_equals_displayed_entry": bar.close == ENTRY,
        "opening_equals_screenshot_spacing_anchor": bar.opening == visual_signal_open(minutes),
        "candle_body_match_count": len(matches),
        "candle_body_conflict_offsets": conflicts,
        "candle_body_unavailable_offsets": missing,
        "classification": "MARKET_CANDLE_MATCH" if exact else "PRICE_OR_VISUAL_ANCHOR_ONLY",
    }


def original_matches(paths: list[Path], groups: dict[int, dict[int, Bar]]) -> list[dict]:
    """Match the rounded displayed geometry to the original Pine probe output."""
    matches = {}
    for path in paths:
        for line_number, line in records(path):
            tag, _, raw = line.partition("|")
            if tag != "WSB_ORIGINAL_PLAN":
                continue
            try:
                item = json.loads(raw, parse_float=D)
                if str(item["direction"]).upper() not in {"SELL", "SHORT", "-1"}:
                    continue
                values = tuple(D(item[key]) for key in ("entry", "sl", "tp1", "tp2", "tp3"))
                if tuple(price.quantize(DISPLAY_TICK, rounding=ROUND_HALF_UP) for price in values) != (ENTRY, STOP, *TARGETS):
                    continue
                opening, closing = int(item["open_ms"]), int(item["close_ms"])
                minutes = (closing - opening) // 60_000
                bars = groups.get(minutes, {})
                if opening not in bars or bars[opening].closing != closing:
                    raise ValueError("plan timestamp has no matching closed OANDA candle")
                item["bar_open_utc"], item["bar_close_utc"] = utc(opening), utc(closing)
                item["timeframe_minutes"] = minutes
                item["log_file"] = path.name
                item["matches_visual_open_anchor"] = opening == visual_signal_open(minutes)
            except (KeyError, TypeError, InvalidOperation, ValueError) as error:
                raise ValueError(f"{path}:{line_number}: invalid {tag} record") from error
            matches.setdefault((opening, minutes, values), item)
    return [item for _, item in sorted(matches.items())]


@dataclass(frozen=True)
class Costs:
    name: str
    spread: Decimal
    slippage: Decimal
    fee_per_side: Decimal = D("0.175")


COSTS = (
    Costs("baseline", D("0.350"), D("0.002")),
    Costs("zero_spread_sensitivity", D("0"), D("0.002")),
    Costs("wider_spread_and_slippage", D("0.700"), D("0.005")),
)


def evidence_window(bars: dict[int, Bar], confirmation: int) -> tuple[list[Bar], list[str]]:
    stamps = range(confirmation, SCREEN_BAR_OPEN, BAR_MS)
    return [bars[t] for t in stamps if t in bars], [utc(t) for t in stamps if t not in bars]


def touches(bars: list[Bar], threshold: Decimal, is_stop: bool) -> dict | None:
    for bar in bars:
        opening = bar.open >= threshold if is_stop else bar.open <= threshold
        crossed = bar.high >= threshold if is_stop else bar.low <= threshold
        if crossed:
            return {
                "threshold": threshold, "bar_open_utc": utc(bar.opening),
                "bar_close_utc": utc(bar.closing),
                "known_at_open": opening,
                "observable_from_open_or_closed_ohlc_utc": utc(bar.opening if opening else bar.closing),
                "exact_intrabar_timestamp": None,
            }
    return None


def simulate_short(bars: list[Bar], costs: Costs, runner: bool = False) -> dict:
    """A conditional next-open short with stop-first unresolved intrabar ordering."""
    if not bars or any(before.closing != after.opening for before, after in zip(bars, bars[1:])):
        raise ValueError("Execution scenario needs a nonempty contiguous completed window")
    half_spread = costs.spread / 2
    next_open = bars[0].open
    entry = next_open - half_spread - costs.slippage
    price_risk = STOP - entry
    result = {
        "name": costs.name, "runner": runner,
        "spread": costs.spread, "slippage_per_market_or_stop_execution": costs.slippage,
        "commission_per_side_per_unit": costs.fee_per_side,
        "next_open_utc": utc(bars[0].opening), "next_open_mid_proxy": next_open,
        "modeled_market_fill": entry, "risk_per_unit_at_fill": price_risk,
    }
    if next_open + half_spread >= STOP or entry <= TARGETS[0]:
        return {**result, "status": "ENTRY_OUTSIDE_PLAN", "fills": []}
    remaining = list(enumerate(TARGETS, start=1))
    if runner:
        remaining.append((4, None))
    units = D(3)
    leg_quantity = units / len(remaining)
    stop = STOP
    tp3_complete = False
    fills = []
    stop_first_bars = []

    def record(bar, leg, reason, price, at_open=False):
        net_per_unit = entry - price - 2 * costs.fee_per_side
        fills.append({
            "leg": leg, "reason": reason, "price": price, "quantity": leg_quantity,
            "bar_open_utc": utc(bar.opening), "bar_close_utc": utc(bar.closing),
            "known_at_open": at_open, "net_usd_per_unit": net_per_unit, "net_usd": net_per_unit * leg_quantity,
        })

    for bar in bars:
        ask_open, ask_high, ask_low = (mid + half_spread for mid in (bar.open, bar.high, bar.low))
        if ask_open >= stop:
            for leg, _ in remaining:
                record(bar, leg, "SL_GAP", ask_open + costs.slippage, True)
            remaining.clear()
            break
        open_targets = [(leg, target) for leg, target in remaining if target is not None and ask_open <= target]
        for leg, target in open_targets:
            record(bar, leg, f"TP{leg}", target, True)
            remaining.remove((leg, target))
            tp3_complete |= leg == 3
        reachable = [(leg, target) for leg, target in remaining if target is not None and ask_low <= target]
        if ask_high >= stop:
            if reachable:
                stop_first_bars.append(utc(bar.opening))
            for leg, _ in remaining:
                record(bar, leg, "SL", stop + costs.slippage)
            remaining.clear()
            break
        for leg, target in reachable:
            record(bar, leg, f"TP{leg}", target)
            remaining.remove((leg, target))
            tp3_complete |= leg == 3
        if not remaining:
            break
        # A runner is kept as a separate offline scenario. No stop change applies to the TP3 candle.
        if runner and tp3_complete and bar.close + half_spread < TARGETS[1]:
            stop = min(stop, TARGETS[1])

    realized = sum((fill["net_usd"] for fill in fills), D(0))
    initial_risk = units * price_risk
    result.update(
        status="ALL_LEGS_CLOSED" if not remaining else "OPEN_AT_CUTOFF",
        fills=fills,
        allocated_units=units,
        realized_net_usd=realized,
        realized_net_r=realized / initial_risk,
        commissions_on_closed_legs=len(fills) * leg_quantity * 2 * costs.fee_per_side,
        entry_commission_on_open_legs=len(remaining) * leg_quantity * costs.fee_per_side,
        ambiguity_stop_first_bar_opens=stop_first_bars,
        unclosed_legs=[leg for leg, _ in remaining],
    )
    if remaining:
        mark = bars[-1].close + half_spread + costs.slippage
        marked = realized + len(remaining) * leg_quantity * (entry - mark - 2 * costs.fee_per_side)
        result.update(mark_only_exit_price=mark, marked_total_net_usd=marked, marked_total_net_r=marked / initial_risk)
    return result


def market_analysis(bars: dict[int, Bar], confirmation: int) -> dict:
    window, missing = evidence_window(bars, confirmation)
    if not window or missing:
        return {
            "status": "INCOMPLETE_CLOSED_CANDLES",
            "confirmation_assumed_utc": utc(confirmation),
            "missing_bar_opens": missing,
        }
    low = min(window, key=lambda bar: bar.low)
    high = max(window, key=lambda bar: bar.high)
    stop = touches(window, STOP, True)
    targets = [touches(window, level, False) for level in TARGETS]
    same_bar = [i for i, target in enumerate(targets, 1)
                if target is not None and stop is not None and target["bar_open_utc"] == stop["bar_open_utc"]]
    return {
        "status": "CONDITIONAL_NEXT_OPEN_REPLAY",
        "execution_timeframe_minutes": 5,
        "level_basis": "Observed tick-rounded stop and target prices",
        "confirmation_assumed_utc": utc(confirmation),
        "next_open_utc": utc(window[0].opening),
        "close_to_next_open_clock_seconds": (window[0].opening - confirmation) // 1_000,
        "last_completed_close_utc": utc(window[-1].closing),
        "completed_bars_after_confirmation": len(window),
        "lowest_bar": low.evidence(), "highest_bar": high.evidence(),
        "raw_market_mfe_from_reference": max(D(0), ENTRY - low.low),
        "raw_market_mae_from_reference": max(D(0), high.high - ENTRY),
        "raw_market_mfe_from_next_open": max(D(0), window[0].open - low.low),
        "raw_market_mae_from_next_open": max(D(0), high.high - window[0].open),
        "raw_stop_touch": stop, "raw_target_touches": targets,
        "raw_stop_and_target_same_bar": same_bar,
        "three_equal_legs": [simulate_short(window, costs) for costs in COSTS],
        "separate_runner_four_equal_legs": [simulate_short(window, costs, runner=True) for costs in COSTS],
        "observability": "OHLC spans cannot verify the tick timestamp or a broker fill.",
        "runner_rule": "After TP3, when ask close is below TP2, tighten the remaining leg to TP2 next bar. Open residual at cutoff is marked only.",
    }


def report(paths: list[Path], screenshot: Path | None = None, original_paths: list[Path] | None = None) -> dict:
    groups, sources = align_sources(paths)
    if screenshot is not None:
        sha = hashlib.sha256(screenshot.read_bytes()).hexdigest()
        if sha != SCREENSHOT_SHA256:
            raise ValueError("The provided image differs from the observed WSB screenshot")
    m5 = groups.get(5, {})
    m15 = groups.get(15, {})
    aggregate = aggregate_m15(m5)
    overlap = m15.keys() & aggregate.keys()
    disagreements = [m15[t].evidence() for t in sorted(overlap) if m15[t] != aggregate[t]]
    originals = original_matches(original_paths or [], groups)
    candidates = []
    frame_scopes = []
    for minutes, bars in ((5, m5), (15, {**aggregate, **m15})):
        screenshot_open = screenshot_bar_open(minutes)
        signal_open = visual_signal_open(minutes)
        frame_scopes.append({
            "timeframe_minutes": minutes, "inferred_current_bar_open_utc": utc(screenshot_open),
            "inferred_signal_bar_open_utc": utc(signal_open),
            "inferred_signal_bar_close_utc": utc(signal_open + FRAME_MS[minutes]),
        })
        for bar in sorted(bars.values(), key=lambda item: item.opening):
            if DAY_OPEN <= bar.opening < screenshot_open and (bar.close == ENTRY or bar.opening == signal_open):
                candidates.append(candidate(bars, bar))
    market_matches = [row for row in candidates if row["classification"] == "MARKET_CANDLE_MATCH"]
    at_anchor = [item for item in originals if item["matches_visual_open_anchor"]]
    native_match = at_anchor[0] if len(at_anchor) == 1 else None
    market_match = market_matches[0] if len(market_matches) == 1 else None
    if native_match:
        minutes = native_match["timeframe_minutes"]
        status = "NATIVE_HISTORICAL_REPRODUCTION"
    elif market_match:
        minutes = market_match["timeframe_minutes"]
        status = "MARKET_CANDLE_MATCH"
    else:
        minutes = None
        status = "INSUFFICIENT_MARKET_EVIDENCE"
    return {
        "schema": "qirvanta.wsb-missed-sell.event.v2",
        "symbol_under_test": SYMBOL, "reconciled_signal_timeframe_minutes": minutes,
        "status": status,
        "native_wsb_signal_identity": "NATIVE_HISTORICAL_REPRODUCTION" if native_match else "UNVERIFIED",
        "native_wsb_matching_plans": originals,
        "screenshot": {
            "sha256": SCREENSHOT_SHA256, "source_verified_against_hash": screenshot is not None,
            "filesystem_capture_utc": SCREENSHOT_AT.isoformat(),
            "displayed_countdown_seconds": DISPLAYED_COUNTDOWN_SECONDS,
            "displayed_live_price": SCREEN_PRICE, "entry": ENTRY, "stop": STOP, "targets": TARGETS,
            "displayed_risk_distance": STOP - ENTRY,
            "displayed_reference_to_live_move": ENTRY - SCREEN_PRICE,
            "displayed_reference_to_live_r_multiple": (ENTRY - SCREEN_PRICE) / (STOP - ENTRY),
            "visual_intervals_from_arrow_to_current_candle": VISIBLE_INTERVALS,
            "candle_body_direction_offsets": BODY_DIRECTIONS,
            "tested_timeframe_interpretations": frame_scopes,
            "limitations": "The image crop omits the time axis, symbol/provider, script version and inputs.",
        },
        "sources": sources,
        "m15_ohlc_cross_check": {
            "aggregate_source": "Three complete consecutive OANDA M5 bars per M15 bar",
            "aggregate_bars": len(aggregate), "native_m15_overlap": len(overlap),
            "native_m15_ohlc_disagreements": disagreements,
        },
        "candidates": candidates,
        "case_execution": market_analysis(m5, visual_signal_open(minutes) + FRAME_MS[minutes]) if minutes else None,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", action="append", type=Path, required=True, help="Native M5 or M15 Pine Logs CSV, repeatable")
    parser.add_argument("--original", action="append", type=Path, help="Native original WSB plan logs, repeatable")
    parser.add_argument("--screenshot", type=Path, help="Original user screenshot for SHA-256 verification")
    parser.add_argument("--report", type=Path, required=True, help="Write a JSON evidence report")
    args = parser.parse_args()
    result = report(args.bars, args.screenshot, args.original)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_bytes((json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
