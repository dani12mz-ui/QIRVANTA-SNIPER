"""Reconstruct the archived OANDA M15 higher-timeframe context and its source bars."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import csv
from datetime import datetime, timezone
from decimal import Decimal
from hashlib import sha256
from io import BytesIO, StringIO
import json
from math import floor
from pathlib import Path
from zipfile import ZipFile

from gold_pro import direct_breakout_research as archives, rc1_qa
from research.direct_trend_breakout.shadow_native import read_native


ROOT = Path(__file__).resolve().parents[2]
DT_ARCHIVE = ROOT / "research/direct_trend_breakout/evidence/base-native-logs.zip"
DT_SHA = "60ee8e06a5a54d2871903fd8eb33cd79d980be4b7ada386cef316fdc631d9a68"
DT_LANES = ("indicator-default", "indicator-early", "audit-fixed", "audit-s5")
OHLC = ("open", "high", "low", "close")
FIRST_CLOSE = 1794355200000  # 2026-11-11 00:00 UTC
LAST_CLOSE = 1794418200000  # 2026-11-11 17:30 UTC
HOUR = 3_600_000
QUARTER = HOUR // 4
FIVE_MIN = HOUR // 12
DIRECTIONS = {-1: "DOWN", 0: "MIXED", 1: "UP", None: "DATA MISSING"}


def utc(stamp):
    return datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat().replace("+00:00", "Z")


def epoch(iso):
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(raw):
    return sha256(raw).hexdigest()


def csv_rows(raw):
    return list(csv.DictReader(StringIO(raw.decode("utf-8-sig"), newline=""), strict=True))


def native_messages(raw, tag):
    for line in csv_rows(raw):
        for message in line["Message"].splitlines():
            name, _, payload = message.partition("|")
            if name == tag:
                yield epoch(line["Date"]), rc1_qa._json_dict(payload)


def archived_dt_base(frame=15):
    raw = DT_ARCHIVE.read_bytes()
    require(digest(raw) == DT_SHA, "Original Direct Trend archive SHA-256 changed")
    provenance, series = {}, {}
    with ZipFile(BytesIO(raw)) as archive:
        for role in DT_LANES:
            name = f"{role}-M{frame}.csv"
            lane = archive.read(name)
            rows = list(native_messages(lane, "DT_BASE"))
            interval = frame * 60_000
            closes = tuple(range(FIRST_CLOSE, LAST_CLOSE + 1, interval))
            require(tuple(row["known_ms"] for _, row in rows) == closes, f"{name}: expected the exact historical closes")
            for chart_log_open, row in rows:
                require(row["symbol"] == rc1_qa.SYMBOL and row["frame"] == str(frame), f"{name}: instrument/timeframe")
                require(chart_log_open == row["open_ms"] == row["known_ms"] - interval, f"{name}: open/close identity")
                require(all(row[key] <= row["open_ms"] for key in ("h1_known_ms", "h4_known_ms")),
                        f"{name}: HTF context not known at chart open")
            provenance[role] = {"file": name, "sha256": digest(lane), "count": len(rows),
                                "first_loaded_open_utc": utc(rows[0][1]["first_open_ms"])}
            series[role] = [row for _, row in rows]
    canonical = series["indicator-default"]
    fields = ("open_ms", "known_ms", *OHLC, "h1_known_ms", "h4_known_ms", "h1_bias", "h4_bias")
    for role, rows in series.items():
        require(all(all(a[key] == b[key] for key in fields) for a, b in zip(canonical, rows)),
                f"{role}: Original DT_BASE market/context divergence")
    return canonical, provenance


def bar(opened, closed, row):
    return {"time": opened, "time_close": closed, **{name: Decimal(str(row[name])) for name in OHLC}}


def whole_buckets(bars, step, size, anchor=0, allow_h4_daily_gap=False):
    parts = defaultdict(list)
    for item in bars:
        opened = item["time"]
        key = ((opened - anchor) // size) * size + anchor
        parts[key].append(item)
    result, incomplete = {}, []
    for opened, children in sorted(parts.items()):
        children.sort(key=lambda item: item["time"])
        start = opened
        if allow_h4_daily_gap and opened % (24 * HOUR) == 22 * HOUR and children[0]["time"] == opened + HOUR:
            start += HOUR
        if [child["time"] for child in children] != list(range(start, opened + size, step)) or any(
                child["time_close"] != child["time"] + step for child in children):
            incomplete.append(opened)
            continue
        result[opened + size] = bar(opened, opened + size, {
            "open": children[0]["open"], "high": max(x["high"] for x in children),
            "low": min(x["low"] for x in children), "close": children[-1]["close"],
        })
    return result, incomplete


def merge_bars(lanes):
    merged, sources, comparisons = {}, {}, []
    for name, lane in lanes:
        shared = set(merged) & set(lane)
        differences = [{"known_utc": utc(stamp), "first_source": sources[stamp], "second_source": name,
                        "fields": {field: [merged[stamp][field], lane[stamp][field]]
                                   for field in ("time", *OHLC) if merged[stamp][field] != lane[stamp][field]}}
                       for stamp in sorted(shared) if merged[stamp] != lane[stamp]]
        comparisons.append({"source": name, "bars": len(lane), "compared": len(shared), "differences": differences})
        merged.update(lane)
        sources.update(dict.fromkeys(lane, name))
    return dict(sorted(merged.items())), sources, comparisons


def lower_and_h1(candidate, last_close):
    candidate_native = read_native(candidate)
    require(candidate_native.timeframe == 15 and candidate_native.end, "Expected complete native M15 market lane")
    m15 = [bar(row["time"], row["time_close"], row) for row in candidate_native.bars if row["time_close"] <= last_close]
    with ZipFile(BytesIO(archives.pinned(archives.WSB_ARCHIVE))) as source:
        raw = archives.pinned("archival_m5_bars.csv", source.read("archival_m5_bars.csv"))
    m5 = csv_rows(raw)
    source_m5 = [bar(int(row["time"]), int(row["time_close"]), row) for row in m5]
    h1_m5, incomplete_m5 = whole_buckets(source_m5, FIVE_MIN, HOUR)
    h1_m15, incomplete_m15 = whole_buckets(m15, QUARTER, HOUR)
    with ZipFile(BytesIO(archives.pinned(archives.PRESSURE_ARCHIVE))) as source:
        raw = source.read("rc2/pressure_context_H1.csv")
    h1_raw = {}
    for opened, row in native_messages(raw, "PR_CTX"):
        require(opened == row["open_ms"] and row["symbol"] == rc1_qa.SYMBOL and row["timeframe"] == "60",
                "Native Pressure H1 timestamp or market mismatch")
        if row["close_ms"] <= last_close:
            h1_raw[row["close_ms"]] = bar(opened, row["close_ms"], row)
    h1, sources, comparisons = merge_bars((("WSB_ARCHIVED_M5_AGGREGATE", h1_m5), ("NATIVE_M15_AGGREGATE", h1_m15),
                                          ("NATIVE_PRESSURE_H1", h1_raw)))
    return m5, m15, h1, sources, {"comparisons": comparisons, "h1_native_source_sha256": digest(raw),
                                 "incomplete_m5_hours": [utc(value) for value in incomplete_m5],
                                 "incomplete_m15_hours": [utc(value) for value in incomplete_m15]}


def h4_history(path, m15, last_close):
    raw = path.read_bytes()
    base = {int(row["close_ms"]): bar(int(row["time_ms"]), int(row["close_ms"]), row) for row in csv_rows(raw)
            if int(row["close_ms"]) <= last_close}
    require(base and max(base) < last_close, "Native H4 warmup source missing or outside historical interval")
    origin = max(base) % (4 * HOUR)
    current = [item for item in m15 if item["time"] >= max(base) - 12 * HOUR]
    extension, incomplete = whole_buckets(current, QUARTER, 4 * HOUR, anchor=origin, allow_h4_daily_gap=True)
    merged, sources, comparisons = merge_bars((("NATIVE_OANDA_H4_HISTORICAL", base), ("NATIVE_M15_AGGREGATE", extension)))
    return merged, sources, {"sha256": digest(raw), "native_history_first_open_utc": utc(min(base.values(), key=lambda item: item["time"])["time"]),
                             "native_history_last_close_utc": utc(max(base)), "comparisons": comparisons,
                             "winter_session_handling": "22:00 UTC H4 bucket may start trading at 23:00 after daily break. Aggregate only actual contiguous candles through 02:00. No OHLC is created for the break.",
                             "incomplete_recent_h4_bars": [utc(value) for value in incomplete]}


def finite_side(close, fast, slow):
    if None in (close, fast, slow):
        return None
    c, f, s = (floor(float(value) * 1_000_000 + 0.5) for value in (close, fast, slow))
    return 1 if c > f > s else -1 if c < f < s else 0


def finite_context(history, source_close):
    closes = list(history)
    index = bisect_right(closes, source_close) - 1
    if index < 109 or closes[index] != source_close:
        return None
    used = closes[index - 109:index + 1]
    fast = slow = float(history[used[0]]["close"])
    for stamp in used[1:]:
        close = float(history[stamp]["close"])
        fast += (close - fast) * (2 / 22)
        slow += (close - slow) * (2 / 56)
    close = float(history[source_close]["close"])
    return {"close": close, "ema21": fast, "ema55": slow, "bias": DIRECTIONS[finite_side(close, fast, slow)],
            "first_used_close_utc": utc(used[0]), "first_used_close": history[used[0]]["close"],
            "source_closes": len(used)}


def ema50(history, *, start=None, seeded=None):
    result = {}
    value = seeded
    for stamp, row in history.items():
        if start is not None and stamp < start:
            continue
        if value is None:
            value = row["close"]
        elif stamp != start or seeded is None:
            value += (row["close"] - value) * (Decimal(2) / Decimal(51))
        result[stamp] = value
    return result


def native_h1_seeds(raw_m5, h1):
    closes = list(h1)
    seeds, mismatched = {}, set()
    for row in raw_m5:
        offset = bisect_right(closes, int(row["time"])) - 1
        if offset < 0 or not all(row[key] for key in ("htfClose", "htfEMA50", "htfEMA50prev")):
            continue
        stamp = closes[offset]
        native = tuple(Decimal(row[key]) for key in ("htfClose", "htfEMA50", "htfEMA50prev"))
        if native[0] != h1[stamp]["close"]:
            mismatched.add(stamp)
            continue
        require(seeds.setdefault(stamp, native) == native, f"Different native EMA50 seed at {utc(stamp)}")
    require(len(seeds) >= 3, "Missing archived H1 EMA50 seeds")
    seed = min(seeds)
    fitted = ema50(h1, start=seed, seeded=seeds[seed][1])
    differences = {stamp: abs(fitted[stamp] - native[1]) for stamp, native in seeds.items() if stamp >= seed}
    latest = max(seeds)
    resumed = ema50(h1, start=latest, seeded=seeds[latest][1])
    return resumed, {"first_seed_utc": utc(seed), "latest_seed_utc": utc(latest), "seed_value": seeds[latest][1],
                     "verified_archive_h1_close_seed_pairs": len(seeds),
                     "native_htf_close_mismatches": [utc(stamp) for stamp in sorted(mismatched)],
                     "recursive_check_max_abs_difference": max(differences.values()),
                     "recursive_check_over_1e_7": sum(value > Decimal("0.0000001") for value in differences.values())}


def legacy_at(history, values, source_close):
    closes = list(values)
    index = bisect_right(closes, source_close) - 1
    if index < 2 or closes[index] != source_close:
        return None
    close, ema, earlier = history[source_close]["close"], values[source_close], values[closes[index - 2]]
    side = 1 if close > ema > earlier else -1 if close < ema < earlier else 0
    return {"close": close, "ema50": ema, "ema50_lag2": earlier, "bias": DIRECTIONS[side]}


def native_number(value, parser=Decimal):
    return None if value.lower() in {"na", "nan"} else parser(value)


def probe_tuple(fields, finite=False):
    if finite:
        names = ("source_close", "close", "ema21", "ema55", "seed_close", "seed_price")
        integers = {"source_close", "seed_close"}
    else:
        names = ("source_close", "close", "ema50", "ema50_lag2", "source_index", "first_open", "seed_price", "bias")
        integers = {"source_close", "source_index", "first_open", "bias"}
    return {name: native_number(value, int if name in integers else Decimal) for name, value in zip(names, fields)}


def probe_rows(path, frame):
    raw = path.read_bytes()
    rows = []
    for raw_row in csv_rows(raw):
        for line in raw_row["Message"].splitlines():
            tag, _, payload = line.partition("|")
            if tag != "HTF_PROBE":
                continue
            fields = payload.split("|")
            require(len(fields) == 48, f"Malformed HTF_PROBE in {path.name}")
            source_frame, first, opened, closed = map(int, fields[:4])
            require(source_frame == frame and epoch(raw_row["Date"]) == opened and closed == opened + frame * 60_000,
                    f"Invalid native probe time in {path.name}")
            variants = {name: probe_tuple(fields[i:i + 8]) for name, i in zip(
                ("h1_default", "h4_default", "h1_explicit_1200", "h4_explicit_1200"), (4, 12, 20, 28))}
            variants.update(h1_finite=probe_tuple(fields[36:42], True), h4_finite=probe_tuple(fields[42:48], True))
            require(all(value["source_close"] is not None and value["source_close"] <= opened for value in variants.values()),
                    f"Unavailable or forward HTF stamp in {path.name}")
            for name, value in variants.items():
                if "bias" in value:
                    c, ema, earlier = (value[key] for key in ("close", "ema50", "ema50_lag2"))
                    side = 1 if None not in (c, ema, earlier) and c > ema > earlier else -1 if (
                        None not in (c, ema, earlier) and c < ema < earlier) else 0
                    require(value["bias"] == side, f"Incorrect legacy decision inputs in {path.name}/{name}")
            rows.append({"chart_first": first, "chart_open": opened, "chart_close": closed, **variants})
    expected = tuple(range(FIRST_CLOSE, LAST_CLOSE + 1, frame * 60_000))
    require(tuple(row["chart_close"] for row in rows) == expected, f"Incomplete native probe {path.name}")
    require(len({row["chart_first"] for row in rows}) == 1, f"Changed chart warmup during {path.name}")
    return rows, digest(raw)


def probe_statistics(directory):
    series, summary, htf_values = {}, {}, {}
    for frame in (5, 15, 30):
        path = directory / f"probe-M{frame}.csv"
        rows, checksum = probe_rows(path, frame)
        series[frame] = rows
        summary[f"M{frame}"] = {"file": path.name, "sha256": checksum, "rows": len(rows),
                                "first_loaded_chart_open_utc": utc(rows[0]["chart_first"]), "requests": {}}
        for label in ("h1", "h4"):
            default, explicit, finite = (f"{label}_{suffix}" for suffix in ("default", "explicit_1200", "finite"))
            equal = sum(row[default] == row[explicit] for row in rows)
            first = {(row[default]["first_open"], row[default]["seed_price"]) for row in rows}
            require(len(first) == 1, f"Changed legacy source warmup inside {path.name}")
            first_open, seed = first.pop()
            summary[f"M{frame}"]["requests"][label] = {
                "default_equals_explicit_1200_rows": equal, "legacy_first_evaluated_htf_open_utc": utc(first_open),
                "legacy_seed_price": seed,
                "legacy_source_index_range": [min(row[default]["source_index"] for row in rows),
                                             max(row[default]["source_index"] for row in rows)],
                "legacy_native_bias": Counter(DIRECTIONS[row[default]["bias"]] for row in rows),
                "finite_native_bias": Counter(DIRECTIONS[finite_side(row[finite]["close"], row[finite]["ema21"],
                                                                     row[finite]["ema55"]) if row["chart_open"]
                    - row[finite]["source_close"] <= (2 if label == "h1" else 8) * HOUR else None] for row in rows),
            }
            for row in rows:
                for name in (default, explicit, finite):
                    source = row[name]
                    key = name, source["source_close"]
                    require(htf_values.setdefault(key, source) == source,
                            f"Probe variants differ for same completed {name} bar at {utc(source['source_close'])}")
    old_m5, _ = archived_dt_base(5)
    comparable = list(zip(old_m5, series[5]))
    for prior, actual in comparable:
        require(prior["open_ms"] == actual["chart_open"] and prior["known_ms"] == actual["chart_close"]
                and prior["h1_known_ms"] == actual["h1_default"]["source_close"]
                and prior["h4_known_ms"] == actual["h4_default"]["source_close"], "Original M5/source probe clock mismatch")
    summary["old_m5_probe_contrast"] = {
        "exact_h1_h4_source_close_pairs": len(comparable),
        "archived_h4_raw_direction_values": Counter(str(prior["h4_bias"]) for prior, _ in comparable),
        "native_thin_h4_raw_direction_values": Counter(str(actual["h4_default"]["bias"]) for _, actual in comparable),
        "note": "The archive logged only direction, so 0 cannot distinguish a neutral score from missing EMA inputs.",
    }
    summary["unique_same_source_tuples_cross_frame"] = Counter(name for name, _ in htf_values)
    return series[15], summary


def compare_probe_value(native, actual, names):
    if actual is None or any(native[name] is None or actual[name] is None for name in names):
        return None
    return {name: abs(Decimal(str(actual[name])) - native[name]) for name in names}


def measured_legacy(probe_dir, h4_history, originals):
    out, details = {}, {}
    for frame, archived in originals.items():
        path = probe_dir / f"legacy-M{frame}.csv"
        rows = [row for _, row in native_messages(path.read_bytes(), "DT_BASE")]
        require(len(rows) == len(archived), "Incomplete native measurement of the legacy Pine")
        counters = Counter()
        by_source = {}
        for actual, original in zip(rows, archived):
            fields = ("open_ms", "known_ms", *OHLC, "h1_known_ms", "h4_known_ms", "h1_bias", "h4_bias")
            require(all(actual[key] == original[key] for key in fields),
                    f"M{frame}: archival state did not reproduce at {utc(actual['known_ms'])}")
            known = actual["h4_known_ms"]
            price, ema, lag = (actual[key] for key in ("h4_price", "h4_ema50", "h4_ema50_lag2"))
            require(price == h4_history[known]["close"], "Legacy and reconstructed H4 prices differ")
            expected = 0 if ema is None or lag is None else 1 if price > ema > lag else -1 if price < ema < lag else 0
            require(expected == actual["h4_bias"], "Legacy recorded operands do not explain the recorded side")
            counters["archived_ohlc_times_and_sides_equal"] += 1
            counters["H4_ema50_missing"] += ema is None
            counters["H4_ema50_lag2_missing"] += lag is None
            counters["legacy_context_unavailable"] += not actual["legacy_ready"]
            counters["H1_ema50_lag2_missing"] += actual["h1_ema50_lag2"] is None
            by_source[utc(known)] = {"price": price, "ema50": ema, "ema50_lag2": lag, "raw_bias": expected}
        out[f"M{frame}"] = {"sha256": digest(path.read_bytes()), "first_open_utc": utc(rows[0]["first_open_ms"]),
                           "counts": dict(counters), "source_closes": by_source}
        details[frame] = {row["known_ms"]: row for row in rows}
    return out, details


def forensic(candidate, h4_source, probe_dir=None, legacy_dir=None):
    old, provenance = archived_dt_base()
    old_m5, m5_provenance = archived_dt_base(5)
    original_m5 = {row["known_ms"]: row for row in old_m5}
    original_pairs = {}
    for row in old:
        close = row["known_ms"]
        lower = original_m5[close]
        require(all(lower[key] == row[key] for key in ("h1_known_ms", "h4_known_ms", "h1_bias")),
                f"Legacy source time or H1 side disagreement at {utc(close)}")
        require(lower["h4_bias"] != row["h4_bias"], f"Expected archived H4 disagreement at {utc(close)}")
        part = [original_m5.get(close - shift * FIVE_MIN) for shift in (2, 1, 0)]
        ohlc = None
        if all(item is not None for item in part):
            ohlc = (part[0]["open"], max(x["high"] for x in part),
                    min(x["low"] for x in part), part[-1]["close"])
        original_pairs[close] = {
            "M5_chart_open_utc": utc(lower["open_ms"]), "M15_chart_open_utc": utc(row["open_ms"]),
            "H1_source_close_utc": utc(row["h1_known_ms"]), "H4_source_close_utc": utc(row["h4_known_ms"]),
            "M5_H4_raw": lower["h4_bias"], "M15_H4_raw": row["h4_bias"], "H1_raw": row["h1_bias"],
            "M5_aggregate_ohlc": dict(zip(OHLC, ohlc)) if ohlc is not None else None,
            "M5_aggregate_matches_M15": ohlc == tuple(row[key] for key in OHLC) if ohlc is not None else None,
        }
    newer = rc1_qa.read_export(candidate, 15, "indicator")
    require(tuple(row.closing for row in newer.context) == tuple(row["known_ms"] for row in old),
            "Newer source must provide exactly the same 71 M15 closes")
    for actual, prior in zip(newer.context, old):
        require(actual.opening == prior["open_ms"] and actual.ohlc == tuple(prior[name] for name in OHLC)
                and actual.htf_closes == tuple(prior[name] for name in ("h1_known_ms", "h4_known_ms")),
                f"OHLC or original source-close mismatch at {utc(actual.closing)}")
    m5, m15, h1, h1_sources, h1_source_checks = lower_and_h1(candidate, LAST_CLOSE)
    h4, h4_sources, h4_source_checks = h4_history(h4_source, m15, LAST_CLOSE)
    measured, measured_rows = measured_legacy(legacy_dir, h4, {5: old_m5, 15: old}) if legacy_dir else (None, {})
    h1_legacy, seed = native_h1_seeds(m5, h1)
    histories = {"h1": h1, "h4": h4}
    source_names = {"h1": h1_sources, "h4": h4_sources}
    full_h4 = ema50(h4)
    old_first = old[0]["first_open_ms"]
    new_first = newer.context[0].first_open
    h4_old_seed = ema50(h4, start=old_first)
    h4_new_seed = ema50(h4, start=new_first)
    probe_summary, probe_by_close, native_h4 = None, {}, None
    if probe_dir:
        probe_rows_m15, probe_summary = probe_statistics(probe_dir)
        probe_by_close = {row["chart_close"]: row for row in probe_rows_m15}
        seed_info = probe_rows_m15[0]["h4_default"]
        seed_close = next((time for time, item in h4.items() if item["time"] == seed_info["first_open"]), None)
        if seed_close is not None and h4[seed_close]["close"] == seed_info["seed_price"]:
            native_h4 = ema50(h4, start=seed_close, seeded=seed_info["seed_price"])
    h4_native_indexes = dict(zip(native_h4, range(len(native_h4)))) if native_h4 else {}
    results, stats = [], defaultdict(Counter)
    for row in old:
        stamp, opened = row["known_ms"], row["open_ms"]
        pair = {}
        for label, history in histories.items():
            known = row[f"{label}_known_ms"]
            max_age = 2 * HOUR if label == "h1" else 8 * HOUR
            require(known in history, f"Missing recorded {label} source close {utc(known)}")
            source_closes = list(history)
            open_join = source_closes[bisect_right(source_closes, opened) - 1]
            close_join = source_closes[bisect_right(source_closes, stamp) - 1]
            stats[label]["open_join_exact"] += open_join == known
            stats[label]["close_join_would_change_source"] += close_join != known
            finite = finite_context(history, known) if 0 <= opened - known <= max_age else None
            original = DIRECTIONS[row[f"{label}_bias"]]
            selected = legacy_at(history, h1_legacy if label == "h1" else native_h4 or full_h4, known)
            stats[label]["legacy_direction_match"] += selected is not None and selected["bias"] == original
            stats[label]["finite_direction_differs_from_legacy_archive"] += finite is not None and finite["bias"] != original
            stats[label][f"archived_{original}"] += 1
            data = {"source_close_utc": utc(known), "age_at_chart_open_minutes": (opened - known) // 60_000,
                    "source_ohlc_lane": source_names[label][known], "original_dt_base_bias": original,
                    "reconstructed_legacy": selected, "canonical_finite_110": finite,
                    "open_join_exact": open_join == known, "close_join_would_change_source": close_join != known}
            if label == "h4":
                data["short_history_seed_sensitivity"] = {"old_chart_start": legacy_at(history, h4_old_seed, known),
                                                          "new_chart_start": legacy_at(history, h4_new_seed, known)}
                for name, context in data["short_history_seed_sensitivity"].items():
                    stats[label][f"hypothetical_{name}_bias_match"] += context is not None and context["bias"] == original
            if probe_by_close:
                probe = probe_by_close[stamp]
                native, native_finite = probe[f"{label}_default"], probe[f"{label}_finite"]
                require(probe["chart_open"] == opened and native["source_close"] == known == native_finite["source_close"],
                        f"Historical/native probe alignment at {utc(stamp)}")
                differences = compare_probe_value(native, selected, ("close", "ema50", "ema50_lag2"))
                finite_differences = compare_probe_value(native_finite, finite, ("close", "ema21", "ema55"))
                finite_seed = bool(finite and epoch(finite["first_used_close_utc"]) == native_finite["seed_close"]
                                   and finite["first_used_close"] == native_finite["seed_price"])
                stats[label]["native_legacy_direction_matches_dt_base"] += DIRECTIONS[native["bias"]] == original
                stats[label]["legacy_reconstructed_numeric_matches_probe_1e_7"] += bool(
                    differences and max(differences.values()) <= Decimal("0.0000001"))
                stats[label]["finite_reconstructed_numeric_matches_probe_1e_7"] += bool(
                    finite_differences and max(finite_differences.values()) <= Decimal("0.0000001") and finite_seed)
                if label == "h4":
                    stats[label]["reconstructed_legacy_source_index_matches_probe"] += (
                        h4_native_indexes.get(known) == native["source_index"])
                data.update(native_probe_default_ema50=native, native_probe_finite=native_finite,
                            legacy_abs_differences=differences, finite_abs_differences=finite_differences,
                            finite_seed_exact=finite_seed)
            pair[label] = data
        if measured_rows:
            original_pairs[stamp]["direct_native_H4_measurement"] = {
                f"M{frame}": {key: measured_rows[frame][stamp][key] for key in
                              ("h4_price", "h4_ema50", "h4_ema50_lag2", "legacy_ready")}
                for frame in (5, 15)}
        results.append({"chart_open_utc": utc(opened), "known_at_utc": utc(stamp),
                        "ohlc": dict(zip(OHLC, (row[key] for key in OHLC))),
                        "legacy_cross_frame": original_pairs[stamp], **pair})
    maxima = {}
    for label in histories:
        maxima[label] = {}
        for name in ("legacy_abs_differences", "finite_abs_differences"):
            comparisons = [row[label][name] for row in results if row[label].get(name)]
            if comparisons:
                maxima[label][name] = {field: max(item[field] for item in comparisons) for field in comparisons[0]}
    seed_sensitivity = {label: {"seed_source_close_utc": utc(min(values)), "seed_price": values[min(values)]}
                        for label, values in (("original_chart_origin", h4_old_seed), ("recent_chart_origin", h4_new_seed))}
    return {"scope": "Historical OANDA:XAUUSD M5/M15. No observations from a sealed or forward period.",
            "old_archive_sha256": DT_SHA, "old_archive_lanes": provenance, "candidate_csv_sha256": digest(candidate.read_bytes()),
            "old_M5_lanes": m5_provenance, "original_M5_M15_equal_source_closes": len(original_pairs),
            "original_M5_M15_unequal_H4_states": len(original_pairs),
            "original_M5_aggregation": dict(Counter(str(item["M5_aggregate_matches_M15"]) for item in original_pairs.values())),
            "direct_legacy_native_measurement": measured,
            "candidate_first_loaded_open_utc": utc(new_first), "exact_matched_local_ohlc_and_source_stamps": len(results),
            "h1_source_checks": h1_source_checks, "h1_legacy_native_seed": seed, "h4_source_checks": h4_source_checks,
            "source_alignment_counts": dict(stats), "thin_native_probe": probe_summary,
            "h4_chart_origin_seed_sensitivity": seed_sensitivity, "max_numeric_errors_vs_thin_probe": maxima, "rows": results,
            "limitations": ["Original DT_BASE has direction and source-close timestamp, not exact EMA payloads.",
                            "Legacy H1 seed is rounded in the native WSB archive. H4 uses available native history, with the thin probe seed when its exact source bar is available, plus labeled chart-origin sensitivity.",
                            "Canonical uses exactly 110 known closes and 2h/8h maximum ages at the chart open.",
                            "Chart-close joins are counterfactual and never inform the reconstruction."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-m15", type=Path, required=True)
    parser.add_argument("--h4-source", type=Path, required=True, help="Private native-matched OANDA H4 OHLC history")
    parser.add_argument("--probe-dir", type=Path, help="Native HTF_PROBE M5/M15/M30 CSV directory")
    parser.add_argument("--legacy-dir", type=Path, help="Native legacy M5/M15 CSVs with logged EMA50 operands")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = forensic(args.candidate_m15, args.h4_source, args.probe_dir, args.legacy_dir)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
                           encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
