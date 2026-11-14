"""Verify the registered native H1 experiment and score its two independent tapes."""

import argparse
from decimal import Decimal
from gzip import compress, decompress
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from research.core_htf_readiness import compare_native as native
from research.net_campaign_evidence.verify_readiness import pinned, require


HERE = Path(__file__).resolve().parent
RESEARCH = "research/net_campaign_evidence"
REGISTERED_NATIVE = "7fc1f0b71edeb05da804385e0351e9228015dc0a"
REGISTERED_FORENSIC = "fd5af2b4314424bd13cd956b13a63f47873d047d"
ARCHIVE_SHA256 = "230c37c21558fcd3dd1790e4689059f2d9808fbbc1efd594688b837071e187a7"


def pinned_archive(repo):
    data = pinned(repo, REGISTERED_NATIVE, f"{RESEARCH}/evidence/M15-5000-native.zip")
    with ZipFile(BytesIO(data)) as archive, TemporaryDirectory(prefix="h1-pinned-a-") as directory:
        file = Path(directory) / "M15-5000-add.csv"
        file.write_bytes(archive.read(file.name))
        digest = sha256(file.read_bytes()).hexdigest()
        require(digest == ARCHIVE_SHA256, "The original 5000-bar export changed")
        start = native.timestamp("2026-11-11T00:00:00Z")
        stop = native.timestamp("2026-11-11T17:30:00Z")
        return native.load_run(file, 15, start, stop)


def check_original(a, archive):
    market = archive["market"]
    assert_market = [(row["open_ms"], row["close_ms"], *(row[k] for k in ("open", "high", "low", "close")))
                     for row in a["bars"]]
    require(assert_market == [native.candle_value(bar) for bar in market.values()],
            "New A market history diverges from the registered 5000-bar original")
    return check_original_scope(a, archive, len(market))


def check_original_scope(a, archive, market_count=None):
    clocks = set(archive["rows"])
    context = {row["close_ms"]: row for row in a["htf"] if row["close_ms"] in clocks}
    require(context.keys() == clocks, "New A export is missing original audit closes")
    terms = {"close1": "htf_close", "ema50_1": "ema50", "ema50_3": "ema50_prev",
             "atr_mean": "atr_mean", "rsi": "rsi", "adx": "adx"}
    def numeric(value):
        return Decimal(str(value)) if value is not None else None
    for clock, row in archive["rows"].items():
        require(all(numeric(context[clock][left]) == numeric(row[right]) for left, right in terms.items()),
                f"New A confirmed HTF/local values changed at {native.utc(clock)}")

    for key in ("CORE", "RAW"):
        original = archive["s5"][key]
        compared = [event for event in a["events"] if event["book"] == key and event["known_time"] in clocks]
        if key == "CORE":
            # RC1_CORE captures the original display bridge's S5 subset.
            compared = [event for event in compared
                        if event["strategy_key"] == "S5"
                        and event["event"] in {"WATCH", "CONFIRMED", "REJECTED", "CANCELLED", "EXPIRED"}]
        require(list(original) == compared, f"New A {key} event stream differs from original")
    ready = [row for row in a["ready"] if row["confirmed_ms"] in clocks]
    require(list(archive["s5"]["READY"]) == ready, "New A READY event stream differs from original")
    books = {row["close_ms"]: row for row in a["books"]["RAW"]}
    for row in archive["s5"]["LEDGER"]:
        stamp = row["known_ms"]
        require(all(books[stamp][field] == value for field, value in row.items()
                    if field not in {"known_ms", "symbol", "frame"}),
                f"New A RAW ledger differs from original at {native.utc(stamp)}")
    return {"status": "VALIDATED_PROTECTED_WINDOW" if market_count is not None else "VALIDATED_PROTECTED_SAMPLE",
            "conditions": "Exact chart series, confirmed tuple, CORE, READY and RAW" if market_count is not None
            else "Exact archived-sample chart bars, confirmed tuple, CORE, READY and RAW",
            "sha256": ARCHIVE_SHA256, "confirmed_bars": market_count, "audited_checkpoint_closes": len(clocks)}


def check_first_capture(repo, path):
    """Reconcile the first bounded Pine export before asking for all four native tapes."""
    from research.net_campaign_evidence.h1_native import validate

    archive = pinned_archive(repo)
    header, tags = validate._read_window(path)
    market = archive["market"]
    clocks = tuple(archive["rows"])
    require(header["lane"] == "A" and header["frame"] == 15
            and header["first_open_ms"] == next(iter(market.values()))["time"]
            and tuple(row["close_ms"] for row in tags["H1_BAR"]) == clocks,
            "First capture must use A, the original origin and the 71 recorded close clocks")
    fields = ("open_ms", "close_ms", "open", "high", "low", "close")
    require(all(tuple(row[key] for key in fields) == native.candle_value(market[row["close_ms"]])
                and row["index"] == market[row["close_ms"]]["index"] for row in tags["H1_BAR"]),
            "First capture has mismatched chart bars or calculation index")
    require(len(tags["H1_HTF"]) == len(clocks)
            and len(tags["H1_BOOK"]) == len(clocks) * len(validate.BOOK_NAMES)
            and len(tags["H1_STATE"]) == len(clocks) * 2,
            "First capture is incomplete")
    clean = lambda name: [{k: v for k, v in row.items() if k != "lane"} for row in tags[name]]
    checked = {
        "htf": clean("H1_HTF"), "events": clean("H1_E"), "ready": clean("H1_READY"),
        "books": {"RAW": [row for row in clean("H1_BOOK") if row["book"] == "RAW"]},
    }
    chart = {row["close_ms"]: row for row in clean("H1_BAR")}
    settings = {"settings": {"core_htf": "240"}, "frame_minutes": 15, "symbol": header["symbol"]}
    validate._context(checked["htf"], chart, settings)
    validate._events(checked["events"], checked["ready"], chart, settings)
    return {**check_original_scope(checked, archive), "new_sample_sha256": header["sha256"],
            "scope": "Bounded parity. Still requires four full runs for A/B."}


def journal_differences(a, b):
    """Retain added, absent and changed event payloads and their independent state at the close."""
    events = []
    for name in ("events", "ready"):
        left, right = ({row["event_id"]: row for row in lane[name]} for lane in (a, b))
        for identity in left.keys() | right.keys():
            old, new = left.get(identity), right.get(identity)
            if old != new:
                row = old if old is not None else new
                events.append({
                    "kind": name, "id": identity, "close_ms": row.get("known_time", row.get("confirmed_ms")),
                    "A": old, "B": new,
                })
    states = []
    for name, left, right in [("formation_and_shields", a["state"], b["state"])] + [
            (book, a["books"][book], b["books"][book]) for book in a["books"]
    ]:
        require(len(left) == len(right), f"Incomplete matched {name} state")
        for old, new in zip(left, right):
            if old != new:
                states.append({
                    "kind": name, "close_ms": old["close_ms"],
                    "side": old.get("side"), "changed": {
                        key: {"A": old.get(key), "B": new.get(key)}
                        for key in sorted(old.keys() | new.keys()) if old.get(key) != new.get(key)
                    },
                })
    return {
        "native_event_differences": sorted(events, key=lambda r: (r["close_ms"], r["kind"], r["id"])),
        "independent_state_differences": sorted(states, key=lambda r: (r["close_ms"], r["kind"], r["side"] or 0)),
    }


def run_experiment(repo, a_file, repeat_a_file, b_file, repeat_b_file, deeper_file=None):
    from research.net_campaign_evidence.h1_native import report, validate

    a, repeat_a, b, repeat_b = (validate.validate_export(path)
                                 for path in (a_file, repeat_a_file, b_file, repeat_b_file))
    require(a["manifest"]["lane"] == repeat_a["manifest"]["lane"] == "A"
            and b["manifest"]["lane"] == repeat_b["manifest"]["lane"] == "B", "Lane labeling is incorrect")
    script_sha = sha256((HERE / "H1_NATIVE_DIAGNOSTIC.pine").read_bytes()).hexdigest()
    runs = (a, repeat_a, b, repeat_b)
    require(all(lane["manifest"]["source_sha256"] == script_sha for lane in runs),
            "Manifest script hash differs from the shipped diagnostic")
    require(len({lane["manifest"]["provenance"]["capture_id"] for lane in runs}) == len(runs)
            and len({lane["manifest"]["provenance"]["profile"] for lane in runs}) == 1,
            "All four captures require distinct provenance labels under the same authorized test-profile alias")
    for first, repeat in ((a, repeat_a), (b, repeat_b)):
        setup, repeated = first["manifest"]["provenance"], repeat["manifest"]["provenance"]
        require(setup["capture_id"] != repeated["capture_id"] and repeated["mode"] in {"reload", "readd"},
                "An independent reload or re-add capture is required per lane")
    primary = json.loads((HERE / "CAPTURE_PROTOCOL.json").read_bytes())["primary"]
    require(a["manifest"]["first_open_ms"] == primary["first_open_ms"]
            and a["manifest"]["fixed_end_close_ms"] == primary["last_close_ms"]
            and a["manifest"]["scoring_start_ms"] == primary["score_first_close_ms"]
            and a["manifest"]["calculated_bars"] == primary["calculated_bars"]
            and a["manifest"]["frame_minutes"] == primary["frame_minutes"], "Unexpected primary market registration")
    parity = check_original(a, pinned_archive(repo))
    repeatability = [validate.compare_runs(first, again, kind="same")
                     for first, again in ((a, repeat_a), (b, repeat_b))]
    require(all(result["status"] == "VALIDATED_EXACT_REPEAT" for result in repeatability),
            "Both native lanes must repeat exactly on identical inputs")
    comparison = validate.compare_runs(a, b, kind="ab")
    require(comparison["status"] == "VALIDATED_MATCHED_AB", f"A/B mismatch: {comparison}")
    depth = {"status": "DEPTH_STABILITY_UNESTABLISHED", "reason": "No authorized deeper A export"}
    if deeper_file is not None:
        deeper = validate.validate_export(deeper_file)
        require(deeper["manifest"]["lane"] == "A" and deeper["manifest"]["source_sha256"] == script_sha,
                "Depth comparison must use the same original A source")
        depth = validate.compare_runs(a, deeper, kind="depth")
        require(depth["status"] != "INVALID_COMPARISON", f"Incompatible deeper export: {depth}")
    scored = report.report_metrics(a, b, comparison, depth_stability=depth, known_case=known_case(repo))
    return {
        "schema": "qirvanta.h1-controlled-result.v1", "result_scope": "HISTORICAL_CONTROLLED_RESEARCH_ONLY",
        "original_continuation_result": None, "independent_holdout_result": None,
        "modeled_cost_admission_replay": None, "original_a_parity": parity,
        "matched_repeatability": repeatability, "matched_ab": comparison, "depth_stability": depth,
        "input_manifests": [lane["manifest"] for lane in runs], "results": scored,
        "difference_journal": journal_differences(a, b),
    }


def known_case(repo):
    data = pinned(repo, REGISTERED_FORENSIC, f"{RESEARCH}/evidence/H1_CONTROL_AND_DIFFS.json.gz")
    result = json.loads(decompress(data))
    cases = result["known_at_case_diagnostics"]["unique_observed_cases"]
    [row] = [item for item in cases if item["frame"] == 15 and item["side"] == 1
             and item["close_ms"] == native.timestamp("2026-11-11T13:00:00Z")]
    [case] = [item["hypothetical_next_open_only"] for item in row["opening_and_fill_clock_evidence_by_capture"]
              if item["source_id"] == "core_htf_readiness/native/M15-5000-add.csv"]
    return {
        "scope": "Mechanical known-at next-open gate and alternate cost-ratio arithmetic, no P&L attributed.",
        "pinned_commit": REGISTERED_FORENSIC, "known_close_ms": row["close_ms"],
        "observed_midpoint_open": case["observed_midpoint_open"],
        "original_gate_ratio": case["source_cost_over_midpoint_risk"],
        "normal_all_in_over_same_midpoint_risk": case["quote_sensitivity"]["normal"]["all_in_cost_over_midpoint_risk"],
        "stress_all_in_over_same_midpoint_risk": case["quote_sensitivity"]["stress"]["all_in_cost_over_midpoint_risk"],
        "gate_threshold": case["source_cost_over_risk_max"],
    }


def encode(value):
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"Unexpected evidence value {type(value).__name__}")


def write_report(path, result):
    data = (json.dumps(result, default=encode, ensure_ascii=False, sort_keys=True, indent=2,
                       allow_nan=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(compress(data, mtime=0) if path.suffix == ".gz" else data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--a", type=Path)
    parser.add_argument("--a-repeat", type=Path)
    parser.add_argument("--b", type=Path)
    parser.add_argument("--b-repeat", type=Path)
    parser.add_argument("--a-deeper", type=Path)
    parser.add_argument("--first-capture", type=Path, help="Check the first 71-bar lane A sample before full capture")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    lanes = (args.a, args.a_repeat, args.b, args.b_repeat)
    if args.first_capture and (any(lanes) or args.a_deeper) or not args.first_capture and not all(lanes):
        parser.error("choose --first-capture or provide all four A/B manifests")
    try:
        if args.first_capture:
            execution = check_first_capture(args.repo.resolve(), args.first_capture)
        else:
            execution = run_experiment(args.repo.resolve(), *lanes, args.a_deeper)
        write_report(args.output, execution)
    except (KeyError, ValueError, OSError) as exc:
        parser.exit(1, f"H1 report rejected: {exc}\n")
    print(f"H1 diagnostic results written to {args.output}")


if __name__ == "__main__":
    main()
