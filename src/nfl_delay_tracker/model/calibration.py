"""Simple calibration, training and scoring tools for the hazard baseline."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


def brier_score(probabilities: Sequence[float], labels: Sequence[int]) -> float:
    _check_pairs(probabilities, labels)
    return sum((p - y) ** 2 for p, y in zip(probabilities, labels, strict=True)) / len(labels)


def log_loss(probabilities: Sequence[float], labels: Sequence[int]) -> float:
    _check_pairs(probabilities, labels)
    clipped = [min(1 - 1e-12, max(1e-12, value)) for value in probabilities]
    return -sum(
        y * math.log(p) + (1 - y) * math.log(1 - p) for p, y in zip(clipped, labels, strict=True)
    ) / len(labels)


def _check_pairs(probabilities: Sequence[float], labels: Sequence[int]) -> None:
    if len(probabilities) != len(labels) or not labels:
        raise ValueError("probability and label arrays must have same nonzero length")
    if any(not 0 <= value <= 1 for value in probabilities):
        raise ValueError("probabilities must be in [0, 1]")
    if any(value not in (0, 1) for value in labels):
        raise ValueError("labels must be binary")


def reliability_bins(
    probabilities: Sequence[float], labels: Sequence[int], *, bin_count: int = 10
) -> list[dict[str, float | int]]:
    _check_pairs(probabilities, labels)
    if bin_count < 1:
        raise ValueError("bin_count must be positive")
    result = []
    for index in range(bin_count):
        lower = index / bin_count
        upper = (index + 1) / bin_count
        members = [
            (probability, label)
            for probability, label in zip(probabilities, labels, strict=True)
            if lower <= probability < upper or (index == bin_count - 1 and probability == 1)
        ]
        if members:
            result.append(
                {
                    "lower": lower,
                    "upper": upper,
                    "count": len(members),
                    "mean_predicted": sum(p for p, _ in members) / len(members),
                    "observed_rate": sum(y for _, y in members) / len(members),
                }
            )
    return result


def isotonic_fit(
    probabilities: Sequence[float], labels: Sequence[int]
) -> list[tuple[float, float]]:
    """Fit a one-dimensional isotonic calibration map with PAV."""
    _check_pairs(probabilities, labels)
    ordered = sorted(zip(probabilities, labels, strict=True))
    blocks: list[list[float]] = []
    for probability, label in ordered:
        blocks.append([probability, probability, float(label), 1.0])
        while len(blocks) > 1 and blocks[-2][2] / blocks[-2][3] > blocks[-1][2] / blocks[-1][3]:
            right = blocks.pop()
            left = blocks.pop()
            blocks.append([left[0], right[1], left[2] + right[2], left[3] + right[3]])
    return [((start + end) / 2, positives / count) for start, end, positives, count in blocks]


def isotonic_predict(mapping: Sequence[tuple[float, float]], probability: float) -> float:
    if not mapping:
        return probability
    return min(mapping, key=lambda point: abs(point[0] - probability))[1]


@dataclass(frozen=True)
class LogisticModel:
    coefficients: tuple[float, ...]
    intercept: float
    feature_names: tuple[str, ...]
    training_rows: int

    def predict_probability(self, features: Sequence[float]) -> float:
        if len(features) != len(self.coefficients):
            raise ValueError("feature count differs from trained model")
        score = self.intercept + sum(
            coefficient * value
            for coefficient, value in zip(self.coefficients, features, strict=True)
        )
        return 1 / (1 + math.exp(-max(-35, min(35, score))))


def fit_logistic(
    features: Sequence[Sequence[float]],
    labels: Sequence[int],
    feature_names: Sequence[str],
    *,
    learning_rate: float = 0.05,
    epochs: int = 2500,
    l2: float = 0.01,
) -> LogisticModel:
    """Fit a reproducible small logistic baseline using batch gradient descent."""
    if not features or len(features) != len(labels):
        raise ValueError("feature matrix and labels must have same nonzero row count")
    width = len(feature_names)
    if any(len(row) != width for row in features):
        raise ValueError("feature rows must match feature_names")
    if any(value not in (0, 1) for value in labels):
        raise ValueError("labels must be binary")
    weights = [0.0] * width
    prevalence = min(1 - 1e-6, max(1e-6, sum(labels) / len(labels)))
    intercept = math.log(prevalence / (1 - prevalence))
    for _ in range(epochs):
        grad = [0.0] * width
        bias_grad = 0.0
        for row, label in zip(features, labels, strict=True):
            score = intercept + sum(
                weight * value for weight, value in zip(weights, row, strict=True)
            )
            prediction = 1 / (1 + math.exp(-max(-35, min(35, score))))
            error = prediction - label
            bias_grad += error
            for index, value in enumerate(row):
                grad[index] += error * value
        scale = 1 / len(labels)
        intercept -= learning_rate * bias_grad * scale
        weights = [
            weight - learning_rate * (gradient * scale + l2 * weight)
            for weight, gradient in zip(weights, grad, strict=True)
        ]
    return LogisticModel(tuple(weights), intercept, tuple(feature_names), len(labels))
