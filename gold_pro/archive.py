"""Run the frozen Gold Pro archive experiments and preserve the original S5 tape."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import asdict, fields, replace
from datetime import datetime
import gzip
from hashlib import sha256
import json
from pathlib import Path
from statistics import mean

from research.data.loader import load_bars
from research.validation.statistics import holm
from unified.tools.audit_premium_s5 import verify_bars
from unified.tools.audit_tv_observations import inspect
from unified.tools.evaluate_v22_signals import feature_rows
from unified.tools.gold_market_data import active_sessions, context_rows

from .evaluate import Campaign, Lane, Opportunity, Study, evaluate
from .opportunity import event_audit, scan
from .replay import Costs, Outcome, Part, Plan, buffered, comparable_units, portfolio

ROOT = Path(__file__).resolve().parents[1]
REGISTRATION = Path(__file__).with_name("registration.json")
COST_ID = "OANDA_2_TICKS_0175_COMMISSION_035_SPREAD_WITH_STRESS"
RISK_ID = "THREE_UNIT_CEILING_100K_025_PERCENT_RISK_ONE_SLOT"
OPPORTUNITY_FIELDS = {f.name for f in fields(Opportunity)}


def _ms(text):
    return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)


def _hash(path):
    return sha256(path.read_bytes()).hexdigest()


def _read(path, digest):
    if _hash(path) != digest:
        raise ValueError(f"Changed pinned input: {path.name}")
    return path


def _read_json(path):
    return json.loads(path.read_bytes())


def _write(path, value, *, compressed=False):
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                          indent=None if compressed else 2) + "\n").encode()
    with path.open("xb") as stream:
        stream.write(gzip.compress(payload, compresslevel=9, mtime=0) if compressed else payload)


def load_archive(config, data_root, logs_dir):
    registered = _read_json(_read(ROOT / config["s5_pin"], config["s5_pin_sha256"]))
    frames = {}
    for tf, item in registered["frames"].items():
        market = data_root / Path(item["local_bars"]).parent.name / "bars.csv"
        journal = logs_dir / item["log_name"]
        bars = load_bars(_read(market, item["bars_sha256"]))
        original = inspect(_read(journal, item["log_sha256"]))
        run = original["run"]
        if original["mode"] != "S5" or original["unfilled"] or run["timeframe"] != tf:
            raise ValueError(f"Invalid S5 control: {tf}")
        verify_bars(bars, run)
        frames[int(tf)] = bars, original, item["bars_sha256"]
    return frames


def native_outcomes(raw, costs):
    outcomes = []
    for campaign in raw["campaigns"]:
        if campaign["state"] not in {"COMPLETE", "OPEN"}:
            raise ValueError(f"Unsupported native campaign state: {campaign['state']}")
        meta = campaign["submitted_metadata"]
        plan = Plan(campaign["plan_id"], "S5", campaign["side"], meta["submitted_bar"],
                    float(meta["reference"]), float(meta["initial_sl"]),
                    tuple(float(meta[f"tp{leg}"]) for leg in range(1, 4)),
                    float(meta["atr"]), int(meta["maxhold"]), float(campaign["submitted_quantity"]),
                    meta.get("regime"))
        exits = []
        for leg in campaign["exit_rows"]:
            units = abs(float(leg["size"]))
            spread = units * costs.spread * costs.pointvalue
            exits.append(Part(leg["exit_bar_index"], float(leg["exit_price"]), units,
                              leg["exit_comment"], float(leg["commission"]), float(leg["net_cash"]) - spread))
        closed = campaign["state"] == "COMPLETE"
        if closed and (not exits or abs(sum(leg.units for leg in exits) - plan.units) > 1e-8):
            raise ValueError(f"Native close does not reconcile to its original quantity: {plan.id}")
        outcomes.append(Outcome(plan, "CLOSED" if closed else "CENSORED_OPEN",
                                exits[-1].bar if closed else raw["run"]["bar_count"],
                                campaign["entry_bar_index"], float(campaign["actual_entry"]), tuple(exits)))
    return outcomes


def cash_campaign(outcome, bars, costs, native=False):
    plan = outcome.plan
    risk_price = plan.side * (outcome.fill - plan.stop)
    # Invalid gaps can put the fill beyond the original stop. Retain the entire
    # realized loss, normalized to the budget known at the submitting close.
    if risk_price <= 0:
        risk_price = plan.side * (plan.reference - plan.stop)
    if risk_price <= 0:
        raise ValueError(f"Missing initial risk: {plan.id}")
    exit_bar = outcome.release
    fee = sum(part.commission for part in outcome.exits)
    base_spread = costs.spread * costs.pointvalue * plan.units
    stress = ((costs.extra_spread + 2 * costs.extra_slip) * costs.pointvalue * plan.units
              + fee * costs.extra_commission)
    edge = "high" if plan.side > 0 else "low"
    observed = [outcome.fill, *(leg.price for leg in outcome.exits)]
    observed.extend(bars[i][edge] for i in range(outcome.entry_bar + 1, exit_bar))
    mfe = max(plan.side * (price - outcome.fill) for price in observed) * plan.units * costs.pointvalue
    slippage_units = plan.units + sum(leg.units for leg in outcome.exits if not leg.reason.startswith("TP"))
    fees_and_spread_and_slip = fee + base_spread + slippage_units * costs.slip * costs.pointvalue
    return Campaign(plan.id, bars[outcome.entry_bar]["time"], bars[exit_bar]["time_close"],
                    risk_price * plan.units * costs.pointvalue, outcome.net, outcome.net - stress, mfe,
                    fees_and_spread_and_slip, plan.side, plan.model,
                    exposure=plan.units * outcome.fill * costs.pointvalue,
                    native_net_cash=outcome.net + base_spread if native else None)


def native_lane(alternative, original, bars, costs):
    admitted, rejected = [], []
    for outcome in original:
        plan = outcome.plan
        if alternative["axis"] == "entry_selection":
            bar = bars[plan.bar]
            if bar["high"] - bar["low"] > alternative["parameter"] * plan.atr:
                rejected.append((plan, "WIDE_RECLAIM_BAR"))
                continue
        if alternative["axis"] == "sizing":
            estimated = (abs(plan.reference - plan.stop) + costs.spread + 2 * costs.slip) * costs.pointvalue + 2 * costs.commission
            units = comparable_units(plan.units, alternative["parameter"] / plan.units, estimated)
            if units < .03:
                rejected.append((plan, "SUB_MINIMUM_RISK_SIZE"))
                continue
            scale = units / plan.units
            parts = tuple(replace(leg, units=leg.units * scale, commission=leg.commission * scale,
                                  net=leg.net * scale) for leg in outcome.exits)
            outcome = replace(outcome, plan=replace(plan, units=units), exits=parts)
        admitted.append(outcome)
    return admitted, rejected


def replay_lane(alternative, plans, bars, costs, minutes, equity):
    execution = {}
    if alternative is not None:
        axis, code = alternative["axis"], alternative["id"]
        if axis == "execution":
            execution = {"method": "limit" if "LIMIT" in code else "stop",
                         "entry_amount": alternative["parameter"]}
        elif axis == "invalidation":
            plans = [buffered(plan, alternative["parameter"], costs) for plan in plans]
        elif axis == "exit":
            execution = {"exit_policy": {"EXIT_COST_BE": "be", "EXIT_LOCK_005": "lock", "EXIT_STALL_24": "stall"}[code]}
    results, rejects = portfolio(plans, bars, costs, minutes, equity, **execution)
    return plans, results, rejects


def _s5_decisions(plans, bars):
    return [{"time_ms": bars[p.bar]["time_close"], "side": p.side, "state": "TREND_CONFIRMED",
             "reason": "ARCHIVED_S5_ADMISSION", "model": "S5"} for p in plans]


def lane(id_, source, bars, plans, outcomes, rejects, observations, event_cfg, minutes, costs,
         market_hash, *, axis, params=None, native=False):
    completed = [cash_campaign(o, bars, costs, native=native and o.plan.model == "S5")
                 for o in outcomes if o.state == "CLOSED"]
    counts = Counter(reason for _, reason in rejects)
    counts.update(row["reason"] for row in observations if row["state"] == "RISK_REJECTED")
    counts.update(o.state for o in outcomes if o.entry_bar is None and o.state in {"ENTRY_EXPIRED", "CENSORED_PENDING"})
    events, outside, details = event_audit(bars, minutes, event_cfg, observations, plans, outcomes, rejects)
    # Classify closed observations by known entry-time session, including UTC DST changes.
    by_session, by_regime = defaultdict(list), defaultdict(list)
    regimes = {o.plan.id: o.plan.regime for o in outcomes}
    for row in completed:
        session = active_sessions(row.entry_ms)
        label = next((x for x in ("LONDON–NEW YORK", "NEW YORK", "LONDON", "ASIA") if x in session),
                     "OUTSIDE_RESEARCH_WINDOWS")
        by_session[label].append(row)
        by_regime[regimes[row.id] or "UNKNOWN"].append(row)

    def breakdown(groups):
        return {key: {"n": len(rows), "mean_net_r": mean(r.net_cash / r.risk_cash for r in rows),
                      "net_cash": sum(r.net_cash for r in rows), "losses": sum(r.net_cash < 0 for r in rows)}
                for key, rows in groups.items()}
    value = Lane(id_, completed, market_hash, COST_ID, RISK_ID,
                 blocked_count=counts["POSITION_OCCUPIED"], rejected_count=sum(counts.values()) - counts["POSITION_OCCUPIED"],
                 axis=axis, parameters=params or {}, source_sha256=source,
                 opportunities=[{k: v for k, v in e.items() if k in OPPORTUNITY_FIELDS} for e in events],
                 off_event_observations=outside)
    detail = {"reasons": dict(counts), "session": breakdown(by_session), "regime": breakdown(by_regime),
              "lifecycle": dict(Counter(x["state"] for x in observations)), "event_summary": details,
              "censored_open_or_pending": sum(o.state.startswith("CENSORED") for o in outcomes)}
    tape = {"events": events, "observations": observations, "outcomes": [asdict(o) for o in outcomes],
            "portfolio_rejections": [{"id": p.id, "bar": p.bar, "reason": r} for p, r in rejects]}
    return value, detail, tape


def _audit_passed(path):
    report = _read_json(path)
    if (report.get("status") != "VERIFIED_NATIVE_CONTEXT" or report.get("symbol") != "OANDA:XAUUSD"
            or set(report.get("frames", {})) != {"5", "15", "30"}):
        raise ValueError("Three paired canonical context exports are required")
    if any(x["bars"] < 100 or not x["eligible_bars"] for x in report["frames"].values()):
        raise ValueError("Insufficient paired canonical context evidence")
    if not all(x["fingerprints_recomputed"] for x in report["frames"].values()):
        raise ValueError("Provide a context window with independently recomputed fingerprints on every frame")
    return _hash(path)


def run(config, frames, audit_digest):
    registry_digest = _hash(REGISTRATION)
    source_paths = [Path(__file__), Path(__file__).with_name("evaluate.py"),
                    Path(__file__).with_name("opportunity.py"), Path(__file__).with_name("replay.py"),
                    ROOT / "unified/tools/gold_market_data.py"]
    sources = {path.relative_to(ROOT).as_posix(): _hash(path) for path in source_paths}
    source_digest = sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
    v22 = _read_json(ROOT / "docs/v22_evidence/historical_registration.json")
    event_cfg = _read_json(ROOT / config["opportunity"]["event_reference"])["event"]
    c = config["cost"]
    costs = Costs(c["tick"], c["pointvalue"], c["commission_per_unit_per_fill"],
                  c["adverse_ticks_market_and_stop"], c["round_trip_spread"],
                  c["stress_extra_round_trip_spread"], c["stress_extra_slip_each_side"], c["stress_commission_fraction"])
    equity = config["study"]["starting_equity_usd"]
    archive, full_tapes = {}, {}
    old_m30 = frames[30][0]
    cutoffs = [b["time_close"] for b in old_m30]

    for minutes in config["frames"]:
        bars, original, market_hash = frames[minutes]
        first, end = bars[0]["time"], bars[-1]["time_close"] + 1
        prior = old_m30[:bisect_right(cutoffs, first)]
        context = [(r.h1.side, r.h4.side) for r in context_rows(bars, tick=costs.tick, earlier=prior)]
        features = feature_rows(bars, v22["signals"], earlier=prior)
        raw_s5 = native_outcomes(original, costs)
        s5_plans = [x.plan for x in raw_s5]
        s5_events = _s5_decisions(s5_plans, bars)
        values, details, tapes = {}, {}, {}

        def add(name, plans, results, rejects, observations, axis, params=None, native=False, pin=None):
            value, detail, tape = lane(name, pin or source_digest, bars, plans, results, rejects,
                                       observations, event_cfg, minutes, costs, market_hash,
                                       axis=axis, params=params, native=native)
            values[name], details[name], tapes[name] = value, detail, tape

        add("S5", s5_plans, raw_s5, [], s5_events, "reference", native=True, pin=original["run"]["source_sha256"])
        p, outcomes, rejected = replay_lane(None, s5_plans, bars, costs, minutes, equity)
        add("S5_REPLAY_CONTROL", p, outcomes, rejected, s5_events, "reference")
        for alternative in config["alternatives"]:
            label = alternative["id"]
            if alternative["family"] == "native":
                results, rejected = native_lane(alternative, raw_s5, bars, costs)
                p = [r.plan for r in results]
                add(label, p, results, rejected, _s5_decisions(p, bars), alternative["axis"], alternative, native=True)
            else:
                p, results, rejected = replay_lane(alternative, s5_plans, bars, costs, minutes, equity)
                add(label, p, results, rejected, s5_events, alternative["axis"], alternative)

        frozen = {x.plan.id: x for x in raw_s5}
        for timing in ("FAST", "CONFIRMED"):
            setup = config["opportunity"]
            for subset in [None, *((m,) for m in setup["models"])]:
                decisions, plans = scan(bars, features, context, setup, costs, equity, mode=timing, enabled=subset)
                outcomes, rejected = portfolio(plans, bars, costs, minutes, equity)
                name = timing if subset is None else f"{timing}_{subset[0]}"
                add(name, plans, outcomes, rejected, decisions, "opportunity", {"policy": timing, "models": subset})
                if subset is None:
                    shared = s5_plans + plans
                    outcomes, rejected = portfolio(shared, bars, costs, minutes, equity, frozen=frozen)
                    add(f"S5_PLUS_{timing}", shared, outcomes, rejected, s5_events + decisions,
                        "opportunity_overlay", {"policy": timing, "S5_same_bar_priority": True}, native=True)

        reports = {}
        listed = config["alternatives"]
        for family, ids in {
            "native": [x["id"] for x in listed if x["family"] == "native"],
            "replay": [x["id"] for x in listed if x["family"] == "replay"],
            "opportunity": config["opportunity"]["promotion_comparison"],
            "diagnostic_models": [k for k in values if k.startswith(("FAST_", "CONFIRMED_"))],
        }.items():
            ref = "S5_REPLAY_CONTROL" if family == "replay" else "S5"
            spec = Study(first, end, _ms(config["registered_utc"]), _ms(config["last_inspected_utc"]),
                         bars[int(len(bars) * config["study"]["archive_development_fraction"])]["time"],
                         config["study"]["fold_days"], market_hash,
                         COST_ID, RISK_ID, ref, f"gold-pro-M{minutes}-{family}", source_digest, tuple(ids),
                         primary=minutes == config["primary_minutes"] and family != "diagnostic_models",
                         embargo_ms=config["holdout"]["embargo_minutes"] * 60_000,
                         min_campaigns=config["study"]["min_campaigns"],
                         min_forward_weeks=config["holdout"]["minimum_days"] // 7)
            reports[family] = evaluate(values[ref], [values[k] for k in ids], spec)
        archive[str(minutes)] = {"reports": reports, "details": details, "market_sha256": market_hash,
                                 "original_native_s5": {"log_sha256": original["log_sha256"],
                                  "campaigns": len([o for o in raw_s5 if o.state == "CLOSED"]),
                                  "closed_net_usd": sum(x.native_net_cash for x in values["S5"].campaigns),
                                  "native_dd_usd": float(original["native_equity_drawdown"]["value"]),
                                  "fingerprints": [original["run"]["checksum1"], original["run"]["checksum2"]]},
                                 "context_ready_bars": sum(a is not None and b is not None for a, b in context)}
        full_tapes[str(minutes)] = tapes
    primary = archive[str(config["primary_minutes"])]["reports"]
    raw_tests = [(cid, x["paired_week_bootstrap"]["p_one_sided"]) for name in ("native", "replay", "opportunity")
                 for cid, x in primary[name]["candidates"].items()]
    global_holm = dict(zip([k for k, _ in raw_tests], holm([p for _, p in raw_tests])))
    return {"schema": "qirvanta.gold-pro.archive-research.v1", "decision": "RETAIN_S5",
            "scope": "ALREADY_INSPECTED_ARCHIVE", "holdout": "BLOCKED_NO_SEALED_FORWARD_EVIDENCE",
            "registration_sha256": registry_digest, "research_sources": sources,
            "native_context_audit_sha256": audit_digest, "primary_holm_all_registered_m5": global_holm,
            "frames": archive}, full_tapes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--logs-dir", type=Path, required=True)
    parser.add_argument("--context-audit", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--tape", type=Path, required=True)
    args = parser.parse_args()
    if args.out.exists() or args.tape.exists():
        parser.error("Choose new paths for immutable output")
    config = _read_json(REGISTRATION)
    context_digest = _audit_passed(args.context_audit)
    frames = load_archive(config, args.data_root, args.logs_dir)
    report, tapes = run(config, frames, context_digest)
    _write(args.tape, tapes, compressed=True)
    report["private_tape_sha256"] = _hash(args.tape)
    _write(args.out, report)
    print(f"{report['decision']} across {len(frames)} frames, no untouched forward sample")


if __name__ == "__main__":
    main()
