"""Compare causal state and order journals from two validated native Pine exports."""

from collections import Counter, defaultdict
from decimal import Decimal
from itertools import pairwise

from research.core_htf_readiness import compare_native as native
from research.entry_decision_audit.native_qa import bar_row, side_row


EMA_TOLERANCE = Decimal("0.001")
LEDGER_TOLERANCE = Decimal("0.00000002")
EMA_FIELDS = ("ema50", "ema50_prev")
CORE_FIELDS = (
    "core_frame", *native.HTF_NUMERIC, *native.LOCAL_NUMERIC, "ready_HTF", "warmup", "core_trend",
)
CARRY_FIELDS = (
    "raw_phase", "raw_plan_id", "last_buy_close_ms", "last_sell_close_ms", "global_paused",
    "family_paused", "filtered_day_net", "filtered_day_streak",
)
BAR_FILTER_FIELDS = (
    "h1_closed_ms", "h1_direction", "h4_closed_ms", "h4_direction", "gap", "atr_pass",
    "bar_range", "max_entry_range_atr", "date_pass", "news_paused", "session_pass",
    "selected_id", "qty_ok", "rc2_filter_active", "rc2_blocked", "previous_close",
)
SIDE_FILTER_FIELDS = ("raw", "reason", "signed_body", "atr", "directional_close_fraction", "cooldown_pass", "range20_edge")
CANDIDATE_FIELDS = ("phase", "born_ms", "anchor", "level")
EVENT_CLOCK = {"CORE": "known_time", "READY": "confirmed_ms", "ALERT": "confirmed_ms", "RAW": "known_time"}
COUNTERS = {"entered": "SIM_ENTRY", "closed": "CLOSED", "capacity_skipped": "CAPACITY"}
LEDGER_MONEY = {
    "gross": "gross_r", "costs": "cost_r", "net": "net_r", "gains": "net_r", "losses": "net_r",
}
TERMINAL = {"CLOSED", "CANCELLED"}
RAW_METADATA = {"evidence", "filter_eligible", "quality", "quality_note", "countertrend", "local_aligned"}


def require(condition, detail):
    if not condition:
        raise ValueError(detail)


def selection(row, keys):
    return {key: row[key] for key in keys}


def diffs(left, right):
    changes = {}
    for key in sorted(left.keys() | right.keys()):
        if key not in left or key not in right or left[key] != right[key]:
            changes[key] = {
                "left": left.get(key), "right": right.get(key),
                "left_present": key in left, "right_present": key in right,
            }
    return changes


def as_json(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: as_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [as_json(item) for item in value]
    return value


def relative_fields(row, prefix):
    return {field: row[f"{prefix}_{field}"] for field in CANDIDATE_FIELDS}


def normalize_run(run):
    context, s5 = run["context"], run["s5"]
    market = run["market"]
    indices = {row["index"]: clock for clock, row in market.items()}
    clocks = list(market)
    previous = {current: past for past, current in pairwise(clocks)}
    ledger = {row["known_ms"]: row for row in s5["LEDGER"]}
    displayed = {row["known_ms"]: row for row in s5["STATE"]}
    snapshots = run["rows"]
    require(bool(snapshots) and tuple(snapshots) == tuple(context) == tuple(ledger),
            "The compared native lane needs a nonempty complete audited state window")
    events = {}
    for kind, clock_field in EVENT_CLOCK.items():
        by_close = defaultdict(list)
        seen = set()
        for row in s5[kind]:
            clock = row[clock_field]
            require(clock in snapshots and row["event_id"] not in seen,
                    f"{kind} has an out-of-window clock or duplicate ID")
            seen.add(row["event_id"])
            by_close[clock].append(row)
        events[kind] = by_close

    bares = {}
    for sent in native.tag_rows(run["logs"], "EDA_BAR"):
        row = bar_row("EDA_BAR|" + sent, run["frame"], context, displayed, ledger)
        clock = row["close_ms"]
        require(clock not in bares, "Duplicate EDA_BAR")
        bares[clock] = row
    require(tuple(bares) == tuple(snapshots), "The compared native lane is missing pre-bar EDA_BAR state")

    rows = {}
    for clock, core in snapshots.items():
        bar = bares[clock]
        records = {}
        for side, label in ((-1, "SELL"), (1, "BUY")):
            raw = "|".join(run["sides"][clock, side])
            sr = side_row("EDA_SIDE|" + raw, run["frame"], context)
            index = sr["last_accepted_index"]
            previous_accept = indices[index] if index is not None else None
            records[label] = {
                "before": {**relative_fields(sr, "before"), "last_accepted_ms": previous_accept},
                "after": {**relative_fields(sr, "after"),
                          "last_accepted_ms": core["last_buy_close_ms" if side == 1 else "last_sell_close_ms"]},
                "evaluation": selection(sr, SIDE_FILTER_FIELDS),
            }
        prior_clock = previous.get(clock)
        rows[clock] = {
            "prior_confirmed_chart_close_ms": prior_clock,
            "checkpoint_post_values_available_for_previous_close": prior_clock in snapshots,
            "prior_confirmed_chart_ohlc": native.candle_value(market[prior_clock]) if prior_clock is not None else None,
            "core": selection(core, CORE_FIELDS), "post_core_carry": selection(core, CARRY_FIELDS),
            "pre_raw": {"phase": bar["before_paper_phase"], "plan_id": bar["before_paper_plan_id"]},
            "post_raw": {"phase": bar["after_paper_phase"], "plan_id": bar["after_paper_plan_id"]},
            "decision": selection(bar, BAR_FILTER_FIELDS),
            "permission": selection(displayed[clock], ("permission", "permission_source", "permission_reason",
                                                        "plan_visible", "paper_capacity", "s5_stage")),
            "sides": records,
            "ledger": ledger[clock],
            "events": {kind: by_close.get(clock, []) for kind, by_close in events.items()},
        }
    return rows, clocks


def raw_state_checks(row, clock):
    pre = row["pre_raw"]
    phase, plan = pre["phase"], pre["plan_id"]
    errors = []
    for event in row["events"]["RAW"]:
        kind = event["event"]
        identity = event["plan_id"]
        allowed = None
        if kind == "QUEUED":
            allowed = phase == 0
            phase, plan = 1, identity
        elif kind == "SIM_ENTRY":
            allowed = phase == 1 and plan == identity
            phase, plan = 2, identity
        elif kind in TERMINAL:
            allowed = phase == (1 if kind == "CANCELLED" else 2) and plan == identity
            phase, plan = 0, ""
        elif kind == "CAPACITY":
            allowed = phase in (1, 2)
        elif kind.startswith("SIM_") or kind == "STOP_UPDATE":
            allowed = phase == 2 and plan == identity
        if allowed is False:
            errors.append({"close_ms": clock, "event_id": event["event_id"], "reason": "RAW_EVENT_PHASE_OR_PLAN_DRIFT"})
    if (phase, plan) != (row["post_raw"]["phase"], row["post_raw"]["plan_id"]):
        errors.append({"close_ms": clock, "reason": "RAW_EVENTS_DO_NOT_RECONCILE_TO_POST_PHASE_OR_PLAN"})
    return errors


def expected_ledger(events):
    counts = Counter(event["event"] for event in events)
    closed = [event for event in events if event["event"] == "CLOSED"]
    expected = {field: counts[kind] for field, kind in COUNTERS.items()}
    for field, event_field in LEDGER_MONEY.items():
        vals = [Decimal(str(event[event_field])) for event in closed]
        if field == "gains":
            vals = [val for val in vals if val > Decimal("1E-10")]
        elif field == "losses":
            vals = [-val for val in vals if val < Decimal("-1E-10")]
        expected[field] = sum(vals, Decimal(0))
    return expected


def reconcile_run(rows):
    residuals = []
    gaps = []
    increment_count = 0
    mismatches = []
    for clock, row in rows.items():
        mismatches.extend(raw_state_checks(row, clock))
        prior_clock = row["prior_confirmed_chart_close_ms"]
        prior = rows.get(prior_clock)
        if prior is None:
            gaps.append(clock)
            continue
        if row["pre_raw"] != prior["post_raw"]:
            mismatches.append({"close_ms": clock, "prior_close_ms": prior_clock, "reason": "RAW_PRE_POST_DISCONTINUITY"})
        for side, stage in row["sides"].items():
            if stage["before"] != prior["sides"][side]["after"]:
                mismatches.append({"close_ms": clock, "prior_close_ms": prior_clock, "side": side,
                                   "reason": "S5_FORMATION_PRE_POST_DISCONTINUITY"})
        amounts = expected_ledger(row["events"]["RAW"])
        actual = {
            field: row["ledger"][field] - prior["ledger"][field] if field in COUNTERS else
            Decimal(str(row["ledger"][field])) - Decimal(str(prior["ledger"][field]))
            for field in amounts
        }
        different = {
            field: {"expected": amounts[field], "observed": actual[field]}
            for field in amounts if abs(amounts[field] - actual[field]) >
            (0 if field in COUNTERS else LEDGER_TOLERANCE)
        }
        if different:
            residuals.append({"close_ms": clock, "prior_close_ms": prior_clock, "fields": different})
        increment_count += 1
    return {
        "status": "MISMATCH" if mismatches or residuals else "OBSERVED_TRANSITIONS_RECONCILED",
        "phase_or_id_or_formation_mismatches": mismatches,
        "ledger_increment_mismatches": residuals, "ledger_increment_checks": increment_count,
        "initial_or_gap_closes_without_prior_ledger": gaps,
        "raw_phase_checks": len(rows),
        "ledger_numeric_tolerance_R": LEDGER_TOLERANCE,
        "scope": "Counters and closed-campaign gross/cost/net/gains/losses. Initial ledger, current open P&L and drawdown are not reconstructed.",
    }


def event_delta(left, right):
    a = {row["event_id"]: row for row in left}
    b = {row["event_id"]: row for row in right}
    changed = []
    for identity in sorted(a.keys() & b.keys()):
        fields = diffs(a[identity], b[identity])
        if fields:
            changed.append({
                "event_id": identity, "fields": fields,
                "only_context_or_evidence_metadata": bool(fields) and fields.keys() <= RAW_METADATA,
            })
    missing = [row for row in left if row["event_id"] not in b]
    added = [row for row in right if row["event_id"] not in a]
    same_common_order = tuple(key for key in a if key in b) == tuple(key for key in b if key in a)
    if not missing and not added and not changed and same_common_order:
        return None
    return {"left_only": missing, "right_only": added, "changed": changed, "common_event_order_same": same_common_order}


def compare_close(left, right, stamp, ema_tolerance):
    record = {}
    prices = diffs(left["core"], right["core"])
    ema_outside = []
    for name in EMA_FIELDS:
        if name in prices:
            change = prices[name]
            a, b = change["left"], change["right"]
            change["absolute_delta"] = abs(a - b) if a is not None and b is not None else None
            if change["absolute_delta"] is None or change["absolute_delta"] > ema_tolerance:
                ema_outside.append(name)
    record["core"] = prices
    record["ema_outside_tolerance_or_missing"] = ema_outside
    record["pre_raw"] = diffs(left["pre_raw"], right["pre_raw"])
    record["post_raw"] = diffs(left["post_raw"], right["post_raw"])
    record["post_core_carry"] = diffs(left["post_core_carry"], right["post_core_carry"])
    record["decision"] = diffs(left["decision"], right["decision"])
    record["permission"] = diffs(left["permission"], right["permission"])
    record["raw_cumulative_ledger"] = diffs(left["ledger"], right["ledger"])
    record["sides"] = {
        side: {part: diffs(left["sides"][side][part], right["sides"][side][part])
               for part in ("before", "after", "evaluation")}
        for side in left["sides"]
    }
    record["events"] = {
        kind: delta for kind in EVENT_CLOCK
        if (delta := event_delta(left["events"][kind], right["events"][kind])) is not None
    }
    record["known_utc"] = native.utc(stamp)
    record["left_prior_confirmed_chart_close_ms"] = left["prior_confirmed_chart_close_ms"]
    record["right_prior_confirmed_chart_close_ms"] = right["prior_confirmed_chart_close_ms"]
    record["both_prior_post_snapshots_observed"] = (
        left["checkpoint_post_values_available_for_previous_close"] and
        right["checkpoint_post_values_available_for_previous_close"]
    )
    return record


def first_matching(rows, predicate):
    return next((clock for clock, row in rows.items() if predicate(row)), None)


def changed_side(row, part):
    return any(side[part] for side in row["sides"].values())


def actual_order_change(row):
    for kind in ("READY", "RAW"):
        events = row["events"].get(kind)
        if events and (events["left_only"] or events["right_only"] or not events["common_event_order_same"]
                       or any(not change["only_context_or_evidence_metadata"] for change in events["changed"])):
            return True
    return False


def first_difference_times(rows):
    tests = {
        "any_core_numeric_or_readiness_or_direction": lambda r: r["core"],
        "ema_outside_tolerance_or_missing": lambda r: r["ema_outside_tolerance_or_missing"],
        "selected_htf_ready_or_warmup": lambda r: {"ready_HTF", "warmup"} & r["core"].keys(),
        "core_trend": lambda r: "core_trend" in r["core"],
        "pre_raw_or_formation_or_cooldown": lambda r: r["pre_raw"] or changed_side(r, "before"),
        "post_carried_state": lambda r: r["post_core_carry"] or r["post_raw"] or changed_side(r, "after"),
        "post_shield_pause_flags": lambda r: {"global_paused", "family_paused"} & r["post_core_carry"].keys(),
        "formation_evaluation": lambda r: changed_side(r, "evaluation"),
        "raw_ledger_absolute_state": lambda r: r["raw_cumulative_ledger"],
        "protected_event_or_payload": lambda r: r["events"],
        "protected_execution_or_ready": actual_order_change,
    }
    tests.update({f"stream_{kind}": lambda r, event=kind: event in r["events"] for kind in EVENT_CLOCK})
    return {
        label: {"close_ms": stamp, "utc": native.utc(stamp)} if stamp is not None else None
        for label, predicate in tests.items()
        for stamp in (first_matching(rows, predicate),)
    }


def evidence_boundary(left, right, rows, firsts):
    first = next(iter(rows))
    first_row = rows[first]
    available_before = {
        "left": {"raw": left[first]["pre_raw"], "formations": {k: s["before"] for k, s in left[first]["sides"].items()}},
        "right": {"raw": right[first]["pre_raw"], "formations": {k: s["before"] for k, s in right[first]["sides"].items()}},
    }
    predecessor_snapshots = {}
    for name, lane in (("left", left), ("right", right)):
        clock = lane[first]["prior_confirmed_chart_close_ms"]
        observed = lane.get(clock)
        predecessor_snapshots[name] = {
            "close_ms": clock, "utc": native.utc(clock) if clock is not None else None,
            "market_open_close_ohlc": lane[first]["prior_confirmed_chart_ohlc"],
            "postbar": {
                "core": observed["core"], "core_carry": observed["post_core_carry"],
                "raw_ledger": observed["ledger"],
                "formations": {side: row["after"] for side, row in observed["sides"].items()},
            } if observed else None,
        }
    common_start = {
        name: {"core": lane[first]["core"], "carry": lane[first]["post_core_carry"],
               "raw_ledger": lane[first]["ledger"]} for name, lane in (("left", left), ("right", right))
    }
    first_event = firsts["protected_event_or_payload"]
    event_observation = None
    if first_event is not None:
        at = first_event["close_ms"]
        current = rows[at]
        if current["core"].keys() & {"ready_HTF", "warmup"} and changed_side(current, "evaluation"):
            explanation = "READINESS_OR_WARMUP_DIFFERS_AT_OBSERVED_EVENT"
        elif {"global_paused", "family_paused"} & current["post_core_carry"].keys() and not actual_order_change(current):
            explanation = "CARRY_AND_METADATA_DIFFER_WITHOUT_READY_OR_RAW_ECONOMIC_EVENT_CHANGE"
        else:
            explanation = "CAUSAL_ISOLATION_UNAVAILABLE"
        event_observation = {
            "at_ms": at, "utc": native.utc(at),
            "observation": explanation, "isolates_ema_from_carry": False,
            "availability_and_warmup_differ": bool(current["core"].keys() & {"ready_HTF", "warmup"}),
            "formation_predicates_differ": changed_side(current, "evaluation"),
            "post_core_carry_differs": bool(current["post_core_carry"]),
            "direct_observed_core_readiness_delta": {
                key: current["core"][key] for key in ("ready_HTF", "warmup", "core_trend") if key in current["core"]
            },
            "direct_observed_shield_delta": {
                key: current["post_core_carry"][key] for key in ("global_paused", "family_paused")
                if key in current["post_core_carry"]
            },
            "protective_payload_only_or_no_ready_execution_difference": not actual_order_change(current),
        }
    return {
        "first_common_confirmed_close_ms": first, "first_common_confirmed_close_utc": native.utc(first),
        "earliest_prebar_carry_observed_in_EDA": available_before,
        "earliest_postbar_core_and_raw_ledger": common_start,
        "prior_chart_closes": [first_row["left_prior_confirmed_chart_close_ms"], first_row["right_prior_confirmed_chart_close_ms"]],
        "preceding_confirmed_chart_rows": predecessor_snapshots,
        "complete_pre_first_close_checkpoint": False,
        "first_event_context": event_observation,
        "unlogged_checkpoint_fields": [
            "Selected HTF source clocks, seed and exact initial EMA state.",
            "Complete per-book position risk, stop, partials, timers, fills and all ledger state.",
            "EvidenceGate pausedAtCount and closed-campaign counters for every gate-dependent book.",
            "Full prior history of timestamped RAW/formations/shields outside the exported decision window.",
        ],
        "causal_limit": "Pre-bar RAW phase/plan and formation/cooldown are observed. CORE context and shield flags are post-bar snapshots. Their co-occurrence does not prove that one caused the other.",
    }


def compare_state(left, right, ema_tolerance=EMA_TOLERANCE):
    """Return JSON-ready causal state diffs keyed by confirmed chart close in UTC milliseconds."""
    tolerance = Decimal(str(ema_tolerance))
    require(tolerance.is_finite() and tolerance >= 0, "EMA tolerance must be finite and nonnegative")
    require(left["frame"] == right["frame"], "Cannot compare two native chart frames")
    a, a_history = normalize_run(left)
    b, b_history = normalize_run(right)
    common = sorted(a.keys() & b.keys())
    require(bool(common), "No common audited closes")
    shared_market = sorted(left["market"].keys() & right["market"].keys())
    candle_conflicts = [clock for clock in shared_market
                        if native.candle_value(left["market"][clock]) != native.candle_value(right["market"][clock])]
    same_history = a_history == b_history and not candle_conflicts
    common_history_contains_shallower = shared_market == min((a_history, b_history), key=len)
    rows = {clock: compare_close(a[clock], b[clock], clock, tolerance) for clock in common}
    firsts = first_difference_times(rows)
    reconciliation = {"left": reconcile_run(a), "right": reconcile_run(b)}
    comparable = (not candle_conflicts and common_history_contains_shallower
                  and tuple(a) == tuple(b) and a_history[-1] == b_history[-1])
    changes = any(
        row["core"] or row["pre_raw"] or row["post_raw"] or row["post_core_carry"] or row["decision"]
        or row["permission"] or row["raw_cumulative_ledger"] or row["events"]
        or any(any(side.values()) for side in row["sides"].values()) for row in rows.values()
    )
    if not comparable:
        status = "NONCOMPARABLE_CHART_HISTORY_OR_AUDIT_COVERAGE"
    elif any(lane["status"] == "MISMATCH" for lane in reconciliation.values()):
        status = "STATE_OR_RAW_RECONCILIATION_MISMATCH"
    elif changes:
        status = "OBSERVED_STATE_OR_PAYLOAD_DIVERGENCE"
    elif same_history:
        status = "SAME_HISTORY_OBSERVED_FIELDS_EXACT"
    else:
        status = "DIFFERENT_HISTORY_OBSERVED_FIELDS_EXACT"
    return as_json({
        "schema": "qirvanta.control_state.native_comparison.v1",
        "status": status, "frame": left["frame"], "ema_tolerance": tolerance,
        "coverage": {
            "closes_compared": len(common), "left_only_closes_ms": sorted(a.keys() - b.keys()),
            "right_only_closes_ms": sorted(b.keys() - a.keys()), "same_complete_loaded_history": same_history,
            "common_chart_ohlc_conflict_closes_ms": candle_conflicts,
            "common_chart_bars": len(shared_market),
            "common_history_contains_complete_shallower_capture": common_history_contains_shallower,
            "same_historical_end": a_history[-1] == b_history[-1],
            "left_confirmed_bars": len(a_history), "right_confirmed_bars": len(b_history),
            "initial_or_gap_closes_without_both_prior_audited_post_snapshots": [
                clock for clock, row in rows.items() if not row["both_prior_post_snapshots_observed"]
            ],
        },
        "first_differences": firsts, "initial_checkpoint_evidence": evidence_boundary(a, b, rows, firsts),
        "raw_reconciliation": reconciliation, "by_close": {str(clock): row for clock, row in rows.items()},
        "limits": [
            "Exact repeatability is restricted to exported fields and closes. No restored Pine checkpoint is claimed.",
            "Only directly observed CORE/READY/alert payload and RAW PAPER are audited, with no server or broker delivery inference.",
            "Ledger counters and net changes reconcile from prior logged closes; inherited absolute totals can legitimately differ with earlier history.",
            "Displayed H1/H4 context is advisory and does not replace the selected core tuple.",
        ],
    })

