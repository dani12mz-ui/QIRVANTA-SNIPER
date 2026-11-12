"""Compare native companion context with the frozen source events, without replaying approvals."""
import argparse
from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
from pathlib import Path

from gold_pro.rc1_qa import SYMBOL
from research.direct_trend_breakout.forensic_timeline import utc
from research.direct_trend_visibility.native_qa import verify_visibility
from research.professional_signal_selector import native_qa as baseline_qa
from research.professional_signal_selector import observations as selector

HERE = Path(__file__).resolve().parent
COMPANION = HERE / "QIRVANTA_CONFIRMED_HTF.pine"
PINNED_PARENT = "91f2f7687fd07f836cad3dfad145721a5c8f9cc74207b1db00afc60f6cb86b4c"
TAG = "HTF_CANON|"
LIMITS = (7_200_000, 28_800_000)
OHLC = ("open", "high", "low", "close")


def check(condition, message):
    if not condition:
        raise ValueError(message)


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def source_qa():
    check(digest(baseline_qa.CANDIDATE) == PINNED_PARENT, "Selector parent SHA-256 mismatch")
    baseline_qa.source_checks()
    source = COMPANION.read_text(encoding="utf-8")
    check(source.count("request.security(") == 2 and "barstate.isconfirmed ? packet" in source,
          "Unexpected companion data path")
    check(all(token not in source for token in ("strategy.", "alert(", "alertcondition(")),
          "Companion must not execute or emit alerts")
    return {"protected_selector_sha256": PINNED_PARENT, "companion_sha256": digest(COMPANION),
            "forward_seal_sha256": digest(baseline_qa.FORWARD_SEAL),
            "protected_source_changed": False, "standalone_request_calls": 2}


def number(value):
    return None if value == "null" else Decimal(value)


def timestamp(value):
    return None if value == "null" else int(value)


def side(prices):
    close, fast, slow = (x.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP) for x in prices)
    return 1 if close > fast > slow else -1 if close < fast < slow else 0


def known_side(source, opening, max_age):
    stamp, close, fast, slow = source
    if stamp is None or not 0 <= opening - stamp <= max_age or None in (close, fast, slow):
        return None
    return side((close, fast, slow))


def snapshots(path, frame):
    out = {}
    for line in baseline_qa.log_messages(path):
        if not line.startswith(TAG):
            continue
        parts = line.split("|")
        check(len(parts) == 19, f"M{frame}: invalid HTF journal field count")
        _, chart, opening, closing, *fields = parts
        opening, closing = int(opening), int(closing)
        check(int(chart) == frame and closing - opening == frame * 60_000 and closing not in out,
              f"M{frame}: invalid chart clock")
        tuples = []
        for index in (0, 5):
            t, c, a, b, state = fields[index:index+5]
            record = (timestamp(t), number(c), number(a), number(b), timestamp(state))
            check(record[0] is None or record[0] <= opening, f"M{frame}: future HTF source")
            check(record[4] == known_side(record[:4], opening, LIMITS[index // 5]),
                  f"M{frame}: unexpected canonical state at {utc(closing)}")
            tuples.append(record)
        packet = closing * 32 + 8
        for scale, row in zip((4, 1), tuples):
            packet += scale * (3 if row[4] is None else row[4] + 1)
        check(packet == int(fields[10]), f"M{frame}: invalid same-close packet")
        out[closing] = {"open": opening, "canonical": tuples, "packet": packet,
                        "ohlc": tuple(Decimal(x) for x in fields[11:])}
    check(tuple(out) == baseline_qa.native_window(frame), f"M{frame}: incomplete frozen journal window")
    return out


def cross_frame(contexts):
    common = {}
    for i, x in enumerate(baseline_qa.FRAMES):
        for y in baseline_qa.FRAMES[i+1:]:
            pairs = [0, 0]
            times = contexts[x].keys() & contexts[y].keys()
            for t in sorted(times):
                for j, (one, two) in enumerate(zip(contexts[x][t]["canonical"], contexts[y][t]["canonical"])):
                    if one[0] == two[0] and one[4] is not None and two[4] is not None:
                        check(one == two, f"M{x}/M{y}: same-source H{1 if j == 0 else 4} difference at {utc(t)}")
                        pairs[j] += 1
            common[f"M{x}/M{y}"] = {"same_chart_closes": len(times), "exact_H1": pairs[0], "exact_H4": pairs[1]}
    return common


def attach_observation_context(path, frame, contexts):
    tape = selector.source_tape(path, frame)
    markets = {bar["time_close"]: bar for bar in tape.market if bar["time_close"] in contexts}
    for stamp, context in contexts.items():
        bar = markets[stamp]
        check(bar["time"] == context["open"] and tuple(Decimal(str(bar[key])) for key in OHLC) == context["ohlc"],
              f"M{frame}: native companion and protected chart OHLC differ at {utc(stamp)}")
    rules = json.loads(baseline_qa.PROTOCOL.read_bytes())["policies"]["shared"]
    rows = selector.normalize(tape, {}, rules)
    confirmed = {}
    for row in rows:
        sides = confirmed.setdefault(row["known_at_ms"], set())
        sides.update(item["side"] for item in row["context"]["active_observation_sources"]
                     if item["stage"] == "BREAKOUT CONFIRMED")
        if row["confirmed_observation"]:
            sides.add(1 if row["direction"] == "BUY" else -1)
    for row in rows:
        stamp = row["known_at_ms"]
        h1, h4 = contexts[stamp]["canonical"]
        original_side = 1 if row["direction"] == "BUY" else -1
        biases = (h1[4], h4[4])
        alignment = "DATA MISSING" if None in biases else (
            "ALIGNED" if biases == (original_side, original_side) else
            "COUNTERTREND" if biases == (-original_side, -original_side) else "MIXED")
        row["htf"] = {
            "status": alignment, "source": "Independent native companion on matching OHLC at the observation close",
            **{name: {"source_close_utc": utc(item[0]) if item[0] is not None else None, "side": item[4],
                      "source_price": item[1], "ema21": item[2], "ema55": item[3]}
               for name, item in zip(("h1", "h4"), (h1, h4))},
        }
        row["triage"], row["triage_reason"] = selector.triage(row, confirmed[stamp], rules)
    return rows


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8", newline="\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-dir", type=Path, required=True, help="Frozen baseline and native companion CSVs")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--observations-output", type=Path)
    args = parser.parse_args()
    frames, observations, contexts = {}, {}, {}
    for frame in baseline_qa.FRAMES:
        label = f"M{frame}"
        baseline = args.native_dir / f"baseline-{label}.csv"
        companion = args.native_dir / f"companion-{label}.csv"
        contexts[frame] = snapshots(companion, frame)
        observations[label] = attach_observation_context(baseline, frame, contexts[frame])
        protected = verify_visibility(baseline, frame)
        check(protected["status"] == "VERIFIED", f"M{frame}: protected source journal failed validation")
        frames[label] = {"baseline_sha256": digest(baseline), "companion_sha256": digest(companion),
                         "same_local_ohlc_closes": len(contexts[frame]),
                         "canonical_sides": [dict(Counter(str(row["canonical"][i][4])
                                                        for row in contexts[frame].values())) for i in (0, 1)],
                         "observations": len(observations[label]),
                         "triage": dict(Counter(row["triage"] for row in observations[label])),
                         "protected_reference_journal": protected}
    if args.observations_output:
        write_json(args.observations_output, observations)
    write_json(args.output, {
        "symbol": SYMBOL, "source": source_qa(), "canonical_cross_frame": cross_frame(contexts), "frames": frames,
        "scope": "Canonical standalone compiled natively. Protected parent was compiled separately. "
                 "Observation join is offline and does not change protected S5/RC2 native decisions.",
        "combined_native_link": "BLOCKED_BY_COMPILATION_AND_BASIC_INDICATOR_LIMIT",
    })


if __name__ == "__main__":
    main()

