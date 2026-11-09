"""Fixed ridge expected-net-R regression on the same causal feature rows."""
import hashlib
import json
from math import isfinite
from statistics import mean, pstdev

from research.ultimate.models import EMBARGO_MS, FEATURES
from research.validation.statistics import block_bootstrap, chronological_split, walk_forward


def expected_r_fold(train, test):
    if not train or not test:
        return {"status": "INSUFFICIENT_TRAIN_OR_EVALUATION"}
    if max(e["end"] for e in train) >= min(e["start"] for e in test) - EMBARGO_MS:
        raise ValueError("net R labels overlap evaluation")
    x = [row["x"] for row in train]
    y = [row["net_r"] for row in train]
    if any(len(row) != len(FEATURES) or any(not isfinite(v) for v in row)
           for row in x + [row["x"] for row in test]) or any(not isfinite(v) for v in y):
        raise ValueError("invalid net R feature or training target")
    width = len(FEATURES)
    means = [mean(row[j] for row in x) for j in range(width)]
    scales = [max(pstdev(row[j] for row in x), 1e-9) for j in range(width)]
    target_mean = mean(y)

    def standardized(row):
        return [(v - m) / s for v, m, s in zip(row, means, scales)]

    x_scaled = [standardized(row) for row in x]
    weights = [0.] * width
    for _ in range(1000):
        gradients = [0.] * width
        for row, target in zip(x_scaled, y):
            error = target_mean + sum(w * v for w, v in zip(weights, row)) - target
            for j, value in enumerate(row):
                gradients[j] += error * value / len(train)
        weights = [w - .05 * (g + .1 * w) for w, g in zip(weights, gradients)]
    predictions = [target_mean + sum(w * v for w, v in zip(weights, standardized(row["x"])))
                   for row in test]
    actual = [e["net_r"] for e in test]
    baseline_sq = [(target_mean - v) ** 2 for v in actual]
    model_sq = [(p - v) ** 2 for p, v in zip(predictions, actual)]
    selected = [row for row, p in zip(test, predictions) if p > 0]
    return {
        "status": "LOCALLY_TESTED_EXPLORATORY", "train_ids": [row["id"] for row in train],
        "training_sha256": hashlib.sha256(json.dumps([x, y], separators=(",", ":")).encode()).hexdigest(),
        "predictions": [{"id": row["id"], "expected_net_r": p, "actual_net_r": row["net_r"]}
                        for row, p in zip(test, predictions)],
        "model": {"algorithm": "fixed_gradient_ridge_v1", "mean": means, "scale": scales,
                  "weights": weights, "intercept": target_mean, "iterations": 1000, "ridge": .1, "rate": .05},
        "constant_mse": mean(baseline_sq), "ridge_mse": mean(model_sq),
        "constant_mae": mean(abs(target_mean - value) for value in actual),
        "ridge_mae": mean(abs(p - v) for p, v in zip(predictions, actual)),
        "mse_improvement": block_bootstrap([a - b for a, b in zip(baseline_sq, model_sq)], repetitions=1000),
        "predicted_positive_cohort": {"n": len(selected), "net_cash": sum(e["net_cash"] for e in selected),
                                      "avg_net_r": mean(e["net_r"] for e in selected) if selected else None},
        "all_evaluation": {"n": len(test), "net_cash": sum(e["net_cash"] for e in test), "avg_net_r": mean(actual)},
    }


def expected_r_report(events):
    sample = [e for e in events if e["net_r"] is not None]
    split = chronological_split(sample, embargo_ms=EMBARGO_MS)
    return {
        "target": "Actual after-cost native net R for complete campaigns",
        "excluded_missing_R_ids": [e["id"] for e in events if e["net_r"] is None],
        "chronological": expected_r_fold(split["train"], split["test"]),
        "walk_forward": [expected_r_fold(fold["train"], fold["test"])
                         for fold in walk_forward(sample, initial_train=30, test_size=12, embargo_ms=EMBARGO_MS)],
        "holdout_independent": False, "promotion_allowed": False,
    }
