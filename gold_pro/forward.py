"""Seal and score externally collected campaign tapes without changing trading rules."""

import argparse
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

from .archive import _ms, _read_json, _write, REGISTRATION
from .evaluate import Approval, Campaign, ForwardEvidence, Lane, Study, evaluate, registered_protocol_sha256


def lanes_from(bundle, with_campaigns):
    lanes = []
    for row in bundle["lanes"]:
        values = dict(row)
        tape = values.pop("campaigns", [])
        if with_campaigns:
            values["campaigns"] = [Campaign(**entry) for entry in tape]
        else:
            values["campaigns"] = ()
            values.pop("opportunities", None)
            values.pop("off_event_observations", None)
        lanes.append(Lane(**values))
    if not lanes:
        raise ValueError("A frozen reference and candidate metadata are required")
    study_values = dict(bundle["study"])
    if study_values.pop("forward", None) is not None:
        raise ValueError("Forward provenance is supplied through separate receipts")
    study_values["registered_candidate_ids"] = tuple(study_values["registered_candidate_ids"])
    study = Study(**study_values)
    reference = lanes[0]
    if reference.id != study.reference_id or tuple(l.id for l in lanes[1:]) != study.registered_candidate_ids:
        raise ValueError("Lane family differs from the ordered registration")
    if not study.rules_sha256 or any(not lane.source_sha256 for lane in lanes):
        raise ValueError("The exact rules and source fingerprints are required")
    return study, reference, lanes[1:]


def bounds(bundle, study):
    a, b = bundle["forward_start_ms"], bundle["forward_end_ms"]
    registry = _read_json(REGISTRATION)
    earliest = _ms(registry["holdout"]["first_eligible_utc"])
    if (a < earliest or study.registered_at_ms >= a or study.last_inspected_ms >= a
            or study.validation_start_ms >= a or b > study.end_ms
            or b - a < registry["holdout"]["minimum_days"] * 86_400_000):
        raise ValueError("Invalid or insufficiently separated forward window")
    return a, b


def seal(bundle):
    study, reference, alternatives = lanes_from(bundle, False)
    a, b = bounds(bundle, study)
    if any(lane.get("campaigns") or lane.get("opportunities") for lane in bundle["lanes"]):
        raise ValueError("Register without outcomes or observed events")
    return {"schema": "qirvanta.gold-pro.forward-seal.v1", "study_id": study.study_id,
            "protocol_sha256": registered_protocol_sha256(study, reference, alternatives, a, b),
            "rules_sha256": study.rules_sha256, "forward_start_ms": a, "forward_end_ms": b,
            "registered_at_ms": study.registered_at_ms,
            "operator_action": "Record this seal with an independent timestamp before data access."}


def _digest(path):
    return sha256(path.read_bytes()).hexdigest()


def score(bundle, seal_path, access_path, market_path, approval_path=None):
    study, reference, alternatives = lanes_from(bundle, True)
    a, b = bounds(bundle, study)
    plan = _read_json(seal_path)
    access = _read_json(access_path)
    protocol = registered_protocol_sha256(study, reference, alternatives, a, b)
    if (plan["schema"] != "qirvanta.gold-pro.forward-seal.v1" or plan["protocol_sha256"] != protocol
            or plan["registered_at_ms"] != study.registered_at_ms):
        raise ValueError("Study or lane rules differ from the recorded seal")
    seal_hash = _digest(seal_path)
    market = _digest(market_path)
    if (market != study.data_sha256 or market != access["data_sha256"]
            or access["seal_sha256"] != seal_hash or access["protocol_sha256"] != protocol
            or not access.get("source") or not access.get("registration_receipt_id")
            or not access.get("access_receipt_id")):
        raise ValueError("Missing provenance or access receipt does not match the data and seal")
    observed = ForwardEvidence(a, b, access["first_access_ms"], study.rules_sha256, market,
                               seal_hash, _digest(access_path), protocol)
    approval = Approval(**_read_json(approval_path)) if approval_path else None
    return evaluate(reference, alternatives, replace(study, forward=observed), approval)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(required=True, dest="action")
    s = sub.add_parser("seal", help="Hash the predeclared study before reading future data")
    s.add_argument("--bundle", required=True, type=Path)
    s.add_argument("--out", required=True, type=Path)
    r = sub.add_parser("score", help="Score costed, frozen outcome tapes with external provenance")
    r.add_argument("--bundle", required=True, type=Path)
    r.add_argument("--seal", required=True, type=Path)
    r.add_argument("--access-receipt", required=True, type=Path)
    r.add_argument("--market", required=True, type=Path)
    r.add_argument("--approval", type=Path)
    r.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    bundle = _read_json(args.bundle)
    result = (seal(bundle) if args.action == "seal" else
              score(bundle, args.seal, args.access_receipt, args.market, args.approval))
    _write(args.out, result)


if __name__ == "__main__":
    main()
