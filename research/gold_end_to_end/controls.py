"""Normalize the hash-pinned older S5 archive for offline product research."""

import argparse
from dataclasses import asdict, fields
from datetime import datetime, timedelta, timezone
import gzip
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path

from gold_pro.direct_breakout_research import FORWARD_SEAL, WARMUP, pinned
from gold_pro.replay import Plan
from research.data.loader import validate_bars
from research.net_campaign_evidence.replay_archive import (
    FRAMES, REGISTRATION, REGISTRATION_SHA, ROOT, comparable_window, load_archive, read_verified,
)


SCHEMA = "qirvanta.end_to_end.market_inputs.v1"
SYMBOL = "OANDA:XAUUSD"
BAR_FIELDS = ("time", "time_close", "open", "high", "low", "close")
PLAN_FIELDS = {field.name for field in fields(Plan)}
NATIVE_CONTROL = "6221eb63be02c70db1d6acae6d03745db9a45c94"
OBSERVED_COUNTS = {5: (5565, 98), 15: (6445, 133), 30: (10091, 165)}
SCOPE = (
    "Pre-seal, previously examined TradingView OANDA standard candles and older S5 admitted geometry only. "
    "No current Visual V2 or RC2 permission/position history, unstated S5 opportunities, native fills, "
    "broker bid/ask, live execution or untouched test."
)
DISPOSITION = "LOCAL_ONLY_RIGHTS_NOT_ESTABLISHED_DO_NOT_COMMIT_RAW_OR_FULL_OHLC"
# Canonical normalized data/geometry bytes from the three registered original inputs.
CANONICAL_PAYLOAD_SHA = "9538547dacdef7686e25149dae5500970e97a96907c1671a9ea578e1351bc146"


def _utc(stamp):
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=stamp)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _encode(payload):
    return (json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            + "\n").encode("utf-8")


def _strict_json(data):
    def reject_constant(value):
        raise ValueError(f"Non-standard JSON number {value}")

    def unique_keys(pairs):
        mapping = dict(pairs)
        if len(mapping) != len(pairs):
            raise ValueError("Duplicate keys in normalized archive")
        return mapping

    return json.loads(data, parse_constant=reject_constant, object_pairs_hook=unique_keys)


def _registration():
    return _strict_json(read_verified(REGISTRATION, REGISTRATION_SHA))


def _sealed_start():
    return _strict_json(pinned(FORWARD_SEAL))["forward_start_ms"]


def _source(registration, frame):
    original = registration["frames"][str(frame)]
    return {
        "bars_sha256": original["bars_sha256"],
        "native_log_sha256": original["log_sha256"],
        "native_log_name": original["log_name"],
        "registered_bars_path": f'{Path(original["local_bars"]).parent.name}/bars.csv',
    }


def _pairing(archive):
    windows, matched = comparable_window(archive)
    start, end = windows["full"]
    return {
        "start_ms": start, "end_ms": end, "start_utc": _utc(start), "end_utc": _utc(end),
        "exact_m5_to_m15_complete_aggregates": matched,
        "windows_ms": {name: list(bounds) for name, bounds in windows.items()},
        "partition": "Descriptive chronological slices of an already examined archive.",
    }


def build_inputs(data_root, native_root):
    """Build deterministic, market-only rows and immutable admitted Plan records."""
    archive = load_archive(Path(data_root), Path(native_root))
    registration = _registration()
    frames = {}
    for frame, (bars, plans, _) in archive.items():
        frames[f"M{frame}"] = {
            "minutes": frame, "source": _source(registration, frame),
            "first_open_ms": bars[0]["time"], "last_close_ms": bars[-1]["time_close"],
            "first_open_utc": _utc(bars[0]["time"]), "last_close_utc": _utc(bars[-1]["time_close"]),
            "bar_count": len(bars), "protected_plan_count": len(plans),
            "bars": [{key: bar[key] for key in BAR_FIELDS} for bar in bars],
            "protected_plans": [{**asdict(plan), "targets": list(plan.targets)} for plan in plans],
        }
    payload = {
        "schema": SCHEMA, "symbol": SYMBOL, "bar_clock": "UTC milliseconds", "chart_type": "standard",
        "scope": SCOPE, "raw_redistribution": DISPOSITION, "native_control_commit": NATIVE_CONTROL,
        "registration_path": REGISTRATION.relative_to(ROOT).as_posix(), "registration_sha256": REGISTRATION_SHA,
        "sealed_start_ms": _sealed_start(), "minimum_archive_warmup_bars": WARMUP,
        "paired_common": _pairing(archive), "frames": frames,
    }
    validate_inputs(payload)
    if sha256(_encode(payload)).hexdigest() != CANONICAL_PAYLOAD_SHA:
        raise ValueError("Registered sources did not reproduce the canonical normalized input")
    return payload


def _positive(value):
    return type(value) in (int, float) and isfinite(value) and value > 0


def _plan_mapping(rows, bars, frame):
    seen = set()
    plans = []
    for row in rows:
        if set(row) != PLAN_FIELDS or row["model"] != "S5_RAW" or type(row["side"]) is not int or row["side"] not in (1, -1):
            raise ValueError(f"M{frame}: unexpected normalized S5 plan schema")
        key, index = row["id"], row["bar"]
        if not isinstance(key, str) or key in seen or type(index) is not int or not 0 <= index < len(bars):
            raise ValueError(f"M{frame}: duplicate or unattested normalized plan ID/index")
        identity = key.split("|")
        if len(identity) != 7 or identity[:5] != ["S5", SYMBOL, str(frame), "2", str(row["side"])]:
            raise ValueError(f"M{frame}: normalized plan identifier differs from S5 source")
        born, known = (int(stamp) for stamp in identity[5:])
        if not 0 < born <= known == bars[index]["time_close"]:
            raise ValueError(f"M{frame}: plan identifier clock differs from original bar")
        prices = (row["reference"], row["stop"], row["atr"])
        targets = row["targets"]
        if not (all(_positive(value) for value in prices) and isinstance(targets, list) and len(targets) == 3
                and all(_positive(value) for value in targets) and row["units"] == 3
                and type(row["max_hold"]) is int and row["max_hold"] > 0):
            raise ValueError(f"M{frame}: invalid normalized plan geometry")
        if (row["reference"] - row["stop"]) * row["side"] <= 0 or any(
            (b - a) * row["side"] <= 0 for a, b in zip([row["reference"], *targets], targets)
        ):
            raise ValueError(f"M{frame}: unordered normalized stop or targets")
        seen.add(key)
        plans.append(Plan(**{**row, "targets": tuple(targets)}))
    return plans


def validate_inputs(payload):
    """Validate time, source metadata and geometry after a portable input load."""
    if (payload["schema"], payload["symbol"], payload["chart_type"], payload["native_control_commit"],
        payload["registration_sha256"], payload["minimum_archive_warmup_bars"]) != (
            SCHEMA, SYMBOL, "standard", NATIVE_CONTROL, REGISTRATION_SHA, WARMUP
    ):
        raise ValueError("Unexpected market/source registration")
    registration = _registration()
    cutoff = _sealed_start()
    if cutoff != payload["sealed_start_ms"] or set(payload["frames"]) != {f"M{frame}" for frame in FRAMES}:
        raise ValueError("Unexpected market-frame registration or protected cutoff")
    normalized = {}
    for frame in FRAMES:
        lane = payload["frames"][f"M{frame}"]
        bars, offers = lane["bars"], lane["protected_plans"]
        source = _source(registration, frame)
        if ((len(bars), len(offers)) != OBSERVED_COUNTS[frame] or
            (lane["bar_count"], lane["protected_plan_count"]) != (len(bars), len(offers)) or
            lane["minutes"] != frame or lane["source"] != source):
            raise ValueError(f"M{frame}: unexpected native source metadata/counts")
        if any(set(row) != set(BAR_FIELDS) or
               type(row["time"]) is not int or type(row["time_close"]) is not int for row in bars):
            raise ValueError(f"M{frame}: normalized bars require only exact market clock and OHLC")
        if (validate_bars(bars) != frame * 60_000 or
            (bars[0]["time"], bars[-1]["time_close"]) != (lane["first_open_ms"], lane["last_close_ms"]) or
            lane["last_close_ms"] >= cutoff or lane["first_open_utc"] != _utc(lane["first_open_ms"]) or
            lane["last_close_utc"] != _utc(lane["last_close_ms"])):
            raise ValueError(f"M{frame}: invalid native history or protected bar")
        normalized[frame] = bars, _plan_mapping(offers, bars, frame), source
    if payload["paired_common"] != _pairing(normalized):
        raise ValueError("Common M5/M15 pairing does not match the original registration")
    return normalized


def read_inputs(path, *, expected_sha256=None):
    """Read the local normalized gzip; supply the summary digest to attest exact bytes."""
    raw = Path(path).read_bytes()
    if expected_sha256 is not None and sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("Normalized local market input digest differs")
    decoded = gzip.decompress(raw)
    if sha256(decoded).hexdigest() != CANONICAL_PAYLOAD_SHA:
        raise ValueError("Normalized payload differs from the content-addressed registration")
    payload = _strict_json(decoded)
    validate_inputs(payload)
    return payload


def read_lanes(path, *, expected_sha256=None):
    """Return replay-compatible mapping frame -> (bars, Plan list, source)."""
    return validate_inputs(read_inputs(path, expected_sha256=expected_sha256))


def summarize(payload, data):
    """Omit market rows and plan details from the publishable provenance report."""
    return {
        **{key: value for key, value in payload.items() if key not in ("frames", "schema")},
        "schema": "qirvanta.end_to_end.market_input_provenance.v1",
        "normalized_gzip_sha256": sha256(data).hexdigest(),
        "normalized_uncompressed_sha256": sha256(_encode(payload)).hexdigest(),
        "normalized_bytes": len(data),
        "frames": {name: {key: value for key, value in lane.items() if key not in ("bars", "protected_plans")}
                   for name, lane in payload["frames"].items()},
        "source_reproduction": (
            "python -B -m research.gold_end_to_end.controls --data-root ../mission --native-root ../tv-final "
            "--output <local-output>/market_inputs.json.gz --manifest <local-output>/market-input-provenance.json"
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, default=ROOT.parent / "mission")
    parser.add_argument("--native-root", type=Path, default=ROOT.parent / "tv-final")
    parser.add_argument("--output", type=Path, default=ROOT.parent.parent / "outputs/gold-end-to-end/market_inputs.json.gz")
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(ROOT.resolve()):
        parser.error("keep full market inputs outside the tracked repository per registered archive provenance")
    if args.manifest is not None and args.manifest.resolve() == args.output.resolve():
        parser.error("market input and metadata output must be distinct paths")
    try:
        payload = build_inputs(args.data_root.resolve(), args.native_root.resolve())
        archive = gzip.compress(_encode(payload), compresslevel=9, mtime=0)
        report = summarize(payload, archive)
        if args.output.exists() and args.output.read_bytes() != archive:
            raise ValueError(f"{args.output}: refuse to replace a different normalized input")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_bytes(archive)
        if args.manifest is not None:
            args.manifest.parent.mkdir(parents=True, exist_ok=True)
            args.manifest.write_text(json.dumps(report, sort_keys=True, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except (ValueError, OSError, KeyError, TypeError) as exc:
        parser.exit(1, f"Market inputs rejected: {exc}\n")
    print(report["normalized_gzip_sha256"])


if __name__ == "__main__":
    main()

