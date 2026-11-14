"""Reconcile bounded Visual V2 RAW PAPER campaigns from genuine native events."""

from collections import Counter, defaultdict
from decimal import Decimal

from gold_pro.rc1_qa import epoch, json_number, require
from research.net_campaign_evidence.metrics import marked_drawdown


FLAT_EPSILON_R = Decimal("0.00000001")
SERIALIZATION_TOLERANCE = Decimal("0.00000005")
PARTS = {"SIM_TP1", "SIM_TP2", "SIM_TP3", "SIM_SL", "SIM_TIME"}
LIFECYCLE = PARTS | {"QUEUED", "CAPACITY", "SIM_ENTRY", "CANCELLED", "CLOSED",
                     "AMBIGUOUS", "TIME_PENDING", "STOP_UPDATE"}
AMOUNTS = ("gross_r", "cost_r", "net_r")
GEOMETRY = ("reference", "initial_sl", "tp1", "tp2", "tp3", "roundtrip_cost_price")


def _amounts(row):
    amounts = {field: json_number(row[field], field) for field in AMOUNTS}
    require(abs(amounts["gross_r"] - amounts["cost_r"] - amounts["net_r"]) <= SERIALIZATION_TOLERANCE,
            f'{row["event_id"]}: native gross minus cost does not reconcile to net R')
    return amounts


def _check_fraction(row, full=False):
    fraction = json_number(row["quantity_fraction"], "native quantity fraction")
    require(0 < fraction <= 1 and (not full or fraction == 1),
            f'{row["event_id"]}: invalid native quantity fraction')
    return fraction


def _record(event):
    return {key: event.get(key) for key in (
        "event_id", "event", "known_time", "effective_time", "price", "reason",
        "quantity_fraction", *AMOUNTS,
    )}


def _one(by_kind, kind, plan_id):
    events = by_kind.get(kind, ())
    require(len(events) <= 1, f"{plan_id}: multiple native {kind} events")
    return events[0] if events else None


def _campaign(plan_id, events, terminal):
    by_kind = defaultdict(list)
    for row in events:
        by_kind[row["event"]].append(row)
    queued, entry, cancelled, closed, capacity = (
        _one(by_kind, kind, plan_id) for kind in ("QUEUED", "SIM_ENTRY", "CANCELLED", "CLOSED", "CAPACITY")
    )
    require(not (cancelled and (entry or closed)) and not
            (capacity and (queued or entry or cancelled or closed or len(events) != 1)),
            f"{plan_id}: contradictory native campaign outcomes")
    parts = [row for row in events if row["event"] in PARTS]
    total_fraction = sum((_check_fraction(row) for row in parts), Decimal(0))
    require(total_fraction <= Decimal(1) + SERIALIZATION_TOLERANCE,
            f"{plan_id}: native partial exits exceed the original position")
    known_part_totals = {
        key: sum((amount[key] for amount in map(_amounts, parts)), Decimal(0)) for key in AMOUNTS
    }
    require(not (cancelled or capacity) or all(row["event"] in {"QUEUED", "CANCELLED", "CAPACITY"} for row in events),
            f"{plan_id}: unfilled plan has position events")
    if cancelled and queued:
        require(queued["known_time"] < cancelled["known_time"],
                f"{plan_id}: cancellation known before a queued close")
    geometry = {key: events[0].get(key) for key in GEOMETRY}
    side = events[0]["side"]
    require(side in (-1, 1) and all(row["side"] == side and
            all(row.get(key) == val for key, val in geometry.items()) for row in events),
            f"{plan_id}: direction or original plan geometry changed within campaign")
    if entry:
        _check_fraction(entry, full=True)
        require(all(row["known_time"] >= entry["known_time"] for row in parts) and
                (closed is None or closed["known_time"] >= entry["known_time"]) and
                (queued is None or queued["known_time"] < entry["known_time"] and
                 queued["known_time"] <= entry["effective_time"]),
                f"{plan_id}: fill/exit sequence precedes entry or queued close")
    fully_witnessed = bool(entry and closed)
    close_totals = None
    if closed:
        _check_fraction(closed, full=True)
        close_totals = _amounts(closed)
        require(all(row["known_time"] <= closed["known_time"] for row in parts),
                f"{plan_id}: exit part is later than its aggregate CLOSED event")
        if fully_witnessed:
            require(parts and abs(total_fraction - Decimal(1)) <= SERIALIZATION_TOLERANCE and
                    all(abs(close_totals[key] - known_part_totals[key]) <= SERIALIZATION_TOLERANCE for key in AMOUNTS),
                    f"{plan_id}: complete native campaign parts do not match its CLOSED event")
    active_at_end = terminal["plan_id"] == plan_id and terminal["phase"] in (1, 2)
    if closed:
        state = "COMPLETE_IN_WINDOW" if fully_witnessed else "CARRY_IN_CLOSED_IN_WINDOW"
    elif cancelled:
        state = "CANCELLED_ZERO_FILL" if queued else "CARRY_IN_CANCELLED_ZERO_FILL"
    elif capacity:
        state = "BLOCKED_CAPACITY"
    else:
        require(active_at_end and (not entry or terminal["phase"] == 2),
                f"{plan_id}: unresolved campaign lacks last-ledger occupancy witness")
        state = "OPEN_AT_WINDOW_END" if terminal["phase"] == 2 else "PENDING_AT_WINDOW_END"
    require(not active_at_end or not (closed or cancelled or capacity),
            f"{plan_id}: completed/cancelled plan recorded as active at end")
    return {
        "plan_id": plan_id, "side": side, "state": state,
        "origin": "QUEUE_OBSERVED_IN_WINDOW" if queued else "CAPACITY" if capacity else "QUEUE_BEFORE_OR_OUTSIDE_WINDOW",
        "entry_origin": "WITNESSED_IN_WINDOW" if entry else "UNFILLED" if cancelled or capacity or
        terminal["phase"] == 1 and active_at_end else "BEFORE_WINDOW_OR_UNAVAILABLE",
        "plan_geometry": geometry,
        "queued_close_ms": queued["known_time"] if queued else None,
        "entry_known_close_ms": entry["known_time"] if entry else None,
        "entry_effective_open_ms": entry["effective_time"] if entry else None,
        "entry_price": entry["price"] if entry else None,
        "close_known_ms": closed["known_time"] if closed else None,
        "close_summary_r": close_totals,
        "reported_part_fraction": total_fraction,
        "reported_part_totals_r": known_part_totals,
        "exit_parts_reconciled": "MATCH" if fully_witnessed else "NOT_FULLY_WITNESSED" if closed else "NO_TERMINAL_CLOSE",
        "cuts_at_last_ledger": active_at_end,
        "remaining_fraction_at_last_ledger": max(Decimal(0), Decimal(1) - total_fraction)
        if active_at_end and entry else None,
        "events": [_record(event) for event in events],
    }


def _metrics(campaigns):
    returns = [row["close_summary_r"]["net_r"] for row in campaigns]
    wins = [value for value in returns if value > FLAT_EPSILON_R]
    losses = [value for value in returns if value < -FLAT_EPSILON_R]
    flats = len(returns) - len(wins) - len(losses)
    settled = Decimal(0)
    marks = []
    for row, value in zip(campaigns, returns):
        settled += value
        marks.append((row["close_known_ms"], settled))
    return {
        "closed": len(returns), "wins": len(wins), "losses": len(losses), "flats": flats,
        "win_rate": len(wins) / len(returns) if returns else None,
        "loss_rate": len(losses) / len(returns) if returns else None,
        "flat_rate": flats / len(returns) if returns else None,
        "net_r": settled,
        "gross_r": sum((row["close_summary_r"]["gross_r"] for row in campaigns), Decimal(0)),
        "cost_r": sum((row["close_summary_r"]["cost_r"] for row in campaigns), Decimal(0)),
        "expectancy_r": settled / len(returns) if returns else None,
        "average_win_r": sum(wins) / len(wins) if wins else None,
        "average_loss_r": sum(losses) / len(losses) if losses else None,
        "worst_loss_r": min(losses, default=None),
        "profit_factor_r": sum(wins) / -sum(losses) if losses else None,
        "realized_campaign_close_dd_r": marked_drawdown(marks)["max_drawdown"],
        "drawdown_scope": "CAMPAIGN_NET_AT_OBSERVED_CLOSES_FROM_ZERO_WITHOUT_CARRY_IN_OR_INTRABAR_EQUITY",
    }


def _reconcile_ledger(ledger, events):
    """The first snapshot already contains the first close's events."""
    by_close = defaultdict(list)
    for row in events:
        by_close[row["known_time"]].append(row)
    for previous, current in zip(ledger, ledger[1:]):
        changes = by_close[current["known_ms"]]
        kinds = Counter(row["event"] for row in changes)
        for field, tag in (("entered", "SIM_ENTRY"), ("closed", "CLOSED"), ("capacity_skipped", "CAPACITY")):
            require(current[field] - previous[field] == kinds[tag],
                    f'{current["known_ms"]}: native {field} change lacks matching event(s)')
        totals = [_amounts(row) for row in changes if row["event"] == "CLOSED"]
        for field, key in (("gross", "gross_r"), ("costs", "cost_r"), ("net", "net_r")):
            require(abs(current[field] - previous[field] - sum(t[key] for t in totals)) <= SERIALIZATION_TOLERANCE,
                    f'{current["known_ms"]}: native {field} differs from aggregate CLOSED evidence')


def summarize_raw(run):
    """Return separate complete, carrying, pending and blocked native outcomes.

    Input is the validated capture returned by core_htf_readiness.compare_native.load_run.
    The RAW book is the Pine indicator's simulator. Its log denominates costs and
    realized results in initial-risk units and contains no broker execution or USD
    size. TradingView strategy report and independent quote-side replay are separate.
    """
    events, ledger = run["s5"]["RAW"], run["s5"]["LEDGER"]
    window = tuple(run["context"])
    require(window and tuple(row["known_ms"] for row in ledger) == window,
            "Native RAW reconciliation requires full matching ledger clocks")
    ids = set()
    grouped = defaultdict(list)
    conflicts = []
    previous = window[0]
    for row in events:
        stamp = epoch(row["known_time"], "native RAW close")
        require(stamp in run["context"] and stamp >= previous and row["book"] == "RAW" and
                row["event_id"] not in ids,
                "Native RAW event is outside the confirmed journal window, out of order or duplicated")
        ids.add(row["event_id"])
        previous = stamp
        if row["event"] == "CONFLICT":
            conflicts.append(_record(row))
            continue
        plan_id = row["plan_id"]
        require(plan_id and row["event"] in LIFECYCLE, f"Unknown native RAW event: {row['event']!r}")
        grouped[plan_id].append(row)
    _reconcile_ledger(ledger, events)
    require(all(row["phase"] in (0, 1, 2) and bool(row["plan_id"]) == bool(row["phase"]) for row in ledger),
            "Native RAW displayed position or pending phase lacks identity")
    require(all(row["side"] in (-1, 1) for parts in grouped.values() for row in parts),
            "Native RAW plan direction missing")

    terminal = ledger[-1]
    roster = [_campaign(plan, rows, terminal) for plan, rows in grouped.items()]
    for record in roster:
        entry_at = record["entry_known_close_ms"]
        if entry_at is not None:
            require(record["entry_effective_open_ms"] == run["context"][entry_at].opening,
                    f'{record["plan_id"]}: native entry is not effective at the corresponding bar open')
    full = sorted((row for row in roster if row["state"] == "COMPLETE_IN_WINDOW"),
                  key=lambda row: (row["close_known_ms"], row["plan_id"]))
    carries_closed = [row for row in roster if row["state"] == "CARRY_IN_CLOSED_IN_WINDOW"]
    start_active = {"phase": ledger[0]["phase"], "plan_id": ledger[0]["plan_id"]}
    end_active = {"phase": terminal["phase"], "plan_id": terminal["plan_id"]}
    if terminal["phase"] and terminal["plan_id"] not in grouped:
        end_active["origin"] = "CARRIED_WITH_NO_EVENTS_IN_WINDOW"
        end_active["realized_parts_r"] = None
    return {
        "status": "BOUNDED_NATIVE_REALIZED_R",
        "audited_first_close_ms": window[0], "audited_last_close_ms": window[-1], "audited_closes": len(window),
        "flat_epsilon_r": format(FLAT_EPSILON_R, "f"),
        "metrics": _metrics(full),
        "metrics_by_side": {label: _metrics([row for row in full if row["side"] == side])
                            for label, side in (("BUY", 1), ("SELL", -1))},
        "event_counts": dict(Counter(row["event"] for row in events)),
        "roster_counts": dict(Counter(row["state"] for row in roster)),
        "campaign_roster": roster,
        "same_close_conflicts_without_plan": conflicts,
        "carry_in_closes_excluded_from_complete_cohort": len(carries_closed),
        "carry_in_closed_net_r_witness": sum((row["close_summary_r"]["net_r"] for row in carries_closed), Decimal(0)),
        "open_or_pending_at_first_close": start_active,
        "start_state_scope": "First ledger snapshot follows that bar's events. Pre-window position state is unavailable.",
        "open_or_pending_at_last_close": end_active,
        "ledger_first_after_events": ledger[0],
        "ledger_last_after_events": terminal,
        "interior_ledger_deltas_reconciled": len(ledger) - 1,
        "observed_roundtrip_cost_price_values": sorted({row["plan_geometry"]["roundtrip_cost_price"] for row in roster
                                                       if row["plan_geometry"]["roundtrip_cost_price"] is not None}),
        "marked_equity": {"status": "UNAVAILABLE", "reason": "The native export has no open mark-to-liquidation equity series."},
        "cash_and_stress": {"status": "UNAVAILABLE", "reason": "Native RAW PAPER quantities are fractions and costs are modeled in R."},
        "uncertainty": "One bounded native audit window with carried state. No independent performance inference.",
    }

