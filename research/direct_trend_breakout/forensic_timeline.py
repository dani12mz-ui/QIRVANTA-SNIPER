"""Join the two pinned OANDA:XAUUSD journals into the November 11 timeline."""

from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
import io
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from gold_pro.rc1_qa import Context, read_export, require


ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "evidence"
PRIOR = "2a3463db4c8595bc049681d9922d8a927044b8f2"
BASE = "72e0e3cdcd0ae6c8b31e4abaff304f9c5ad309f0"
PRIOR_ARCHIVE = "research/wsb_missed_sell/evidence/native-logs.zip"
BASE_ARCHIVE = "research/rc2-pressure-meter/evidence/native-logs.zip"
SYMBOL = "OANDA:XAUUSD"
PRICES = ("open", "high", "low", "close")
BAR_FIELDS = ("open_ms", "close_ms", *PRICES, "volume")
DIRECTIONS = {-1: "DOWN", 0: "MIXED", 1: "UP"}
START = "2026-11-11T09:00:00Z"
END = "2026-11-11T16:30:00Z"


def utc(stamp):
    return datetime.fromtimestamp(stamp / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ms(value):
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)


def archive(commit, path):
    raw = subprocess.check_output(["git", "show", f"{commit}:{path}"], cwd=ROOT)
    return ZipFile(io.BytesIO(raw)), sha256(raw).hexdigest()


def records(source, name):
    content = source.read(name)
    with io.StringIO(content.decode("utf-8-sig"), newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        require(reader.fieldnames == ["Date", "Message"], f"{name}: Pine journal schema")
        rows = []
        for entry in reader:
            tag, _, payload = entry["Message"].partition("|")
            rows.append((tag, ms(entry["Date"]), payload))
    return rows


def decode(payload):
    return json.loads(payload, parse_float=Decimal)


def select_bars(rows, tag, frame):
    result = {}
    for kind, stamp, payload in rows:
        if kind != tag:
            continue
        bar = decode(payload)
        require(bar["symbol"] == SYMBOL and str(bar["timeframe"]) == str(frame), f"{tag}: symbol or timeframe")
        require(stamp == bar["open_ms"] < bar["close_ms"] <= stamp + frame * 60_000, f"{tag}: times")
        require(bar["close_ms"] not in result, f"{tag}: duplicate bar")
        if result:
            require(next(reversed(result)) <= stamp, f"{tag}: out-of-order bar")
        result[bar["close_ms"]] = bar
    require(result, f"No {tag} bars")
    return result


def values(bar, fields):
    return tuple(bar[key] for key in fields)


def join_bars(old, new):
    shared = old.keys() & new.keys()
    require(shared, "Native captures have no shared bars")
    for stamp in shared:
        require(values(old[stamp], BAR_FIELDS) == values(new[stamp], BAR_FIELDS),
                f"Native OHLCV disagreement at {utc(stamp)}")
    return dict(sorted((old | new).items())), len(shared)


def aggregate_m5(bar, m5):
    opens = [bar["open_ms"] + index * 300_000 for index in range(3)]
    members = [m5.get(opened + 300_000) for opened in opens]
    if any(part is None or part["open_ms"] != opened for part, opened in zip(members, opens)):
        return None
    return (members[0]["open"], max(x["high"] for x in members), min(x["low"] for x in members),
            members[-1]["close"], sum(x["volume"] for x in members))


def event_rows(export):
    result = defaultdict(list)
    for stream, rows, timestamp in (("CORE", export.core, "known_time"), ("READY", export.ready, "confirmed_ms"),
                                    ("NATIVE", export.native, "known_time")):
        for row in rows:
            result[row[timestamp]].append({
                "stream": stream,
                "side": row["direction"] if "direction" in row else ("BUY" if row["side"] == 1 else "SELL"),
                "state": row["state"] if "state" in row else row["event"],
                "reason": row["reason"],
                "price": row.get("price", row.get("entry")),
                "effective_utc": utc(row["effective_time"]) if row.get("effective_time") else None,
                "cash_pnl": row.get("cash_pnl"),
                "id": row["event_id"],
            })
    return result


def decisions(events):
    return [row for row in events if row["stream"] != "NATIVE"]


def alert_rows(records):
    result = defaultdict(list)
    for tag, _, payload in records:
        if tag == "RC2_ALERT":
            for alert in decode(payload)["events"]:
                result[alert["confirmed_ms"]].append(alert)
    return result


def previous_range(bars, index, length):
    if index < length:
        return None, None
    prior = bars[index - length:index]
    return max(bar["high"] for bar in prior), min(bar["low"] for bar in prior)


def ema(price, previous, length):
    return price if previous is None else previous + (price - previous) * 2 / (length + 1)


def swing(bars, index, field, width=3):
    """Strict symmetric local extremum, observed at the last right-hand bar close."""
    if index < 2 * width:
        return None
    center = index - width
    candidate = bars[center]
    side = 1 if field == "high" else -1
    price = candidate[field]
    if any(side * (price - bars[j][field]) <= 0
           for j in range(center - width, index + 1) if j != center):
        return None
    return {"price": price, "source_utc": utc(candidate["open_ms"]),
            "known_utc": utc(bars[index]["close_ms"])}


def calculations(joined):
    bars = list(joined.values())
    seed, ema21, ema55, atr = [], None, None, None
    two_back_ema21 = []
    previous_side = 0
    prior_high = prior_low = prior_fast_high = prior_fast_low = None
    downside_edge = upside_edge = None
    result = {}
    for index, bar in enumerate(bars):
        opened, closed, o, h, low, c = values(bar, ("open_ms", "close_ms", *PRICES))
        prior = bars[index - 1] if index else None
        previous_close = prior["close"] if prior else None
        prior_atr = atr
        tr = max(h - low, abs(h - previous_close), abs(low - previous_close)) if prior else h - low
        if atr is None:
            seed.append(tr)
            if len(seed) == 14:
                atr = sum(seed) / 14
        else:
            atr = (13 * atr + tr) / 14
        ema21, ema55 = ema(c, ema21, 21), ema(c, ema55, 55)
        local = 0
        if len(two_back_ema21) == 2:
            if c < ema21 < ema55 and ema21 <= two_back_ema21[0]:
                local = -1
            elif c > ema21 > ema55 and ema21 >= two_back_ema21[0]:
                local = 1
        two_back_ema21 = (two_back_ema21 + [ema21])[-2:]
        hi4, lo4 = previous_range(bars, index, 4)
        hi12, lo12 = previous_range(bars, index, 12)
        hi20, lo20 = previous_range(bars, index, 20)
        size = h - low
        early = 0
        if prior_atr is not None and hi12 is not None and size >= Decimal("1.15") * prior_atr:
            if c < lo12 and o - c >= Decimal("0.50") * prior_atr:
                early = -1
            elif c > hi12 and c - o >= Decimal("0.50") * prior_atr:
                early = 1
        early_new = early if early != previous_side and prior and opened == prior["close_ms"] else 0
        previous_side = early
        new_high = swing(bars, index, "high")
        new_low = swing(bars, index, "low")
        lower_high = new_high and prior_high and new_high["price"] < prior_high["price"]
        lower_low = new_low and prior_low and new_low["price"] < prior_low["price"]
        if new_high:
            prior_high = new_high
        if new_low:
            prior_low = new_low
        new_fast_high = swing(bars, index, "high", 2)
        new_fast_low = swing(bars, index, "low", 2)
        fast_lower_high = new_fast_high and prior_fast_high and new_fast_high["price"] < prior_fast_high["price"]
        fast_lower_low = new_fast_low and prior_fast_low and new_fast_low["price"] < prior_fast_low["price"]
        if new_fast_high:
            prior_fast_high = new_fast_high
        if new_fast_low:
            prior_fast_low = new_fast_low
        down_cross = lo20 is not None and c < lo20 <= previous_close
        up_cross = hi20 is not None and c > hi20 >= previous_close
        reclaim = downside_edge is not None and c > downside_edge >= previous_close
        retrace = upside_edge is not None and c < upside_edge <= previous_close
        if down_cross or reclaim:
            downside_edge = lo20 if down_cross else None
        if up_cross or retrace:
            upside_edge = hi20 if up_cross else None
        result[closed] = {
            "m15_direction_reconstructed": DIRECTIONS[local],
            "ema21_reconstructed": ema21, "ema55_reconstructed": ema55,
            "atr14_reconstructed": atr, "prior_atr14_reconstructed": prior_atr, "range": size,
            "bear_body": o - c, "body_over_prior_atr": (o - c) / prior_atr if prior_atr else None,
            "range_over_prior_atr": size / prior_atr if prior_atr else None,
            "prior4_high": hi4, "prior4_low": lo4, "prior12_high": hi12, "prior12_low": lo12,
            "prior20_high": hi20, "prior20_low": lo20,
            "close_cross_below_prior20": down_cross, "close_cross_above_prior20": up_cross,
            "wick_below_prior20_close_inside": lo20 is not None and low < lo20 <= c,
            "reclaimed_last_downside_edge": reclaim, "lost_last_upside_edge": retrace,
            "confirmed_swing_high": new_high, "confirmed_swing_low": new_low,
            "confirmed_lower_high": bool(lower_high), "confirmed_lower_low": bool(lower_low),
            "confirmed_fast_swing_high": new_fast_high, "confirmed_fast_swing_low": new_fast_low,
            "confirmed_fast_lower_high": bool(fast_lower_high), "confirmed_fast_lower_low": bool(fast_lower_low),
            "early_geometry_side": early, "early_if_enabled_new_side": early_new,
        }
    return result


def timeline(prior, base, old_hash, base_hash):
    captures = {key: records(prior, f"{key}_M15.csv") for key in ("s5", "rc2-indicator", "rc2-audit", "original")}
    feature = records(base, "rc2/feature_indicator_M15.csv")
    fresh = select_bars(feature, "PR_CTX", 15)
    old = select_bars(captures["original"], "WSB_ORIGINAL_CTX", 15)
    m15, shared = join_bars(old, fresh)
    m5, shared_m5 = join_bars(select_bars(records(prior, "original_M5.csv"), "WSB_ORIGINAL_CTX", 5),
                              select_bars(records(base, "rc2/feature_indicator_M5.csv"), "PR_CTX", 5))
    native = {key: select_bars(captures[key], "WSB_FORENSIC", 15)
              for key in ("s5", "rc2-indicator", "rc2-audit")}
    contexts = {ctx.closing: ctx for tag, _, value in feature if tag == "RC1_CTX"
                for ctx in (Context.parse(value, 15),)}
    all_decisions = calculations(m15)
    alert = alert_rows(feature)
    wsb = defaultdict(list)
    for tag, _, payload in captures["original"]:
        if tag == "WSB_ORIGINAL_PLAN":
            plan = decode(payload)
            wsb[plan["close_ms"]].append({key: plan[key] for key in ("direction", "entry")})
    with TemporaryDirectory() as scratch:
        exports = {}
        for key, source, name, role in (
            ("s5", prior, "s5_M15.csv", "audit"), ("rc2-audit", prior, "rc2-audit_M15.csv", "audit"),
            ("rc2-indicator", prior, "rc2-indicator_M15.csv", "indicator"),
            ("pressure", base, "rc2/feature_indicator_M15.csv", "indicator"),
        ):
            path = Path(scratch) / f"{key}.csv"
            path.write_bytes(source.read(name))
            exports[key] = read_export(path, 15, role)
    events = {key: event_rows(export) for key, export in exports.items()}
    require(native["s5"].keys() == native["rc2-indicator"].keys() == native["rc2-audit"].keys(),
            "Three WSB replay lanes must have identical forensic coverage")
    for stamp in native["s5"]:
        lanes = [series[stamp] for series in native.values()]
        first = lanes[0]["first_open_ms"]
        require(all(lane["first_open_ms"] == first == old[stamp]["first_open_ms"] for lane in lanes),
                f"Replay warmup mismatch at {utc(stamp)}")
        require(all(values(lane, BAR_FIELDS) == values(m15[stamp], BAR_FIELDS) for lane in lanes),
                f"Replay OHLCV mismatch at {utc(stamp)}")
        s5, ind, audit = lanes
        for key in ("sell_phase_before", "sell_phase_after", "sell_reason", "buy_reason", "candidate_count"):
            require(s5[key] == ind[key] == audit[key], f"{key}: baseline decision mismatch at {utc(stamp)}")
        context = all_decisions[stamp]
        require(bool(context["early_geometry_side"] == -1) == ind["early_bear_geometry"],
                f"Early geometry replay disagreement at {utc(stamp)}")
        require(values(context, ("prior20_high", "prior20_low")) == values(ind, ("prior20_high", "prior20_low")),
                f"Prior range disagreement at {utc(stamp)}")
        for archived, reconstructed in (("atr_rma14", "atr14_reconstructed"), ("ema21", "ema21_reconstructed")):
            require(abs(ind[archived] - context[reconstructed]) <= Decimal("0.00000005"),
                    f"{archived}: native replay mismatch at {utc(stamp)}")
        require(decisions(events["s5"][stamp]) == decisions(events["rc2-indicator"][stamp])
                == decisions(events["rc2-audit"][stamp]), f"Decision journal disagreement at {utc(stamp)}")
        require(decisions(events["rc2-indicator"][stamp]) == decisions(events["pressure"][stamp]),
                f"Pressure decision journal disagreement at {utc(stamp)}")
    rows = []
    buy_boundary = buy_anchor = buy_watch_ms = None
    for stamp, bar in m15.items():
        if not ms(START) <= stamp <= ms(END):
            continue
        co = all_decisions[stamp]
        ind = native["rc2-indicator"].get(stamp, {})
        s5 = native["s5"].get(stamp, {})
        aud = native["rc2-audit"].get(stamp, {})
        aggregate = aggregate_m5(bar, m5)
        require(aggregate and aggregate[:4] == values(bar, PRICES), f"Independent M5 OHLC at {utc(stamp)}")
        alert_states = [f'{a["direction"]} {a["state"]}' for a in alert[stamp]]
        buy_events = [event for event in events["pressure"][stamp] if event["side"] == "BUY"]
        buy_transition = ""
        buy_broken_anchor = buy_reclaimed_boundary = None
        for event in buy_events:
            if event["state"] == "WATCH":
                buy_boundary, buy_anchor, buy_watch_ms = event["price"], bar["low"], stamp
                buy_transition = "WATCH_STARTED"
            elif event["state"] in {"CONFIRMED", "REJECTED", "CANCELLED", "EXPIRED"}:
                if event["state"] == "CANCELLED" and buy_anchor is not None and bar["close"] < buy_anchor:
                    buy_broken_anchor = buy_anchor
                if event["state"] in {"CONFIRMED", "REJECTED"}:
                    buy_reclaimed_boundary = buy_boundary
                buy_boundary = buy_anchor = buy_watch_ms = None
                buy_transition = event["state"]
        row = {
            "open_utc": utc(bar["open_ms"]), "known_utc": utc(stamp), **{k: bar[k] for k in (*PRICES, "volume")},
            **co, "h1_last_closed_utc": utc(contexts[stamp].htf_closes[0]),
            "h4_last_closed_utc": utc(contexts[stamp].htf_closes[1]),
            "h1_native_bias": DIRECTIONS.get(ind.get("h1_bias")),
            "h4_native_bias": DIRECTIONS.get(ind.get("h4_bias")),
            "s5_sell_phase_before": s5.get("sell_phase_before"), "s5_sell_phase_after": s5.get("sell_phase_after"),
            "s5_sell_reason": s5.get("sell_reason"), "s5_buy_reason": s5.get("buy_reason"),
            "s5_buy_watch_active_boundary": buy_boundary, "s5_buy_watch_active_anchor": buy_anchor,
            "s5_buy_watch_since_utc": utc(buy_watch_ms) if buy_watch_ms else None,
            "s5_buy_watch_transition": buy_transition, "s5_buy_watch_anchor_broken_at_close": buy_broken_anchor,
            "s5_buy_watch_reclaimed_boundary": buy_reclaimed_boundary,
            "s5_candidate_count": s5.get("candidate_count"), "s5_warmup_pass": s5.get("warmup_pass"),
            "s5_valid_atr": s5.get("valid_atr"), "s5_acceptable_bar": s5.get("acceptable_bar"),
            "s5_bar_gap": s5.get("bar_gap"), "s5_date_pass": s5.get("date_pass"),
            "s5_news_paused": s5.get("news_paused"), "s5_session_pass": s5.get("session_pass"),
            "s5_fvg_required": s5.get("fvg_required"), "s5_structural_room_required": s5.get("structural_room_required"),
            "s5_bearish_close_location_pass": s5.get("bearish_close_location_pass"),
            "s5_extra_h1_required": s5.get("extra_h1_required"), "s5_native_position": s5.get("native_position_qty_at_close"),
            "s5_native_held_or_pending": s5.get("native_held_or_pending_plan_id"),
            "rc2_dashboard_state_at_close": ind.get("decision_state_at_close"),
            "rc2_dashboard_side": ind.get("decision_side"),
            "rc2_selected_marker_stage": ind.get("selected_marker_stage"),
            "rc2_range_filter_enabled": ind.get("range_filter_enabled"),
            "rc2_early_enabled": ind.get("early_enabled"), "rc2_early_alert_enabled": ind.get("alert_early_enabled"),
            "rc2_native_position": aud.get("native_position_qty_at_close"),
            "rc2_native_held_or_pending": aud.get("native_held_or_pending_plan_id"),
            "rc2_core": events["pressure"][stamp], "rc2_alert_payload_states": alert_states,
            "s5_native_execution": [row for row in events["s5"][stamp] if row["stream"] == "NATIVE"],
            "rc2_native_execution": [row for row in events["rc2-audit"][stamp] if row["stream"] == "NATIVE"],
            "wsb_original_plans": wsb[stamp], "independent_m5_ohlc_match": True,
            "independent_m5_volume_sum": aggregate[4], "m5_volume_difference": bar["volume"] - aggregate[4],
        }
        # The pinned forensic probe ends at 16:00. Later chart states are reconstructed
        # from observed core events and the last native confirmed state, where possible.
        if not ind:
            if row["rc2_core"]:
                row["rc2_dashboard_state_inferred"] = (
                    "INVALIDATED" if row["rc2_core"][0]["state"] == "CANCELLED" else row["rc2_core"][0]["state"])
            elif rows[-1].get("rc2_dashboard_state_inferred") == "INVALIDATED":
                row["rc2_dashboard_state_inferred"] = "NO SETUP"
        else:
            row["rc2_dashboard_state_inferred"] = None
        rows.append(row)
    early_events = []
    for row in rows:
        side = row["early_if_enabled_new_side"]
        if side:
            at = ms(row["known_utc"])
            nxt = m15.get(at + 900_000)
            early_events.append({
                "known_utc": row["known_utc"], "direction": "SELL" if side == -1 else "BUY",
                "close": row["close"], "previous_boundary": row["prior12_low" if side == -1 else "prior12_high"],
                "next_open_reference_only": nxt["open"] if nxt and nxt["open_ms"] == at else None,
            })
    target_bars = {}
    for level in (Decimal("4001.000"), Decimal("4000.755")):
        target_bars[str(level)] = {
            f"M{frame}": [{"open_utc": utc(bar["open_ms"]), "close_utc": utc(stamp),
                            **{key: bar[key] for key in PRICES}}
                           for stamp, bar in series.items() if ms(START) <= stamp <= ms(END) and bar["low"] == level]
            for frame, series in ((5, m5), (15, m15))
        }
    summary = {
        "source_commit": BASE, "source_archive_sha256": base_hash, "forensic_commit": PRIOR,
        "forensic_archive_sha256": old_hash, "symbol": SYMBOL, "chart": "standard M15 candles",
        "timeline": {"first_close_utc": START, "last_close_utc": END, "bars": len(rows)},
        "matched_old_and_base_ohlcv": {"M15": shared, "M5": shared_m5},
        "m5_aggregated_ohlc_matches": len(rows),
        "m5_volume_sum_differences": [
            {"known_utc": row["known_utc"], "m15": row["volume"], "m5_sum": row["independent_m5_volume_sum"]}
            for row in rows if row["m5_volume_difference"]
        ],
        "native_wsb_replay_bars": len(native["s5"]),
        "matched_warmup_open_utc": utc(next(iter(native["s5"].values()))["first_open_ms"]),
        "native_state_cutoff_utc": utc(next(reversed(native["s5"]))),
        "decision_parity": "Exact CORE and READY for prior S5, RC2 indicator, RC2 audit and pressure within native replay scope",
        "notes": {
            "timestamps": "UTC. Rows use decision time at chart-bar close. Tick-level low time is unavailable.",
            "early": "Only reconstructed condition and edge with native geometry parity; actual EARLY WATCH disabled.",
            "swing": "Strict 3+3 local price pivot. Fast swings use 2+2. A pivot is known only at its right-side confirmation close.",
            "alerts": "Native Pine Logs contain generated payloads. External delivery was not observed.",
            "post_cutoff": "The two final M15 rows use pressure native core/alerts; dashboard state inferred from the lifecycle.",
        },
        "target_lows": target_bars,
        "early_if_enabled": early_events,
        "core_events": dict(Counter(f'{e["side"]} {e["state"]}' for row in rows for e in row["rc2_core"])),
        "buy_watches": [
            {"known_utc": row["known_utc"], "watch_boundary": row["prior20_low"],
             "anchor_low": row["s5_buy_watch_active_anchor"],
             "close": row["close"], "h1": row["h1_native_bias"], "h4": row["h4_native_bias"]}
            for row in rows if any(e["state"] == "WATCH" and e["side"] == "BUY" for e in row["rc2_core"])
        ],
        "buy_watch_cancellations_below_anchor": [
            {"known_utc": row["known_utc"], "anchor": row["s5_buy_watch_anchor_broken_at_close"], "close": row["close"]}
            for row in rows if row["s5_buy_watch_anchor_broken_at_close"] is not None
        ],
    }
    return rows, summary


def serial(value):
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def main():
    prior, old_hash = archive(PRIOR, PRIOR_ARCHIVE)
    base, base_hash = archive(BASE, BASE_ARCHIVE)
    with prior, base:
        rows, summary = timeline(prior, base, old_hash, base_hash)
    OUT.mkdir(parents=True, exist_ok=True)
    columns = list(dict.fromkeys(key for row in rows for key in row))
    with (OUT / "forensic-timeline-M15.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: serial(value) if isinstance(value, (list, dict)) else value
                             for key, value in row.items()})
    (OUT / "forensic-summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False, default=str) + "\n",
                                                encoding="utf-8", newline="\n")
    print(f"{len(rows)} M15 decisions, {summary['native_wsb_replay_bars']} matched native replay bars")


if __name__ == "__main__":
    main()
