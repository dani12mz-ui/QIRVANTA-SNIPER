
"""Build and check paired TradingView journals without changing the research or release source."""
from argparse import ArgumentParser
from collections import Counter
import csv
from datetime import datetime, timezone
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
from research.direct_trend_visibility import native_qa

RELEASE = ROOT / "releases/gold-pro-unified"
BASE = RELEASE / "QIRVANTA_GOLD_PRO_UNIFIED.pine"
CANDIDATE = HERE / "QIRVANTA_GOLD_PRO_UNIFIED_MISSED_MOVE.pine"
BASE_SHA = "03150c89a587cc81559d8712118f8c0179eaf66496e61e0ec8689dba79c2c588"
SEAL = ROOT / "docs/gold_pro_evidence/forward_seal_m5.json"
SEAL_SHA = "09181ecaf9e30cde62c5203b524cb458d808a07c24029e961e32bb25c474afd1"
TAGS = ("DT_MARKET", "MM_TRANSITION", "MM_ALERT_INTENT")
FRAMES = (5, 15, 30)


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def demand(condition, message):
    if not condition:
        raise ValueError(message)


def build(output, start, end):
    demand(digest(BASE) == BASE_SHA, "unexpected release source")
    spec = importlib.util.spec_from_file_location("unified_probes", RELEASE / "qa_probes.py")
    qa = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(qa)
    output.mkdir(parents=True, exist_ok=True)
    for name, source in (("baseline", BASE), ("candidate", CANDIDATE)):
        text = qa.probe(source.read_text(encoding="utf-8"), qa.utc_ms(start), qa.utc_ms(end), 1200, False)
        if name == "candidate":
            text = qa.set_default(text, "mmQa", "false", "true")
            text = qa.set_default(text, "mmNotify", "false", "true")
        qa.save(output / f"{name}.pine", text)


def rows(path):
    result = {tag: [] for tag in TAGS}
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        demand(reader.fieldnames == ["Date", "Message"], f"{path.name}: unexpected header")
        for row in reader:
            for line in row["Message"].splitlines():
                tag, sep, payload = line.partition("|")
                if sep and tag in result:
                    result[tag].append(json.loads(payload))
    return result


def timestamp(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def research_checks(journal, frame):
    bars = {bar["time_close"] for bar in journal["DT_MARKET"]}
    transitions, intents = journal["MM_TRANSITION"], journal["MM_ALERT_INTENT"]
    demand(bool(transitions) and bool(bars), f"M{frame}: missing lifecycle or native bars")
    events = {}
    states, sources = Counter(), Counter()
    for event in transitions:
        event_id, known = event["event_id"], event["known_ms"]
        state, direction = event["state"], event["direction"]
        demand(event_id not in events and bool(event_id), f"M{frame}: duplicate or empty event ID {event_id!r}")
        demand(known in bars, f"M{frame}: transition does not match a native bar close: {event_id}")
        demand(event["symbol"] == "OANDA:XAUUSD" and str(event["timeframe"]) == str(frame),
               f"M{frame}: wrong symbol/interval: {event_id}")
        demand(direction in ("BUY", "SELL", 1, -1), f"M{frame}: missing direction: {event_id}")
        utc = datetime.fromisoformat(event["confirmation_utc"].replace("Z", "+00:00"))
        demand(utc == timestamp(known), f"M{frame}: display timestamp mismatch: {event_id}")
        for key in ("origin_ms", "level_known_ms"):
            demand(event.get(key) is None or event[key] <= known, f"M{frame}: future {key}: {event_id}")
        demand(event.get("observation_only") is True and state != "RISK APPROVED" and
               event.get("risk_status") != "APPROVED", f"M{frame}: research granted approval: {event_id}")
        for key in ("reference", "sl", "tp1", "tp2", "tp3"):
            demand(event.get(key) is None or isinstance(event[key], (float, int)),
                   f"M{frame}: invalid {key}: {event_id}")
        if state == "ENTRY CANDIDATE" and event.get("reference") is not None and event.get("sl") is not None:
            is_buy = direction in ("BUY", 1)
            demand((event["reference"] > event["sl"]) if is_buy else (event["reference"] < event["sl"]),
                   f"M{frame}: stop on wrong side: {event_id}")
        events[event_id] = event
        states[state] += 1
        sources[event["source"]] += 1
    alert_ids = set()
    for intent in intents:
        event_id = intent["event_id"]
        demand(event_id not in alert_ids, f"M{frame}: duplicate early intent: {event_id}")
        alert_ids.add(event_id)
        original = events.get(event_id)
        demand(original is not None and "EARLY" in original["state"], f"M{frame}: alert without early event: {event_id}")
        demand(intent["known_ms"] == original["known_ms"], f"M{frame}: alert/transition time mismatch: {event_id}")
    observed = sorted(events.values(), key=lambda event: event["known_ms"])
    return {
        "status": "VERIFIED_HISTORICAL_CLOSED_BAR_JOURNAL",
        "transitions": len(transitions), "states": dict(sorted(states.items())), "sources": dict(sorted(sources.items())),
        "alert_intents": len(intents), "chart_bars": len(bars), "first_transition_utc": timestamp(observed[0]["known_ms"]).isoformat(),
        "last_transition_utc": timestamp(observed[-1]["known_ms"]).isoformat(),
        "scope": "Native bar-close timestamp and payload consistency; no server delivery claim.",
    }


def compare(directory, frame):
    left, right = (directory / f"{name}-M{frame}.csv" for name in ("baseline", "candidate"))
    if not left.is_file() or not right.is_file():
        return {"status": "MISSING_EVIDENCE", "files": [path.name for path in (left, right) if not path.is_file()]}
    core = native_qa.verify_pair(left, right, frame, "indicator")
    shadow = native_qa.verify_shadow(left, right, frame)
    journal = rows(right)
    research = research_checks(journal, frame)
    legacy = directory / f"early-M{frame}.csv"
    if legacy.is_file():
        old = set()
        with legacy.open(encoding="utf-8-sig", newline="") as stream:
            for row in csv.DictReader(stream, strict=True):
                for line in row["Message"].splitlines():
                    if line.startswith("RC2_EARLY|"):
                        _, version, symbol, interval, known, side, _ = line.split("|")
                        demand((version, symbol, interval) == ("1", "OANDA:XAUUSD", str(frame)),
                               f"M{frame}: invalid legacy EARLY stream")
                        old.add((int(known), side))
        new = {(item["known_ms"], item["direction"]) for item in journal["MM_TRANSITION"]
               if item["source"] == "RC2 EARLY" and item["state"] == f"EARLY {item['direction']} WATCH"}
        research["legacy_early"] = {"status": "MATCH" if old == new else "MISMATCH",
                                    "legacy": len(old), "candidate": len(new),
                                    "legacy_only": sorted(old - new), "candidate_only": sorted(new - old)}
    else:
        research["legacy_early"] = {"status": "MISSING_EVIDENCE"}
    return {
        "status": "MATCH" if (core["status"], shadow["status"]) == ("MATCH", "MATCH") and research["legacy_early"]["status"] == "MATCH" else "NONIDENTICAL_OR_UNVERIFIED",
        "baseline_log_sha256": digest(left), "candidate_log_sha256": digest(right),
        "s5_rc2": core, "direct_trend": shadow, "research": research,
    }


def main():
    parser = ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--build", type=Path, metavar="DIRECTORY")
    group.add_argument("--logs", type=Path, metavar="DIRECTORY")
    parser.add_argument("--start", default="2026-11-11T00:00:00Z")
    parser.add_argument("--end", default="2026-11-11T17:30:00Z")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.build:
        build(args.build, args.start, args.end)
        return 0
    if not args.output:
        parser.error("--logs requires --output")
    demand(digest(BASE) == BASE_SHA and digest(SEAL) == SEAL_SHA, "protected release or forward seal changed")
    evidence = {"source_sha256": digest(CANDIDATE), "baseline_sha256": digest(BASE), "forward_seal_sha256": digest(SEAL),
                "frames": {}}
    for frame in FRAMES:
        try:
            evidence["frames"][f"M{frame}"] = compare(args.logs, frame)
        except (ValueError, KeyError, OSError, csv.Error) as error:
            evidence["frames"][f"M{frame}"] = {"status": "INVALID_OR_INCOMPLETE_EVIDENCE", "reason": str(error)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return int(any(item["status"] != "MATCH" for item in evidence["frames"].values()))


if __name__ == "__main__":
    raise SystemExit(main())

