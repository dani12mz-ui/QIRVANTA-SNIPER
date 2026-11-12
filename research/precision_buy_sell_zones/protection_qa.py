"""Verify that the published companion leaves the pinned TradingView control intact."""
import argparse
from collections import Counter
import csv
from decimal import Decimal
import io
from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZipFile

from gold_pro import rc1_qa, rc2_qa
from research.final_htf_data_integrity import qa as protected
from research.professional_signal_selector import native_qa as paired


COMPANION_SHA = "acfaf5ac3fd80f08efd7f179cd3802eb41043f957a70800c3444595bf5ebc09a"
ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
PINNED_EVIDENCE = ROOT / "research/final_htf_data_integrity/evidence/native-qa.json"
PINNED_NATIVE = PINNED_EVIDENCE.with_name("native-pairs.zip")
TRADE_TAGS = ("RC1_CORE", "RC1_READY", "RC1_ALERT", "RC2_ALERT", "DTV_PAPER", "DTV_ALERT")


def sha(path):
    return sha256(path.read_bytes()).hexdigest()


def control_frame(path, frame, archive):
    run = rc1_qa.read_export(path, frame, "indicator")
    if tuple(row.closing for row in run.context) != paired.native_window(frame):
        raise ValueError(f"M{frame}: missing protected journal closes")
    events = rc2_qa.extras(path)
    prior = archive.read(f"candidate-M{frame}.csv").decode("utf-8-sig")
    old = tuple(line for row in csv.DictReader(io.StringIO(prior)) for line in row["Message"].splitlines() if line)
    new = paired.log_messages(path)

    def records(messages, tag):
        return tuple(line.split("|", 1)[1] for line in messages if line.startswith(tag + "|"))

    stream_checks = {}
    for tag in TRADE_TAGS:
        a, b = (records(messages, tag) for messages in (old, new))
        if a != b:
            raise ValueError(f"M{frame}: protected stream changed: {tag}")
        stream_checks[tag] = {"exact": True, "rows": len(b)}
    old_ledger, new_ledger = ([json.loads(line, parse_float=Decimal) for line in records(messages, "DTV_LEDGER")]
                              for messages in (old, new))
    if len(old_ledger) != len(new_ledger) or not new_ledger:
        raise ValueError(f"M{frame}: missing paper ledger")
    drift = Counter(field for a, b in zip(old_ledger, new_ledger)
                    for field in a.keys() | b.keys() if a.get(field) != b.get(field))
    nets = [ledger[-1]["net"] - ledger[0]["net"] for ledger in (old_ledger, new_ledger)]
    if abs(nets[0] - nets[1]) > Decimal("0.00000002"):
        raise ValueError(f"M{frame}: native within-window P&L differs")
    return {"sha256": sha(path), "closes": len(run.context), "core": len(run.core), "ready": len(run.ready),
            "paper_events": stream_checks["DTV_PAPER"]["rows"], "rc2_alert_batches": len(events["RC2_ALERT"]),
            "prior_archive_payloads": stream_checks, "ledger_raw_differing_fields": dict(drift),
            "ledger_start_net_r": [str(old_ledger[0]["net"]), str(new_ledger[0]["net"])],
            "net_r_change_first_to_last_close": [str(net) for net in nets]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--native-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    pine = HERE / "QIRVANTA_GOLD_PRO_PRECISION_ZONES.pine"
    after = pine.read_text(encoding="utf-8")
    pins = protected.source_report()
    forbidden = ("strategy.", "request.", "input.source(", "alertcondition(")
    if sha(pine) != COMPANION_SHA or not after.startswith("//@version=6\n") or any(
            token in after for token in forbidden):
        raise ValueError("Unexpected Pine source or connection to external trading state")
    old = json.loads(PINNED_EVIDENCE.read_text(encoding="utf-8"))
    if not old["exact"] or old["native"]["protected_parity_closes"] != 318:
        raise ValueError("Missing previously certified exact S5/RC2 parity")
    with ZipFile(PINNED_NATIVE) as archive:
        controls = {f"M{frame}": control_frame(args.native_dir / f"baseline-M{frame}.csv", frame, archive)
                    for frame in paired.FRAMES}
    report = {"mode": "INDEPENDENT_COMPANION", "compiled_companion_sha256": sha(pine),
              "protected_pine_sha256": sha(protected.CANDIDATE), "source_pins": pins,
              "prior_protected_exact_parity": {"evidence_sha256": sha(PINNED_EVIDENCE),
                                               "native_archive_sha256": sha(PINNED_NATIVE), "closes": 318},
              "fresh_protected_control": controls,
              "scope": "The companion has no S5/RC2 execution or alert path. Its fresh candles match the fresh "
                       "protected charts. The prior archive uses an older calculation origin; equal in-window "
                       "trade payloads do not imply identical cumulative ledger levels across different warmup."}
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
