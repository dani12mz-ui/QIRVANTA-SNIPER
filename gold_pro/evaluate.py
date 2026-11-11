"""Evaluate immutable, costed campaign tapes on a shared chronological calendar."""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import random
import re
from statistics import mean, median

from research.validation.statistics import cash_metrics, holm, quantile


DAY_MS = 86_400_000
WEEK_MS = 7 * DAY_MS
SHA256 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class Campaign:
    """One fully closed position, including all partial exits and charged costs."""

    id: str
    entry_ms: int
    exit_ms: int
    risk_cash: float
    net_cash: float
    stressed_net_cash: float
    mfe_cash: float | None = None
    costs_cash: float | None = None
    side: int | str | None = None
    model: str | None = None
    exposure: float | None = None
    native_net_cash: float | None = None


@dataclass(frozen=True, slots=True)
class Opportunity:
    """Independent qualifying event and this lane's causal response."""

    id: str
    time_ms: int
    side: int | str
    detected: bool
    actionable: bool
    filled: bool
    capture_fraction: float | None = None
    net_r: float | None = None


@dataclass(frozen=True, slots=True)
class OffEventObservation:
    """Known confirmation or campaign loss outside same-side events."""

    time_ms: int
    confirmations: int
    losses: int


@dataclass(frozen=True, slots=True)
class Lane:
    id: str
    campaigns: Sequence[Campaign]
    data_sha256: str
    cost_model_id: str
    risk_policy_id: str
    blocked_count: int = 0
    rejected_count: int = 0
    axis: str = "reference"
    parameters: Mapping = field(default_factory=dict)
    source_sha256: str | None = None
    opportunities: Sequence[Opportunity | Mapping] | None = None
    off_event_observations: Sequence[OffEventObservation | Mapping] | None = None


@dataclass(frozen=True, slots=True)
class ForwardEvidence:
    """Provenance attested by the operator of the pre-registered forward collection."""

    start_ms: int
    end_ms: int
    first_access_ms: int
    rules_sha256: str
    data_sha256: str
    registration_receipt_sha256: str
    access_receipt_sha256: str
    protocol_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class Approval:
    """Explicit operator approval bound to one candidate and one evidence report."""

    candidate_id: str
    study_id: str
    evidence_sha256: str
    approved_by: str
    approved_at_ms: int


@dataclass(frozen=True, slots=True)
class Study:
    start_ms: int
    end_ms: int
    registered_at_ms: int
    last_inspected_ms: int
    validation_start_ms: int
    fold_days: int
    data_sha256: str
    cost_model_id: str
    risk_policy_id: str
    reference_id: str = "S5"
    study_id: str = "gold-pro"
    rules_sha256: str | None = None
    registered_candidate_ids: tuple[str, ...] = ()
    primary: bool = True
    embargo_ms: int = 0
    max_campaign_risk_cash: float = 250.0
    min_campaigns: int = 30
    min_forward_weeks: int = 8
    min_expectancy_lift_r: float = 0.05
    min_frequency_ratio: float = 0.65
    max_drawdown_ratio: float = 1.0
    max_tail_loss_r_degradation: float = 0.05
    min_actionable_recall_ratio: float = 1.0
    min_event_fill_recall_ratio: float = 1.0
    min_event_capture_ratio: float = 1.0
    max_off_event_losses_ratio: float = 1.0
    event_duration_ms: int = 3_600_000
    alpha: float = 0.05
    bootstrap_repetitions: int = 2000
    bootstrap_weeks: int = 2
    bootstrap_seed: int = 20261111
    forward: ForwardEvidence | None = None


def _finite(value):
    return not isinstance(value, bool) and math.isfinite(float(value))


def _valid_sha(value):
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _normalize_json(value):
    """Normalize Decimal values while preserving JSON metadata and tuple order."""
    if isinstance(value, Mapping):
        return {key: _normalize_json(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_normalize_json(item) for item in value]
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    return float(value)


def _digest(value):
    encoded = json.dumps(_normalize_json(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _validate_study(study):
    counts = (study.fold_days, study.min_campaigns, study.min_forward_weeks,
              study.bootstrap_repetitions, study.bootstrap_weeks, study.event_duration_ms)
    times = (study.start_ms, study.end_ms, study.validation_start_ms, study.embargo_ms,
             study.registered_at_ms, study.last_inspected_ms)
    positive = (study.max_campaign_risk_cash, study.max_drawdown_ratio,
                study.min_expectancy_lift_r, study.min_frequency_ratio, study.alpha,
                study.min_actionable_recall_ratio, study.min_event_fill_recall_ratio,
                study.min_event_capture_ratio, study.max_off_event_losses_ratio)
    if any(type(time) is not int or time < 0 for time in times):
        raise ValueError("Study timestamps and embargo must be nonnegative integer milliseconds")
    if not study.start_ms < study.validation_start_ms < study.end_ms:
        raise ValueError("Validation must begin inside the common market window")
    if not _valid_sha(study.data_sha256) or not study.cost_model_id or not study.risk_policy_id:
        raise ValueError("Specify the common market fingerprint, cost and risk policy")
    if not study.study_id or not study.reference_id:
        raise ValueError("Specify the study and immutable reference IDs")
    if not all(type(count) is int and count > 0 for count in counts):
        raise ValueError("Evaluation window, sample and resampling counts must be positive integers")
    if (study.bootstrap_repetitions < 100 or study.embargo_ms < 0
            or not all(_finite(value) and value > 0 for value in positive)
            or not _finite(study.max_tail_loss_r_degradation)
            or study.max_tail_loss_r_degradation < 0
            or study.min_frequency_ratio > 1 or study.alpha >= 1):
        raise ValueError("Invalid risk, evidence or non-inferiority settings")
    if study.rules_sha256 is not None and not _valid_sha(study.rules_sha256):
        raise ValueError("Rules fingerprint must be a SHA-256")
    if len(study.registered_candidate_ids) != len(set(study.registered_candidate_ids)):
        raise ValueError("Registration contains duplicate candidates")
    if study.forward is not None and any(type(time) is not int or time < 0 for time in (
        study.forward.start_ms, study.forward.end_ms, study.forward.first_access_ms,
    )):
        raise ValueError("Forward timestamps must be nonnegative integer milliseconds")


def _rows(items, row_type):
    return tuple(row_type(**item) if isinstance(item, Mapping) else item for item in items or ())


def _validate_lane(lane, study):
    if (lane.data_sha256, lane.cost_model_id, lane.risk_policy_id) != (
        study.data_sha256, study.cost_model_id, study.risk_policy_id,
    ):
        raise ValueError(f"{lane.id} uses different market data, cost model or risk policy")
    if not lane.id or not lane.axis or lane.blocked_count < 0 or lane.rejected_count < 0:
        raise ValueError("Lane metadata is invalid")
    if lane.source_sha256 is not None and not _valid_sha(lane.source_sha256):
        raise ValueError(f"{lane.id} source fingerprint must be a SHA-256")
    seen = set()
    for campaign in lane.campaigns:
        if not campaign.id or campaign.id in seen:
            raise ValueError(f"{lane.id} repeats a campaign ID or contains an empty one")
        seen.add(campaign.id)
        if (type(campaign.entry_ms) is not int or type(campaign.exit_ms) is not int
                or not study.start_ms <= campaign.entry_ms <= campaign.exit_ms < study.end_ms):
            raise ValueError(f"{lane.id} contains a campaign outside the common market window")
        cash = (campaign.risk_cash, campaign.net_cash, campaign.stressed_net_cash)
        optional = (campaign.mfe_cash, campaign.costs_cash, campaign.exposure)
        if (not all(_finite(value) for value in cash) or campaign.risk_cash <= 0
                or campaign.stressed_net_cash > campaign.net_cash
                or any(value is not None and (not _finite(value) or value < 0) for value in optional)
                or campaign.native_net_cash is not None and not _finite(campaign.native_net_cash)):
            raise ValueError(f"{lane.id} contains invalid risk, net cash or cost stress")
    seen_events = set()
    for row in _rows(lane.opportunities, Opportunity):
        if (not row.id or row.id in seen_events or type(row.time_ms) is not int
                or not study.start_ms <= row.time_ms < study.end_ms
                or row.side not in (1, -1, "BUY", "SELL")
                or not all(type(value) is bool for value in (row.detected, row.actionable, row.filled))
                or row.actionable and not row.detected or row.filled and not row.actionable
                or row.capture_fraction is not None and (
                    not row.filled or not _finite(row.capture_fraction) or row.capture_fraction < 0
                ) or row.net_r is not None and (not row.filled or not _finite(row.net_r))):
            raise ValueError(f"{lane.id} has invalid or repeated opportunity evidence")
        seen_events.add(row.id)
    for row in _rows(lane.off_event_observations, OffEventObservation):
        if (type(row.time_ms) is not int or not study.start_ms <= row.time_ms < study.end_ms
                or any(type(value) is not int or value < 0 for value in (row.confirmations, row.losses))):
            raise ValueError(f"{lane.id} has invalid off-event evidence")


def _event_side(row):
    return "BUY" if row.side in (1, "BUY") else "SELL"


def _event_calendar(lane):
    return {(row.id, row.time_ms, _event_side(row)) for row in _rows(lane.opportunities, Opportunity)}


def _opportunities(lane, start_ms, end_ms, study):
    if lane.opportunities is None:
        return None
    rows = [row for row in _rows(lane.opportunities, Opportunity)
            if start_ms <= row.time_ms and row.time_ms + study.event_duration_ms <= end_ms]

    def metrics(events):
        detected = sum(row.detected for row in events)
        actionable = sum(row.actionable for row in events)
        filled = sum(row.filled for row in events)
        measured = [float(row.capture_fraction) for row in events if row.capture_fraction is not None]
        return {
            "events": len(events), "detected": detected, "actionable": actionable, "filled": filled,
            "detection_recall": _ratio(detected, len(events)),
            "actionable_recall": _ratio(actionable, len(events)), "fill_recall": _ratio(filled, len(events)),
            "capture_measured_count": len(measured),
            "mean_capture_when_measured": mean(measured) if measured else None,
            "mean_capture_per_eligible_event": (_ratio(sum(measured), len(events))
                                                if len(measured) == filled else None),
            "event_net_r": sum(float(row.net_r) for row in events if row.net_r is not None),
            "event_net_r_measured_count": sum(row.net_r is not None for row in events),
        }

    off_event = None
    if lane.off_event_observations is not None:
        off_rows = [row for row in _rows(lane.off_event_observations, OffEventObservation)
                    if start_ms <= row.time_ms < end_ms]
        off_event = {
            "confirmations": sum(row.confirmations for row in off_rows),
            "losses": sum(row.losses for row in off_rows),
        }
    return {
        "overall": metrics(rows),
        "by_side": {side: metrics([row for row in rows if _event_side(row) == side])
                    for side in ("BUY", "SELL")},
        "off_event": off_event,
    }


def _opportunity_gates(reference, candidate, study):
    if reference is None or candidate is None:
        return ["MISSED_OPPORTUNITY_EVIDENCE_UNAVAILABLE"]
    failures = set()
    for side in ("BUY", "SELL"):
        base, trial = reference["by_side"][side], candidate["by_side"][side]
        if not base["events"]:
            failures.add(f"{side}_EVENT_SAMPLE_UNAVAILABLE")
            continue
        for key, ratio, reason in (
            ("actionable_recall", study.min_actionable_recall_ratio, "ACTIONABLE_RECALL"),
            ("fill_recall", study.min_event_fill_recall_ratio, "EVENT_FILL_RECALL"),
            ("mean_capture_per_eligible_event", study.min_event_capture_ratio, "EVENT_CAPTURE"),
        ):
            if base[key] is None or trial[key] is None:
                failures.add(f"{side}_{reason}_EVIDENCE_UNAVAILABLE")
            elif trial[key] + 1e-8 < base[key] * ratio:
                failures.add(f"{side}_{reason}")
    baseline_off, candidate_off = reference["off_event"], candidate["off_event"]
    if baseline_off is None or candidate_off is None:
        failures.add("OFF_EVENT_EVIDENCE_UNAVAILABLE")
    elif candidate_off["losses"] > baseline_off["losses"] * study.max_off_event_losses_ratio:
        failures.add("OFF_EVENT_LOSSES")
    return sorted(failures)


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _closed_drawdown_duration(ordered, cash, start_ms, end_ms):
    equity = peak = 0.0
    peak_at = start_ms
    longest = 0
    for campaign, change in zip(ordered, cash):
        equity += change
        if equity >= peak:
            longest = max(longest, campaign.exit_ms - peak_at) if equity - change < peak else longest
            peak, peak_at = equity, campaign.exit_ms
    if equity < peak:
        longest = max(longest, end_ms - peak_at)
    return longest / DAY_MS


def _occupied_ms(campaigns):
    intervals = sorted((campaign.entry_ms, campaign.exit_ms) for campaign in campaigns)
    total = end = 0
    for start, next_end in intervals:
        if next_end > end:
            total += next_end - max(start, end)
            end = next_end
    return total


def _summarize(campaigns, start_ms, end_ms):
    ordered = sorted(campaigns, key=lambda campaign: (campaign.exit_ms, campaign.entry_ms, campaign.id))
    cash = [float(campaign.net_cash) for campaign in ordered]
    stressed = [float(campaign.stressed_net_cash) for campaign in ordered]
    risk = [float(campaign.risk_cash) for campaign in ordered]
    returns = [value / budget for value, budget in zip(cash, risk)]
    stress_r = [value / budget for value, budget in zip(stressed, risk)]
    wins = [value for value in cash if value > 0]
    losses = [value for value in cash if value < 0]
    loss_returns = [value for value in returns if value < 0]
    tail = sorted(returns)[:math.ceil(len(returns) * 0.05)]
    movement = [(value, float(campaign.mfe_cash)) for campaign, value in zip(ordered, cash)
                if campaign.mfe_cash is not None and campaign.mfe_cash > 0]
    measured = [float(campaign.costs_cash) for campaign in ordered if campaign.costs_cash is not None]
    native = [float(campaign.native_net_cash) for campaign in ordered if campaign.native_net_cash is not None]
    exposure = [float(campaign.exposure) for campaign in ordered if campaign.exposure is not None]
    days = (end_ms - start_ms) / DAY_MS
    result = cash_metrics(cash)
    stress_metrics = cash_metrics(stressed)
    result.update({
        "avg_win_cash": mean(wins) if wins else None,
        "avg_loss_cash": mean(losses) if losses else None,
        "expectancy_r": mean(returns) if returns else None,
        "risk_weighted_expectancy_r": _ratio(sum(cash), sum(risk)),
        "median_net_r": median(returns) if returns else None,
        "average_loss_r": mean(loss_returns) if loss_returns else None,
        "worst_net_r": min(returns, default=None),
        "p05_net_r": quantile(returns, 0.05),
        "tail_mean_net_r": mean(tail) if tail else None,
        "initial_risk_cash_total": sum(risk),
        "initial_risk_cash_mean": mean(risk) if risk else None,
        "initial_risk_cash_max": max(risk, default=None),
        "stressed_net_cash": sum(stressed),
        "stressed_expectancy_r": mean(stress_r) if stress_r else None,
        "stressed_drawdown_cash": stress_metrics["campaign_close_drawdown"],
        "drawdown_duration_calendar_days": _closed_drawdown_duration(ordered, cash, start_ms, end_ms),
        "campaigns_per_calendar_week": len(ordered) * 7 / days,
        "mean_hold_minutes": (mean(campaign.exit_ms - campaign.entry_ms for campaign in ordered)
                              / 60_000 if ordered else None),
        "occupied_fraction_of_wall_time": _occupied_ms(ordered) / (end_ms - start_ms),
        "favorable_movement_measured_count": len(movement),
        "favorable_movement_retained": _ratio(
            sum(min(max(value, 0), mfe) for value, mfe in movement),
            sum(mfe for _, mfe in movement),
        ),
        "known_cost_cash": sum(measured) if measured else None,
        "known_cost_count": len(measured),
        "additional_stress_cash": sum(cash) - sum(stressed),
        "native_net_cash": sum(native) if native else None,
        "native_net_cash_count": len(native),
        "max_known_entry_notional_cash": max(exposure, default=None),
    })
    return result


def _window(campaigns, start_ms, end_ms):
    return tuple(campaign for campaign in campaigns
                 if start_ms <= campaign.entry_ms and campaign.exit_ms < end_ms)


def _intersects(campaign, start_ms, end_ms):
    return campaign.entry_ms < end_ms and campaign.exit_ms >= start_ms


def _period(lane, start_ms, end_ms):
    selected = _window(lane.campaigns, start_ms, end_ms)
    metrics = _summarize(selected, start_ms, end_ms)
    metrics["boundary_excluded_count"] = sum(
        _intersects(campaign, start_ms, end_ms) for campaign in lane.campaigns
    ) - len(selected)
    return metrics


def _gates(reference, candidate, study):
    """Evaluate effect size and downside together on the same window."""
    failures = []
    if min(reference["n"], candidate["n"]) < study.min_campaigns:
        failures.append("INSUFFICIENT_CAMPAIGNS")
    if candidate["n"] < reference["n"] * study.min_frequency_ratio:
        failures.append("FREQUENCY_FLOOR")
    expectancy, baseline_expectancy = candidate["expectancy_r"], reference["expectancy_r"]
    if (expectancy is None or baseline_expectancy is None or expectancy <= 0
            or expectancy - baseline_expectancy < study.min_expectancy_lift_r):
        failures.append("NET_EXPECTANCY")
    stress, baseline_stress = candidate["stressed_expectancy_r"], reference["stressed_expectancy_r"]
    if (stress is None or baseline_stress is None or stress <= 0 or stress <= baseline_stress
            or candidate["stressed_net_cash"] <= reference["stressed_net_cash"]
            or candidate["net_cash"] <= reference["net_cash"]):
        failures.append("AFTER_COSTS_AND_FREQUENCY")
    if any(candidate[key] > reference[key] * study.max_drawdown_ratio + 1e-8
           for key in ("campaign_close_drawdown", "stressed_drawdown_cash")):
        failures.append("DRAWDOWN")
    tail, baseline_tail = candidate["tail_mean_net_r"], reference["tail_mean_net_r"]
    if tail is None or baseline_tail is None or tail < baseline_tail - study.max_tail_loss_r_degradation:
        failures.append("TAIL_LOSS")
    for metrics in (reference, candidate):
        maximum = metrics["initial_risk_cash_max"]
        if maximum is not None and maximum > study.max_campaign_risk_cash + 1e-8:
            failures.append("COMPARABLE_RISK_BUDGET")
            break
    return failures


def _walk_forward(lanes, study, end_ms):
    folds = []
    width = study.fold_days * DAY_MS
    for start in range(study.validation_start_ms, end_ms, width):
        stop = min(start + width, end_ms)
        fit_stop = start - study.embargo_ms
        if fit_stop <= study.start_ms:
            raise ValueError("Embargo consumes the initial development window")
        training = {lane.id: _period(lane, study.start_ms, fit_stop) for lane in lanes}
        eligible = [lane.id for lane in lanes[1:]
                    if not _gates(training[study.reference_id], training[lane.id], study)]
        selected = max(
            sorted(eligible),
            key=lambda lane_id: (training[lane_id]["stressed_expectancy_r"],
                                 training[lane_id]["stressed_net_cash"],
                                 -training[lane_id]["stressed_drawdown_cash"]),
            default=study.reference_id,
        )
        folds.append({
            "training_end_ms_exclusive": fit_stop,
            "validation_start_ms": start,
            "validation_end_ms_exclusive": stop,
            "full_fold": stop - start == width,
            "selection": selected,
            "development": training,
            "validation": {lane.id: _period(lane, start, stop) for lane in lanes},
        })
    return folds


def _weekly_rows(lane, start_ms, end_ms):
    rows = defaultdict(lambda: [0.0, 0, 0.0])
    for campaign in _window(lane.campaigns, start_ms, end_ms):
        amount = float(campaign.stressed_net_cash)
        index = (campaign.exit_ms - start_ms) // WEEK_MS
        rows[index][0] += amount / float(campaign.risk_cash)
        rows[index][1] += 1
        rows[index][2] += amount
    return [tuple(rows[index]) for index in range((end_ms - start_ms) // WEEK_MS)]


def _paired_inference(reference, candidate, start_ms, end_ms, study):
    # End on an exact week and include every full calendar week, even when flat.
    weeks = (end_ms - start_ms) // WEEK_MS
    cutoff = start_ms + weeks * WEEK_MS
    base = _weekly_rows(reference, start_ms, cutoff)
    trial = _weekly_rows(candidate, start_ms, cutoff)
    usable = min(sum(row[1] for row in base), sum(row[1] for row in trial))
    effect = None
    samples = []
    result = {"weeks": weeks, "end_ms_exclusive": cutoff,
              "minimum_campaign_count": usable, "bootstrap_draws": 0,
              "stressed_expectancy_difference_r": None, "ci95": [None, None], "p_one_sided": 1.0,
              "mean_paired_stressed_net_cash_per_week": (mean(b[2] - a[2] for a, b in zip(base, trial))
                                                         if weeks else None)}
    if weeks <= study.bootstrap_weeks or usable < study.min_campaigns:
        return {**result, "status": "INSUFFICIENT_SAMPLE"}

    def difference(indices):
        indices = tuple(indices)
        base_count = sum(base[index][1] for index in indices)
        trial_count = sum(trial[index][1] for index in indices)
        if base_count and trial_count:
            return (sum(trial[index][0] for index in indices) / trial_count
                    - sum(base[index][0] for index in indices) / base_count)
        return None

    effect = difference(range(weeks))
    rng = random.Random(study.bootstrap_seed)
    for _ in range(study.bootstrap_repetitions):
        indices = []
        while len(indices) < weeks:
            first = rng.randrange(weeks)
            indices.extend((first + offset) % weeks for offset in range(study.bootstrap_weeks))
        sample = difference(indices[:weeks])
        if sample is not None:
            samples.append(sample)
    if len(samples) < study.bootstrap_repetitions * 0.9:
        return {**result, "status": "INSUFFICIENT_BOOTSTRAP_COVERAGE", "bootstrap_draws": len(samples)}
    return {
        **result, "status": "ESTIMATED", "bootstrap_draws": len(samples),
        "stressed_expectancy_difference_r": effect,
        "ci95": [quantile(samples, 0.025), quantile(samples, 0.975)],
        "p_one_sided": (1 + sum(sample - effect >= effect for sample in samples)) / (1 + len(samples)),
    }


OBJECTIVES = {
    "win_rate": 1,
    "avg_win_cash": 1,
    "avg_loss_cash": 1,
    "expectancy_r": 1,
    "stressed_expectancy_r": 1,
    "profit_factor": 1,
    "campaign_close_drawdown": -1,
    "tail_mean_net_r": 1,
    "favorable_movement_retained": 1,
    "campaigns_per_calendar_week": 1,
}


def _tradeoffs(base, trial):
    deltas = {
        key: trial[key] - base[key] if trial[key] is not None and base[key] is not None else None
        for key in OBJECTIVES
    }
    directions = {key: (None if delta is None else "IMPROVED" if delta * OBJECTIVES[key] > 1e-8
                        else "WORSENED" if delta * OBJECTIVES[key] < -1e-8 else "UNCHANGED")
                  for key, delta in deltas.items()}
    return {"deltas_vs_s5": deltas, "directions_vs_s5": directions}


def _pareto_front(metrics, minimum_count):
    axes = ("expectancy_r", "stressed_expectancy_r", "net_cash", "stressed_net_cash",
            "tail_mean_net_r", "stressed_drawdown_cash")
    values = {
        lane_id: tuple(row[key] * (-1 if key == "stressed_drawdown_cash" else 1) for key in axes)
        for lane_id, row in metrics.items()
        if row["n"] >= minimum_count and all(row[key] is not None for key in axes)
    }

    def dominates(first, second):
        return (all(a >= b - 1e-8 for a, b in zip(first, second))
                and any(a > b + 1e-8 for a, b in zip(first, second)))

    return {
        "objectives": axes,
        "eligible_for_description": sorted(values),
        "non_dominated": sorted(
            lane_id for lane_id, value in values.items()
            if not any(other != lane_id and dominates(other_value, value)
                       for other, other_value in values.items())
        ),
    }


def registered_protocol_sha256(study, baseline, candidates, forward_start_ms, forward_end_ms):
    """Fingerprint the frozen protocol without reading any campaign or forward market data."""
    controls = asdict(study)
    for field_name in ("data_sha256", "forward", "last_inspected_ms"):
        del controls[field_name]
    sources = {
        lane.id: {"source_sha256": lane.source_sha256, "axis": lane.axis, "parameters": lane.parameters}
        for lane in (baseline, *candidates)
    }
    return _digest({"controls": controls, "sources": sources, "forward_window": [forward_start_ms, forward_end_ms]})


def _forward_failures(study, lanes):
    evidence = study.forward
    if evidence is None:
        return ["UNTOUCHED_FORWARD_DATA_UNAVAILABLE"]
    reasons = []
    if not (
        study.validation_start_ms < evidence.start_ms < evidence.end_ms <= study.end_ms
        and study.registered_at_ms < evidence.start_ms
        and study.last_inspected_ms < evidence.start_ms
        and evidence.first_access_ms >= evidence.end_ms
    ):
        reasons.append("FORWARD_CHRONOLOGY")
    if evidence.rules_sha256 != study.rules_sha256 or evidence.data_sha256 != study.data_sha256:
        reasons.append("FORWARD_PROVENANCE_MISMATCH")
    if not all(_valid_sha(value) for value in (study.rules_sha256, evidence.registration_receipt_sha256,
                                               evidence.access_receipt_sha256)):
        reasons.append("FORWARD_PROVENANCE_UNATTESTED")
    protocol = registered_protocol_sha256(study, lanes[0], lanes[1:], evidence.start_ms, evidence.end_ms)
    if evidence.protocol_sha256 != protocol:
        reasons.append("FROZEN_PROTOCOL_MISMATCH")
    if set(study.registered_candidate_ids) != {lane.id for lane in lanes[1:]} or not all(
        _valid_sha(lane.source_sha256) for lane in lanes
    ):
        reasons.append("CANDIDATE_FAMILY_NOT_FROZEN")
    if evidence.end_ms - evidence.start_ms < study.min_forward_weeks * WEEK_MS:
        reasons.append("FORWARD_WINDOW_TOO_SHORT")
    return reasons


def _lane_metadata(lane):
    return {
        "axis": lane.axis, "parameters": _normalize_json(lane.parameters),
        "source_sha256": lane.source_sha256,
        "campaign_tape_sha256": _digest([asdict(campaign) for campaign in sorted(
            lane.campaigns, key=lambda campaign: (campaign.entry_ms, campaign.exit_ms, campaign.id)
        )]),
        "blocked_count": lane.blocked_count, "rejected_count": lane.rejected_count,
        "opportunity_tape_sha256": (_digest([asdict(row) for row in sorted(
            _rows(lane.opportunities, Opportunity), key=lambda row: (row.time_ms, row.id)
        )]) if lane.opportunities is not None else None),
        "off_event_tape_sha256": (_digest([asdict(row) for row in sorted(
            _rows(lane.off_event_observations, OffEventObservation),
            key=lambda row: (row.time_ms, row.confirmations, row.losses),
        )]) if lane.off_event_observations is not None else None),
    }


def evaluate(baseline: Lane, candidates: Sequence[Lane], manifest: Study, approval: Approval | None = None):
    """Produce research evidence and an approval gate. The evaluator has no execution path."""
    _validate_study(manifest)
    lanes = (baseline, *candidates)
    if baseline.id != manifest.reference_id or len({lane.id for lane in lanes}) != len(lanes):
        raise ValueError("Pass the immutable reference followed by uniquely identified challengers")
    for lane in lanes:
        _validate_lane(lane, manifest)
    event_calendars = [_event_calendar(lane) for lane in lanes if lane.opportunities is not None]
    if event_calendars and any(calendar != event_calendars[0] for calendar in event_calendars[1:]):
        raise ValueError("Opportunity comparisons require the same independently defined event calendar")

    study = manifest
    forward_failures = _forward_failures(study, lanes)
    forward = study.forward
    chronological_forward = (forward is not None
                             and study.validation_start_ms < forward.start_ms < forward.end_ms <= study.end_ms)
    history_end = forward.start_ms if chronological_forward else study.end_ms
    folds = _walk_forward(lanes, study, history_end)
    historical = {lane.id: _period(lane, study.start_ms, history_end) for lane in lanes}
    validation = {lane.id: _period(lane, study.validation_start_ms, history_end) for lane in lanes}
    inference_start = forward.start_ms if chronological_forward else study.validation_start_ms
    inference_end = forward.end_ms if chronological_forward else history_end
    evaluation = {lane.id: _period(lane, inference_start, inference_end) for lane in lanes}
    opportunities = {lane.id: _opportunities(lane, inference_start, inference_end, study) for lane in lanes}
    tests = [_paired_inference(baseline, lane, inference_start, inference_end, study)
             for lane in lanes[1:]]
    adjusted = holm([test["p_one_sided"] for test in tests])
    results = {}
    for lane, test, adjusted_p in zip(lanes[1:], tests, adjusted):
        gates = _gates(evaluation[baseline.id], evaluation[lane.id], study)
        confidence = test["ci95"][0]
        inference_passed = confidence is not None and confidence > 0 and adjusted_p <= study.alpha
        evaluation_failures = gates + ([] if inference_passed else ["MULTIPLICITY_ADJUSTED_EVIDENCE"])
        opportunity_gates = _opportunity_gates(opportunities[baseline.id], opportunities[lane.id], study)
        candidate_reasons = [*forward_failures, *evaluation_failures, *opportunity_gates]
        if not study.primary:
            candidate_reasons.append("SECONDARY_DESCRIPTIVE_STUDY")
        adequate_sample = min(evaluation[baseline.id]["n"], evaluation[lane.id]["n"]) >= study.min_campaigns
        lost_opportunities = any(reason.endswith(("_RECALL", "_CAPTURE")) or reason == "OFF_EVENT_LOSSES"
                                 for reason in opportunity_gates)
        status = ("REJECTED" if adequate_sample and (gates or lost_opportunities)
                  else "INCONCLUSIVE" if candidate_reasons else "AWAITING_EXPLICIT_APPROVAL")
        results[lane.id] = {
            "status": status, "reasons": candidate_reasons,
            "tradeoffs": _tradeoffs(evaluation[baseline.id], evaluation[lane.id]),
            "paired_week_bootstrap": {**test, "holm_p_one_sided": adjusted_p},
            "opportunity_gate_failures": opportunity_gates,
        }

    report = {
        "schema": "qirvanta.gold-pro.performance.v1",
        "study": _normalize_json(asdict(study)),
        "market_window_semantics": "UTC milliseconds, start inclusive and end exclusive",
        "reference": baseline.id,
        "lane_metadata": {lane.id: _lane_metadata(lane) for lane in lanes},
        "history": historical,
        "historical_walk_forward": folds,
        "historical_validation": validation,
        "evaluation": evaluation,
        "opportunity_evaluation": opportunities,
        "pareto_front_descriptive": _pareto_front(evaluation, study.min_campaigns),
        "evaluation_window": [inference_start, inference_end],
        "evaluation_scope": "FORWARD_WITH_ATTESTED_PROVENANCE" if not forward_failures else "EXPLORATORY",
        "forward_failures": forward_failures,
        "multiplicity": {"method": "Holm", "family": [lane.id for lane in lanes[1:]], "alpha": study.alpha},
        "candidates": results,
        "limitations": [
            "Resampling pairs full calendar weeks using circular blocks and assumes comparable, weakly dependent blocks.",
            "Confidence intervals are descriptive. One-sided paired test p-values receive Holm family correction.",
            "Drawdown uses realized campaign closes. Wall time includes weekends and market closures.",
            "MFE capture weights known favorable excursions by cash and assigns zero retained movement to net losses.",
            "Forward provenance depends on immutable, externally recorded registration and access receipts.",
        ],
    }
    evidence_hash = _digest(report)
    approved = (approval is not None and approval.study_id == study.study_id and approval.approved_by.strip()
                and approval.evidence_sha256 == evidence_hash and forward is not None
                and approval.approved_at_ms >= forward.first_access_ms
                and results.get(approval.candidate_id, {}).get("status") == "AWAITING_EXPLICIT_APPROVAL")
    return {
        **report, "evidence_sha256": evidence_hash,
        "decision": "APPROVED_FOR_MANUAL_REVIEW" if approved else "RETAIN_IMMUTABLE_REFERENCE",
        "approved_candidate": approval.candidate_id if approved else None,
        "approval": asdict(approval) if approved else None,
    }
