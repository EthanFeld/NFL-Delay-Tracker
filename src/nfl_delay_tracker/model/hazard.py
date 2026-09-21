"""Hazard conversion and forecast-horizon blending."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

from nfl_delay_tracker.models import HazardPoint


def probability_per_bin(
    probability_over_period: float, period_minutes: int, bin_minutes: int = 5
) -> float:
    """Convert a period-level probability to an equal-duration marginal hazard."""
    if not 0 <= probability_over_period <= 1 or period_minutes <= 0 or bin_minutes <= 0:
        raise ValueError("probability and period/bin durations invalid")
    if probability_over_period in (0, 1):
        return probability_over_period
    return float(1 - math.pow(1 - probability_over_period, bin_minutes / period_minutes))


def blend_hazards(
    nowcast_probability: float,
    forecast_probability: float,
    lead_minutes: int,
    *,
    nowcast_weight_at_zero: float = 0.9,
    nowcast_weight_at_60: float = 0.2,
) -> float:
    """Blend observed/near-term and numerical forecast risks smoothly."""
    if not 0 <= nowcast_probability <= 1 or not 0 <= forecast_probability <= 1:
        raise ValueError("probabilities must be in [0, 1]")
    fraction = min(1.0, max(0.0, lead_minutes / 60))
    weight = nowcast_weight_at_zero + fraction * (nowcast_weight_at_60 - nowcast_weight_at_zero)
    return weight * nowcast_probability + (1 - weight) * forecast_probability


def hourly_hazard_points(
    periods: list[tuple[datetime, datetime, float]],
    *,
    kickoff: datetime,
    bin_minutes: int = 5,
    source: str,
) -> list[HazardPoint]:
    """Expand provider thunder probabilities into time-indexed five-minute bins."""
    points: list[HazardPoint] = []
    for start, end, probability in periods:
        if start.tzinfo is None or end.tzinfo is None or kickoff.tzinfo is None:
            raise ValueError("weather interval and kickoff times must be timezone-aware")
        duration = max(1, int((end - start).total_seconds() / 60))
        marginal = probability_per_bin(probability, duration, bin_minutes)
        first = int((start - kickoff).total_seconds() / 60)
        last = int((end - kickoff).total_seconds() / 60)
        first_aligned = math.floor(first / bin_minutes) * bin_minutes
        for offset in range(first_aligned, last, bin_minutes):
            valid_at = kickoff.astimezone(UTC) + timedelta(minutes=offset)
            points.append(
                HazardPoint(
                    offset_minutes=offset, probability=marginal, source=source, valid_at=valid_at
                )
            )
    points.sort(key=lambda item: item.offset_minutes)
    # Adjacent NWS intervals may round to same bin. Keep one point, preferring higher risk.
    unique: dict[int, HazardPoint] = {}
    for point in points:
        previous = unique.get(point.offset_minutes)
        if previous is None or point.probability > previous.probability:
            unique[point.offset_minutes] = point
    return [unique[offset] for offset in sorted(unique)]
