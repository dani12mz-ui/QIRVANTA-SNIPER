"""Report bounded RAW simulation outcomes from validated H1 native A/B journals."""

from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from statistics import median
from types import SimpleNamespace

from gold_pro.rc1_qa import require
from research.net_campaign_evidence.cooldown_open_checks import cost_scenarios
from research.net_campaign_evidence.metrics import FLAT_EPSILON, campaign_metrics, marked_drawdown
from research.net_campaign_evidence.native_raw_metrics import PARTS, SERIALIZATION_TOLERANCE, summarize_raw


USD_RISK = Decimal(250)
TICK = Decimal("0.001")
FLAT_R = Decimal("0.00000001")
ZERO = Decimal(0)
MINUTE = 60_000
HOUR = 60 * MINUTE
INTERVAL = 15 * MINUTE
START = 1794355200000  # 2026-11-11 00:00 UTC
END = 1794607200000    # 2026-11-13 22:00 UTC


def checked_tape(run):
    """Require the natural origin, one RAW state per market close and paired evidence."""
    bars = tuple(run["bars"])
    ledger = tuple(run["books"]["RAW"])
    events = tuple(row for row in run["events"] if row["book"] == "RAW")
    require(bars and len(ledger) == len(bars), "Full RAW ledger and OHLC are required")
    require(all(bar["index"] == index and bar["close_ms"] == book["close_ms"]
                and bar["close_ms"] - bar["open_ms"] == INTERVAL and
                (index == 0 or bar["open_ms"] >= bars[index - 1]["close_ms"])
                for index, (bar, book) in enumerate(zip(bars, ledger))),
            "RAW books and chronological chart are not aligned from index zero")
    require(bars[0]["close_ms"] < START < END == bars[-1]["close_ms"]
            and any(bar["close_ms"] == START for bar in bars),
            "Frozen score window needs its complete native origin, starting close and 13 Nov cutoff")
    first = ledger[0]
    require(first["phase"] == 0 and first["entered"] == first["closed"] == first["net"] == 0
            and all(event["known_time"] != bars[0]["close_ms"] for event in events),
            "Unknown starting RAW portfolio state")
    context = {bar["close_ms"]: SimpleNamespace(opening=bar["open_ms"]) for bar in bars}
    ledger = tuple({**row, "known_ms": row["close_ms"]} for row in ledger)
    raw = summarize_raw({"s5": {"RAW": events, "LEDGER": ledger}, "context": context})
    return bars, ledger, events, raw


def new_fill(event, scenario):
    """Reference units give $250 loss at a non-gapped initial stop per scenario."""
    distance = (event["price"] - event["initial_sl"]) * event["side"]
    require(distance >= TICK, f'{event["plan_id"]}: invalid observed initial stop distance')
    model = {"distance": distance, "entry": event["price"]}
    if scenario is None:
        return model
    spread = scenario["spread_usd"]
    slip = scenario["slippage_ticks"] * TICK
    fees = 2 * scenario["commission_per_side_per_unit_usd"]
    stop_friction = spread + 2 * slip + fees
    return {
        **model, "stop_friction_per_unit": stop_friction, "spread": spread,
        "slip": slip, "fees": fees, "units": USD_RISK / (distance + stop_friction),
    }


def priced_part(event, fill, fraction, scenario):
    if scenario is None:
        return event["net_r"]
    direction = event["side"]
    entry_quote = fill["entry"] + direction * (fill["spread"] / 2 + fill["slip"])
    exit_slip = ZERO if event["event"] in {"SIM_TP1", "SIM_TP2", "SIM_TP3"} else fill["slip"]
    exit_quote = event["price"] - direction * (fill["spread"] / 2 + exit_slip)
    return fraction * fill["units"] * (direction * (exit_quote - entry_quote) - fill["fees"])


def mark_open(fill, position, price, scenario):
    direction, remaining = position["side"], position["remaining"]
    delta = direction * (price - fill["entry"])
    if scenario is None:
        return remaining * (delta - position["native_cost"]) / fill["distance"]
    cost = fill["stop_friction_per_unit"]
    return remaining * fill["units"] * (delta - cost)


def economics(bars, ledger, events, raw, scenario):
    """Use observed native chronology and marks, with no change to execution prices in the logs."""
    events_at = defaultdict(list)
    for event in events:
        events_at[event["known_time"]].append(event)
    roster = {row["plan_id"]: dict(row) for row in raw["campaign_roster"]}
    active = None
    settled = ZERO
    output_marks = []
    for bar, native in zip(bars, ledger):
        stamp = bar["close_ms"]
        for event in events_at[stamp]:
            kind, plan_id = event["event"], event["plan_id"]
            if kind == "SIM_ENTRY":
                require(active is None and event["effective_time"] == bar["open_ms"],
                        f"{plan_id}: missing original exit or inconsistent entry timing")
                model = new_fill(event, scenario)
                active = {
                    "id": plan_id, "side": event["side"], "remaining": Decimal(1),
                    "native_cost": event["roundtrip_cost_price"], "realized": ZERO,
                    "part_valuation": [], "model": model,
                }
                roster[plan_id]["initial_stop_distance"] = model["distance"]
                if scenario is not None:
                    roster[plan_id]["modeled_units"] = model["units"]
                    roster[plan_id]["modeled_non_gap_initial_sl_risk_usd"] = USD_RISK
                    roster[plan_id]["modeled_stop_friction_per_unit"] = model["stop_friction_per_unit"]
            elif kind in PARTS:
                require(active is not None and active["id"] == plan_id, f"{plan_id}: exit lacks observed entry")
                fraction = event["quantity_fraction"]
                residual = active["remaining"] - fraction
                require(residual >= -SERIALIZATION_TOLERANCE, f"{plan_id}: close fractions exceed one")
                # Treat the sub-precision residual of a final third as zero.
                if abs(residual) <= SERIALIZATION_TOLERANCE:
                    fraction, residual = active["remaining"], ZERO
                value = priced_part(event, active["model"], fraction, scenario)
                active["realized"] += value
                active["remaining"] = residual
                active["part_valuation"].append({"event_id": event["event_id"], "event": kind, "known_ms": stamp,
                                                 "repriced_fraction": fraction, "net_value": value})
            elif kind == "CLOSED":
                require(active is not None and active["id"] == plan_id and active["remaining"] == 0,
                        f"{plan_id}: CLOSED without a fully settled observed position")
                value = active["realized"] if scenario is not None else event["net_r"]
                row = roster[plan_id]
                row["scenario_net"] = value
                row["valuation_parts"] = active["part_valuation"]
                settled += value
                active = None

        if native["phase"] == 2:
            require(active is not None and active["id"] == native["plan_id"]
                    and native["entry"] == active["model"]["entry"]
                    and abs(native["risk"] - active["model"]["distance"]) <= SERIALIZATION_TOLERANCE
                    and abs(native["remaining"] - active["remaining"]) <= SERIALIZATION_TOLERANCE,
                    f"{stamp}: RAW PAPER open state is inconsistent with observed fills and exits")
            mark = active["realized"] + mark_open(active["model"], active, bar["close"], scenario)
        else:
            require(active is None, f"{stamp}: unsettled position absent from the RAW ledger")
            mark = ZERO
        output_marks.append((stamp, settled + mark))

    if active is not None:
        row = roster[active["id"]]
        row["valuation_at_cutoff"] = {
            "remaining_fraction": active["remaining"], "realized_parts_net": active["realized"],
            "liquidation_net_including_realized_parts": mark,
        }
        row["valuation_parts"] = active["part_valuation"]
    return list(roster.values()), output_marks


def marked_window(marks):
    origin = next(value for stamp, value in marks if stamp == START)
    rebased = [(stamp, value - origin) for stamp, value in marks if START <= stamp <= END]
    return {**marked_drawdown(rebased), "scope": "Score-window close marks incl. carried exposure, rebased at start",
            "first_value_before_rebase": origin, "last_value_rebased": rebased[-1][1],
            "series_rebased": rebased}


def day(stamp):
    return datetime.fromtimestamp(stamp / 1000, timezone.utc).date()


def native_stats(campaigns):
    returns = [item["scenario_net"] for item in campaigns]
    wins, losses = [r for r in returns if r > FLAT_R], [r for r in returns if r < -FLAT_R]
    flats = len(returns) - len(wins) - len(losses)
    expectancy = sum(returns, ZERO) / len(returns) if returns else None
    return {
        "closed": len(returns), "wins": len(wins), "losses": len(losses), "flats": flats,
        "flat_epsilon_r": FLAT_R, "win_rate": len(wins) / len(returns) if returns else None,
        "loss_rate": len(losses) / len(returns) if returns else None,
        "flat_rate": flats / len(returns) if returns else None,
        "net_r": sum(returns, ZERO), "expectancy_r": expectancy,
        "profit_factor_r": sum(wins, ZERO) / -sum(losses, ZERO) if losses else None,
        "average_win_r": sum(wins, ZERO) / len(wins) if wins else None,
        "average_loss_r": sum(losses, ZERO) / len(losses) if losses else None,
        "worst_loss_r": min(losses, default=None),
    }


def cohort(roster, marks, scenario):
    fills = [row for row in roster if row["entry_effective_open_ms"] is not None
             and START <= row["entry_effective_open_ms"] < END]
    closed = sorted((r for r in fills if r["state"] == "COMPLETE_IN_WINDOW"),
                    key=lambda r: (r["close_known_ms"], r["plan_id"]))
    carry = [r for r in roster if r["entry_effective_open_ms"] is not None
             and r["entry_effective_open_ms"] < START and (r["close_known_ms"] is None or r["close_known_ms"] > START)]
    cutoff = [r for r in roster if r["cuts_at_last_ledger"]]
    in_window_plans = [r for r in roster if any(
        e["event"] in {"QUEUED", "CAPACITY"} and START <= e["known_time"] <= END for e in r["events"]
    )]
    if scenario is None:
        stats = native_stats(closed)
    else:
        trading_days = sorted({day(stamp) for stamp, _ in marks if START < stamp <= END})
        normalized = [{"id": r["plan_id"], "close_ms": r["close_known_ms"],
                       "net_usd": r["scenario_net"], "net_r": r["scenario_net"] / USD_RISK}
                      for r in closed]
        stats = campaign_metrics(normalized, trading_days)
    return {
        "closed_cohort": stats, "filled": len(fills), "filled_plan_ids": [r["plan_id"] for r in fills],
        "closed_plan_ids": [r["plan_id"] for r in closed],
        "carry_in_campaigns": carry, "open_or_pending_at_cutoff": cutoff,
        "campaign_states_all_history": dict(Counter(r["state"] for r in roster)),
        "plan_states_score_window": dict(Counter(r["state"] for r in in_window_plans)),
        "completed_exit_part_reasons": dict(Counter(
            e["event"] for r in closed for e in r["events"] if e["event"] in PARTS
        )),
        "marked_close_equity": marked_window(marks),
        "full_observed_campaign_roster": roster,
    }


def coverage(bars, roster):
    """Use the independent fixed UTC-hour criterion. OHLC is outcome data here."""
    by_hour = defaultdict(list)
    fills = defaultdict(list)
    for bar in bars:
        by_hour[bar["open_ms"] // HOUR * HOUR].append(bar)
    for row in roster:
        stamp = row["entry_effective_open_ms"]
        if stamp is not None and START <= stamp < END:
            fills[stamp // HOUR * HOUR, row["side"]].append((stamp, row["plan_id"]))
    intervals = []
    rows = []
    scored_hours = 0
    for start, group in by_hour.items():
        expected = list(range(start, start + HOUR, INTERVAL))
        if ([b["open_ms"] for b in group] != expected
                or any(b["close_ms"] - b["open_ms"] != INTERVAL for b in group)):
            continue
        span = max(b["high"] for b in group) - min(b["low"] for b in group)
        move = group[-1]["close"] - group[0]["open"]
        if len(intervals) >= 24 and group[0]["index"] >= 250 and START <= start and start + HOUR <= END:
            scale = median(intervals[-24:])
            if scale > 0:
                scored_hours += 1
                if span > 0 and abs(move) >= Decimal("1.5") * scale and abs(move) >= Decimal("0.6") * span:
                    side = 1 if move > 0 else -1
                    actual = sorted(fills[start, side])
                    delay = (actual[0][0] - start) / MINUTE if actual else None
                    rows.append({"hour_open_ms": start, "side": side, "fill_delay_minutes": delay,
                                 "timely": delay is not None and delay <= 30, "same_side_fills": actual})
        intervals.append(span)
    delays = [row["fill_delay_minutes"] for row in rows if row["fill_delay_minutes"] is not None]
    return {
        "eligible_hours": scored_hours, "significant_hours": len(rows),
        "same_side_fill_hours": len(delays), "timely_same_side_fill_hours": sum(r["timely"] for r in rows),
        "median_delay_minutes_if_covered": median(delays) if delays else None, "hourly_events": rows,
        "by_side": {
            label: {"significant_hours": sum(row["side"] == sign for row in rows),
                    "same_side_fill_hours": sum(row["side"] == sign and row["fill_delay_minutes"] is not None for row in rows),
                    "timely_same_side_fill_hours": sum(row["side"] == sign and row["timely"] for row in rows)}
            for label, sign in (("BUY", 1), ("SELL", -1))
        },
    }


def lane_metrics(run):
    bars, ledger, events, raw = checked_tape(run)
    scenarios = {"native_r": None, **cost_scenarios()}
    result = {}
    for name, scenario in scenarios.items():
        roster, marks = economics(bars, ledger, events, raw, scenario)
        result[name] = cohort(roster, marks, scenario)
    result["observed_fill_coverage_shared_by_scenarios"] = coverage(bars, raw["campaign_roster"])
    return result


def identify_changes(left, right, flat_epsilon):
    """Attribute outcome differences by ID, without inferring why a slot changed."""
    def by_id(lane):
        return {row["plan_id"]: row for row in lane["full_observed_campaign_roster"]}

    a, b = by_id(left), by_id(right)
    a_fills, b_fills = set(left["filled_plan_ids"]), set(right["filled_plan_ids"])
    changes = []
    for plan_id in sorted(a_fills | b_fills):
        old, new = a.get(plan_id), b.get(plan_id)
        old_net, new_net = (row.get("scenario_net") if row else None for row in (old, new))
        if plan_id not in a_fills:
            kind = "B_ONLY_OBSERVED_FILL"
        elif plan_id not in b_fills:
            kind = "A_ONLY_OBSERVED_FILL"
        elif (old["state"], old["entry_effective_open_ms"], old["entry_price"], old_net, old["events"]) != (
                new["state"], new["entry_effective_open_ms"], new["entry_price"], new_net, new["events"]):
            kind = "SHARED_PLAN_DIFFERENT_EXECUTION_OR_NET"
        else:
            continue
        new_wins = None if new_net is None else new_net > flat_epsilon
        old_wins = None if old_net is None else old_net > flat_epsilon
        changes.append({
            "plan_id": plan_id, "change": kind,
            "A": {"state": old["state"], "net": old_net, "win": old_wins,
                  "entry_ms": old["entry_effective_open_ms"]} if old else None,
            "B": {"state": new["state"], "net": new_net, "win": new_wins,
                  "entry_ms": new["entry_effective_open_ms"]} if new else None,
            "observed_same_plan_capacity_displacement_in_B": (
                kind == "A_ONLY_OBSERVED_FILL" and new is not None and new["state"] == "BLOCKED_CAPACITY"
            ),
        })
    return {
        "counts": dict(Counter(row["change"] for row in changes)), "plans": changes,
        "A_only_completed_wins": sum(row["change"] == "A_ONLY_OBSERVED_FILL" and row["A"]["win"] is True
                                    for row in changes),
        "B_only_completed_wins": sum(row["change"] == "B_ONLY_OBSERVED_FILL" and row["B"]["win"] is True
                                    for row in changes),
        "confirmed_capacity_displacement_in_B": sum(row["observed_same_plan_capacity_displacement_in_B"]
                                                     for row in changes),
        "scope": "Capacity displacement requires an observed B CAPACITY event under the same plan ID.",
    }


def report_metrics(left, right, comparison, *, depth_stability=None, known_case=None):
    """Pair a validator-approved native A/B. Unavailable evidence does not become a zero."""
    require(comparison.get("status") == "VALIDATED_MATCHED_AB", "Validated matched native A/B required")
    for prefix, run, expected_lane in (("left", left, "A"), ("right", right, "B")):
        actual = run.get("manifest_sha256")
        supplied = comparison.get(f"{prefix}_manifest_sha256")
        require(actual is not None and supplied == actual, f"{prefix} capture is not the validated native manifest")
        require(run["manifest"]["lane"] == expected_lane and run["manifest"]["frame_minutes"] == 15,
                f"{prefix} capture does not match registered M15 lane {expected_lane}")
        require(run["manifest"]["scoring_start_ms"] == START and run["manifest"]["fixed_end_close_ms"] == END,
                f"{prefix} capture lies outside registered outcome bounds")
    result_a, result_b = lane_metrics(left), lane_metrics(right)
    return {
        "status": "BOUNDED_NATIVE_AB_DIAGNOSTIC",
        "score_start_close_ms": START, "score_end_close_ms": END,
        "depth_stability": depth_stability, "prior_known_m15_open_sensitivity": known_case,
        "lanes": {"A": result_a, "B": result_b},
        "observed_plan_changes_by_scenario": {
            name: identify_changes(result_a[name], result_b[name], FLAT_R if name == "native_r" else FLAT_EPSILON)
            for name in ("native_r", "normal", "stress")
        },
        "economics": {
            "native_r": "Native RAW PAPER event R. Open marks add remaining midpoint liquidation using the frozen native modeled cost.",
            "normal_and_stress": "Post-fill sensitivity on frozen observed native event times, midpoint prices and allocations. "
                                 "Spread applied once via two side quotes, slippage on market/stop legs, and fees each side. "
                                 "TP remains the native scheduled event; its observed midpoint price receives half-spread only. "
                                 "Costs and fractional units do not change admissions, quotes passed to gates, occupancy or fills.",
            "normalization": "Exactly $250 modeled initial, non-gapped, stop risk per admitted plan per scenario. "
                             "Theoretical fractional XAU units use pointvalue 1 USD per one XAU unit per USD, "
                             "with no quantity-grid, account equity, funding, margin or actual execution claim. "
                             "Gaps can exceed the nominal risk budget.",
            "drawdown": "Sampled at native bar closes using settled cash and marked remaining open quantity, not intrabar drawdown.",
            "coverage": "Identical decisions and entries in the fixed-fill cost sensitivities. Timely means within 30 minutes "
                        "of a completed UTC significant move, with outcome labels used for evaluation only.",
        },
        "promotion": "NOT_ELIGIBLE_WITHOUT_FULL_INDEPENDENT_VALIDATION",
    }
