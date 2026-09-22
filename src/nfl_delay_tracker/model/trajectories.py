"""Correlated event generation using an AR(1) Gaussian copula."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence
from statistics import NormalDist

_NORMAL = NormalDist()


def _bounded_probability(value: float) -> float:
    return min(1.0, max(0.0, value))


def correlated_events(
    probabilities: Sequence[float],
    *,
    rho: float,
    rng: random.Random,
) -> list[bool]:
    """Draw Bernoulli events with requested marginals and serial dependence."""
    if not -0.999 < rho < 0.999:
        raise ValueError("rho must be between -0.999 and 0.999")
    # Start in the stationary N(0, 1) distribution. Starting at zero shrinks
    # the first latent draw's variance and distorts its requested Bernoulli
    # marginal whenever p is not 0.5.
    previous = rng.gauss(0.0, 1.0)
    residual_scale = math.sqrt(1 - rho * rho)
    events: list[bool] = []
    for raw_probability in probabilities:
        probability = _bounded_probability(raw_probability)
        latent = rho * previous + residual_scale * rng.gauss(0.0, 1.0)
        previous = latent
        if probability <= 0:
            events.append(False)
        elif probability >= 1:
            events.append(True)
        else:
            events.append(_NORMAL.cdf(latent) < probability)
    return events


def estimate_latent_correlation(
    sequences: Sequence[Sequence[bool]], *, minimum_pairs: int = 30
) -> float | None:
    """Estimate adjacent-bin latent normal correlation from binary histories.

    Jeffreys smoothing avoids infinite probits for all-zero/all-one windows.
    Return None when archive does not yet hold enough transitions.
    """
    latent_pairs: list[tuple[float, float]] = []
    for sequence in sequences:
        for left, right in zip(sequence, sequence[1:], strict=False):
            p_left = 0.75 if left else 0.25
            p_right = 0.75 if right else 0.25
            latent_pairs.append((_NORMAL.inv_cdf(p_left), _NORMAL.inv_cdf(p_right)))
    if len(latent_pairs) < minimum_pairs:
        return None
    left_mean = sum(pair[0] for pair in latent_pairs) / len(latent_pairs)
    right_mean = sum(pair[1] for pair in latent_pairs) / len(latent_pairs)
    covariance = sum((left - left_mean) * (right - right_mean) for left, right in latent_pairs)
    left_variance = sum((left - left_mean) ** 2 for left, _ in latent_pairs)
    right_variance = sum((right - right_mean) ** 2 for _, right in latent_pairs)
    if left_variance == 0 or right_variance == 0:
        return None
    return min(0.95, max(0.0, covariance / math.sqrt(left_variance * right_variance)))
