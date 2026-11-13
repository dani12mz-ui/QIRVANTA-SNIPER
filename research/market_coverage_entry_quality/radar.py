"""Track existing confirmed Shadow direction without connecting it to entry decisions."""

from collections import defaultdict
from itertools import groupby
from statistics import median

from gold_pro.direct_trend_breakout import CONSOLIDATION, RETEST


PRE_HOUR = 15 * 60_000
MINUTE = 60_000
HOUR = 60 * MINUTE
STATES = {
    "WATCH": "WATCH", "BREAKOUT CONFIRMED": "TREND CONFIRMED",
    "RETEST WATCH": "RETEST PENDING", "ENTRY CANDIDATE": "ENTRY CANDIDATE",
    "REJECTED": "REJECTED", "EXPIRED": "EXPIRED", "INVALIDATED": "INVALIDATED",
}


def key(row):
    return (row["side"], row.get("origin_model") or row["model"], row["source_ms"],
            row["level"], row["breakout_ms"])


def clock(row):
    return row["time_ms"], row["side"], row["model"]


def lifecycle(bars, events):
    """Return the linked retest state at each close and later confirmed setups."""
    if any(a["time_ms"] > b["time_ms"] for a, b in zip(events, events[1:])):
        raise ValueError("Shadow events are out of order")
    closes = {b["time_close"] for b in bars}
    if any(e["time_ms"] not in closes or e["level_known_ms"] > e["time_ms"] for e in events):
        raise ValueError("Shadow event outside its known-at archive")
    grouped = {stamp: list(rows) for stamp, rows in groupby(events, key=lambda row: row["time_ms"])}
    active, snapshots, marked, transitions = {}, {}, {}, []
    for bar in bars:
        stamp = bar["time_close"]
        for row in grouped.get(stamp, ()):
            state, model, side = row["state"], row["model"], row["side"]
            linked = active.get(side)
            previous = linked if linked is not None and linked["breakout_ms"] < stamp else None
            transition = {"time_ms": stamp, "side": side, "source": model, "state": STATES[state],
                          "reason": row["reason"], "identity": key(row), "level_known_ms": row["level_known_ms"]}
            if state == "BREAKOUT CONFIRMED" and previous is not None:
                if model == RETEST and key(row) == key(previous):
                    marked[clock(row)] = {"kind": RETEST, "origin": key(previous)}
                elif model == CONSOLIDATION:
                    marked[clock(row)] = {"kind": CONSOLIDATION, "origin": key(previous)}
            if model == RETEST:
                if state == "RETEST WATCH":
                    if linked is not None or row["breakout_ms"] != stamp:
                        raise ValueError("Unlinked or overlapping Shadow retest")
                    active[side] = row
                elif state in {"BREAKOUT CONFIRMED", "INVALIDATED", "EXPIRED"}:
                    if linked is None or key(row) != key(linked):
                        raise ValueError("Retest terminal does not match the active origin")
                    del active[side]
            transitions.append(transition)
        snapshots[stamp] = {s: key(row) for s, row in active.items()}
    return snapshots, marked, transitions


def warning_clocks(early, shadow, begin, end):
    """Deduplicate genuine observations by close and side, keeping source names."""
    clocks = defaultdict(set)
    for row in early:
        if begin <= row["known_ms"] <= end:
            clocks[row["known_ms"], row["side"]].add("RC2 EARLY")
    for row in shadow:
        if begin <= row["time_ms"] <= end and row["state"] in {"WATCH", "BREAKOUT CONFIRMED"}:
            clocks[row["time_ms"], row["side"]].add(f"{row['model']} · {row['state']}")
    return [{"known_ms": stamp, "side": side, "sources": sorted(sources)}
            for (stamp, side), sources in sorted(clocks.items())]


def detection(warnings, hours, categories):
    """Audit clock coverage and unmatched warnings on all eligible hours."""
    directional = {(h["start_ms"], h["side"]) for h in hours}
    matches = {}
    classes = defaultdict(int)
    assignment = defaultdict(list)
    for row in warnings:
        stamp, side = row["known_ms"], row["side"]
        start = stamp // HOUR * HOUR
        if (start, side) in directional:
            assignment[start, side].append(row)
        elif (start + HOUR, side) in directional and start + HOUR - stamp <= PRE_HOUR:
            assignment[start + HOUR, side].append(row)
        else:
            classes[categories.get(start, "INELIGIBLE_OR_PARTIAL")] += 1

    stats = defaultdict(int)
    first_latency, in_hour_latency = [], []
    for start, side in sorted(directional):
        clocks = assignment.get((start, side), [])
        before = next((r for r in clocks if r["known_ms"] < start), None)
        within = next((r for r in clocks if r["known_ms"] >= start), None)
        first = before or within
        delay = (first["known_ms"] - start) / MINUTE if first else None
        in_hour_delay = (within["known_ms"] - start) / MINUTE if within else None
        label = ("MISSED" if first is None else "PRE_HOUR" if delay < 0
                 else "FIRST_15_MINUTES" if delay <= 15 else "LATE_IN_HOUR")
        stats[label] += 1
        if first:
            first_latency.append(delay)
        if within:
            in_hour_latency.append(in_hour_delay)
        matches[start] = {"classification": label, "first": first, "lead_minutes": delay,
                          "first_pre_hour": before, "first_in_hour": within,
                          "new_clocks_in_hour": sum(r["known_ms"] >= start for r in clocks),
                          "assigned_clocks": len(clocks)}
    summary = {
        "new_clocks": len(warnings), "matched_hours": len(first_latency),
        "in_hour": len(in_hour_latency), "first_classification": dict(stats),
        "first_known_latency_median_min": median(first_latency) if first_latency else None,
        "in_hour_latency_median_min": median(in_hour_latency) if in_hour_latency else None,
        "outside_same_side_windows": dict(classes),
        "outside_eligible_same_side_windows": sum(n for key_, n in classes.items()
                                                  if key_ != "INELIGIBLE_OR_PARTIAL"),
        "repeated_assigned_clocks": sum(max(0, len(rows) - 1) for rows in assignment.values()),
    }
    return matches, summary
