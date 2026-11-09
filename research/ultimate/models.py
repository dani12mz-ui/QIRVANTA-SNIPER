"""Local model fits on historical native campaign labels, separated in time."""
from math import isfinite
from statistics import mean, pstdev
from time import perf_counter_ns

from research.models.logistic import Logistic, logit, score
from research.validation.statistics import chronological_split, walk_forward


FEATURES = ("side", "directional_body_fraction", "four_bar_signed_extension_atr", "adx", "atr_ratio",
            "directional_rsi", "native_efficiency", "native_htf_agreement", "planned_stop_atr")
TARGETS = ("net_positive", "tp1_before_stop", "tp2_before_stop", "tp3_before_stop")
EMBARGO_MS = 900_000


def target_before_stop(campaign, target):
    stop = min((x["exit_bar_index"] for x in campaign["exit_rows"] if x["exit_comment"] == "SL"), default=None)
    take = min((x["exit_bar_index"] for x in campaign["exit_rows"] if x["exit_comment"] == target), default=None)
    if take is None:
        return 0
    if stop == take:
        return None
    return int(stop is None or take < stop)


def make_events(context, campaigns):
    by_id = {c["plan_id"]: c for c in campaigns}
    rows, unavailable = [], []
    for row in context:
        x = [float(row["native_htf_relation"] == "WITH_HTF") if name == "native_htf_agreement" else row[name]
             for name in FEATURES]
        if any(value is None or not isfinite(value) for value in x):
            unavailable.append(row["id"])
            continue
        campaign = by_id[row["id"]]
        labels = {"net_positive": int(row["net_cash"] > 0)}
        labels.update({f"tp{i}_before_stop": target_before_stop(campaign, f"TP{i}") for i in (1, 2, 3)})
        rows.append({
            "id": row["id"], "start": row["signal_at"], "end": row["exit_time"] + EMBARGO_MS,
            "x": x, "labels": labels, "net_cash": row["net_cash"], "net_r": row["net_r"],
        })
    return rows, unavailable


def metrics(probabilities, events):
    y = [row["y"] for row in events]
    selected = [row for p, row in zip(probabilities, events) if p >= .5]
    rejected = [row for p, row in zip(probabilities, events) if p < .5]
    true_positive = sum(row["y"] for row in selected)
    result = score(probabilities, y)
    result["precision"] = true_positive / len(selected) if selected else None
    result["recall"] = true_positive / sum(y) if sum(y) else None
    result["fixed_half_policy"] = {}
    for label, rows in (("all", events), ("accepted", selected), ("rejected", rejected)):
        risk = [row["net_r"] for row in rows if row["net_r"] is not None]
        result["fixed_half_policy"][label] = {
            "n": len(rows), "net_cash": sum(row["net_cash"] for row in rows),
            "avg_net_r": mean(risk) if risk else None,
            "cash_winners": sum(row["net_cash"] > 0 for row in rows),
            "cash_losers": sum(row["net_cash"] < 0 for row in rows),
        }
    return result


def evaluate_fold(train, validation, test):
    result = {
        "train_ids": [e["id"] for e in train],
        "validation_ids": [e["id"] for e in validation],
        "evaluation_ids": [e["id"] for e in test],
    }
    if not train or not test:
        return {**result, "status": "INSUFFICIENT_TRAIN_OR_EVALUATION"}
    if max(row["end"] for row in train) >= min(row["start"] for row in (validation or test)) - EMBARGO_MS:
        raise ValueError("training label overlaps later evaluation")
    if validation and max(row["end"] for row in validation) >= min(row["start"] for row in test) - EMBARGO_MS:
        raise ValueError("calibration label overlaps evaluation")
    prevalence = mean(row["y"] for row in train)
    result["constant"] = metrics([prevalence] * len(test), test)
    if len(train) < 10 or len({row["y"] for row in train}) != 2:
        return {**result, "status": "INSUFFICIENT_TRAINING_CLASSES_OR_ROWS"}
    model = Logistic().fit([row["x"] for row in train], [row["y"] for row in train])
    start = perf_counter_ns()
    raw = model.predict([row["x"] for row in test])
    latency_ms = (perf_counter_ns() - start) / 1_000_000
    result.update(status="LOCALLY_TESTED_EXPLORATORY", model=model.artifact(), raw=metrics(raw, test),
                  raw_inference_latency_ms_batch=latency_ms)
    scores = [{"id": row["id"], "label": row["y"], "raw": prob} for row, prob in zip(test, raw)]
    if len(validation) >= 8 and len({row["y"] for row in validation}) == 2:
        calibration_x = [[logit(p)] for p in model.predict([row["x"] for row in validation])]
        calibrator = Logistic().fit(calibration_x, [row["y"] for row in validation])
        calibrated = calibrator.predict([[logit(p)] for p in raw])
        result.update(calibrator=calibrator.artifact(), calibrated=metrics(calibrated, test))
        for row, p in zip(scores, calibrated):
            row["calibrated"] = p
    else:
        result["calibration_status"] = "INSUFFICIENT_VALIDATION_CLASSES_OR_ROWS"
    result["predictions"] = scores
    result["feature_drift"] = {}
    for j, name in enumerate(FEATURES):
        train_values = [row["x"][j] for row in train]
        test_values = [row["x"][j] for row in test]
        scale = pstdev(train_values)
        result["feature_drift"][name] = {
            "train_mean": mean(train_values), "evaluation_mean": mean(test_values),
            "delta_train_sd": (mean(test_values) - mean(train_values)) / scale if scale > 0 else None,
        }
    return result


def research_models(context, campaigns):
    events, unavailable = make_events(context, campaigns)
    objectives = {}
    for target in TARGETS:
        sample = [{**event, "y": event["labels"][target]} for event in events
                  if event["labels"][target] is not None]
        split = chronological_split(sample, embargo_ms=EMBARGO_MS)
        folds = []
        for split_fold in walk_forward(sample, initial_train=30, test_size=12, embargo_ms=EMBARGO_MS):
            train = split_fold["train"]
            pivot = int(.8 * len(train))
            validation = train[pivot:]
            fit = [row for row in train[:pivot]
                   if not validation or row["end"] < validation[0]["start"] - EMBARGO_MS]
            folds.append(evaluate_fold(fit, validation, split_fold["test"]))
        objectives[target] = {
            "sample_n": len(sample), "positives": sum(event["y"] for event in sample),
            "ambiguous_ids": [event["id"] for event in events if event["labels"][target] is None],
            "purged_ids": [e["id"] for e in split["purged"]],
            "chronological": evaluate_fold(split["train"], split["validation"], split["test"]),
            "walk_forward": folds,
        }
    return {
        "feature_order": list(FEATURES), "events": events, "missing_features_ids": unavailable,
        "objectives": objectives, "provenance": "Point-in-time submitted metadata + confirmed OHLC features",
        "classification_scope": "Historical outcome classification for selected campaigns",
        "untrained": ["random_forest", "xgboost", "lightgbm", "HMM", "mixture_of_experts",
                      "TFT", "N-BEATS", "DeepAR", "autoencoder", "reinforcement_learning",
                      "contextual_bandit", "bayesian_updating"],
        "untrained_reason": "At most 72 selected campaign labels, already observed. No independent validation sample.",
        "untouched_holdout": False, "external_inference": False, "promotion_allowed": False,
    }
