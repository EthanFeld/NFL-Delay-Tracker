"""Conservative kinematic estimates for a tracked storm-cell centroid.

This module deliberately has no provider or pipeline dependencies. It turns a
short history of one identified MRMS reflectivity-object centroid into an
observed motion estimate relative to a venue's configured policy circle.

The centroid represents a point. Object footprint is only approximated by the
optional, static ``effective_radius_miles`` supplied with each observation;
radar echo is not itself a lightning footprint. ETA is constant-velocity
extrapolation, not a forecast or safety decision. Its bounds are sensitivity
ranges, not calibrated probability intervals: radial speed uncertainty is at
least 2 mph or 25% of the observed radial speed, whichever is greater, and may
be larger when recent segment speeds vary.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from statistics import median

from nfl_delay_tracker.geo import distance_miles
from nfl_delay_tracker.models import Venue, WeatherPolicy


class StormMotionStatus(StrEnum):
    """Availability and direction class for the latest observed track segment."""

    NO_HISTORY = "no_history"
    STALE = "stale"
    AMBIGUOUS = "ambiguous"
    APPROACHING = "approaching"
    MOVING_AWAY = "moving_away"
    UNCERTAIN = "uncertain"
    INSIDE_POLICY = "inside_policy"


@dataclass(frozen=True)
class StormCellCentroid:
    """One timestamped centroid from a consistently identified storm object."""

    track_id: str
    observed_at: datetime
    latitude: float
    longitude: float
    effective_radius_miles: float = 0.0

    def __post_init__(self) -> None:
        if not self.track_id.strip():
            raise ValueError("track_id cannot be empty")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must include a UTC offset")
        if not math.isfinite(self.latitude) or not -90 <= self.latitude <= 90:
            raise ValueError("latitude must be finite and between -90 and 90")
        if not math.isfinite(self.longitude) or not -180 <= self.longitude <= 180:
            raise ValueError("longitude must be finite and between -180 and 180")
        if not math.isfinite(self.effective_radius_miles) or self.effective_radius_miles < 0:
            raise ValueError("effective_radius_miles must be finite and nonnegative")


@dataclass(frozen=True)
class StormMotionEstimate:
    """Observed motion, venue-relative geometry, and constant-speed ETA range.

    ``radial_speed_toward_mph`` is positive when the centroid is approaching
    the venue and negative when it is receding. ETA bounds are left unset when
    the storm is already inside the policy circle, moving away, or its radial
    direction is too uncertain to support a conservative arrival estimate.
    ``distance_to_policy_boundary_miles`` is based on the newest observed
    centroid and effective radius, even for stale history; check ``status``
    and ``observation_age_minutes`` before using it as current position.
    """

    status: StormMotionStatus
    track_id: str | None
    observed_at: datetime | None
    observation_age_minutes: float | None
    bearing_degrees: float | None
    speed_mph: float | None
    radial_speed_toward_mph: float | None
    radial_speed_uncertainty_mph: float | None
    distance_to_policy_boundary_miles: float | None
    eta_minutes: float | None
    eta_lower_minutes: float | None
    eta_upper_minutes: float | None


def estimate_storm_motion(
    observations: Sequence[StormCellCentroid],
    venue: Venue,
    policy: WeatherPolicy,
    *,
    now: datetime,
    max_observation_age: timedelta = timedelta(minutes=15),
    max_track_gap: timedelta = timedelta(minutes=20),
    max_observation_lead: timedelta = timedelta(minutes=2),
) -> StormMotionEstimate:
    """Estimate latest centroid movement toward a venue policy boundary.

    Observations may arrive out of order, but must all belong to one track and
    have unique timestamps. A long gap, implausible speed, mixed track IDs, or
    a timestamp materially in the future returns ``ambiguous``. A fresh track
    needs at least two observations before motion or ETA is reported.
    """
    _require_aware("now", now)
    if max_observation_age < timedelta(0):
        raise ValueError("max_observation_age cannot be negative")
    if max_track_gap <= timedelta(0):
        raise ValueError("max_track_gap must be positive")
    if max_observation_lead < timedelta(0):
        raise ValueError("max_observation_lead cannot be negative")

    if not observations:
        return _empty_estimate(StormMotionStatus.NO_HISTORY)

    ordered = sorted(observations, key=lambda item: item.observed_at)
    track_ids = {item.track_id for item in ordered}
    if len(track_ids) != 1:
        return _empty_estimate(StormMotionStatus.AMBIGUOUS)

    latest = ordered[-1]
    age = now.astimezone(latest.observed_at.tzinfo) - latest.observed_at
    age_minutes = age.total_seconds() / 60
    track_id = latest.track_id
    distance_to_boundary = _boundary_distance(latest, venue, policy)

    if age < -max_observation_lead:
        return _empty_estimate(StormMotionStatus.AMBIGUOUS, track_id=track_id)
    if age > max_observation_age:
        return StormMotionEstimate(
            status=StormMotionStatus.STALE,
            track_id=track_id,
            observed_at=latest.observed_at,
            observation_age_minutes=age_minutes,
            bearing_degrees=None,
            speed_mph=None,
            radial_speed_toward_mph=None,
            radial_speed_uncertainty_mph=None,
            distance_to_policy_boundary_miles=distance_to_boundary,
            eta_minutes=None,
            eta_lower_minutes=None,
            eta_upper_minutes=None,
        )

    if len(ordered) < 2:
        return StormMotionEstimate(
            status=StormMotionStatus.NO_HISTORY,
            track_id=track_id,
            observed_at=latest.observed_at,
            observation_age_minutes=age_minutes,
            bearing_degrees=None,
            speed_mph=None,
            radial_speed_toward_mph=None,
            radial_speed_uncertainty_mph=None,
            distance_to_policy_boundary_miles=distance_to_boundary,
            eta_minutes=None,
            eta_lower_minutes=None,
            eta_upper_minutes=None,
        )

    if any(
        current.observed_at <= previous.observed_at
        for previous, current in zip(ordered, ordered[1:], strict=False)
    ):
        return _empty_estimate(StormMotionStatus.AMBIGUOUS, track_id=track_id)

    if any(
        current.observed_at - previous.observed_at > max_track_gap
        for previous, current in zip(ordered, ordered[1:], strict=False)
    ):
        return _empty_estimate(StormMotionStatus.AMBIGUOUS, track_id=track_id)

    segments = list(zip(ordered, ordered[1:], strict=False))
    segment_speeds: list[float] = []
    segment_radial_speeds: list[float] = []
    for previous, current in segments:
        elapsed_hours = (current.observed_at - previous.observed_at).total_seconds() / 3600
        segment_distance = distance_miles(
            previous.latitude,
            previous.longitude,
            current.latitude,
            current.longitude,
        )
        previous_range = _boundary_distance(previous, venue, policy)
        current_range = _boundary_distance(current, venue, policy)
        segment_speeds.append(segment_distance / elapsed_hours)
        segment_radial_speeds.append((previous_range - current_range) / elapsed_hours)

    previous, current = segments[-1]
    speed = segment_speeds[-1]
    bearing = (
        _bearing_degrees(
            previous.latitude,
            previous.longitude,
            current.latitude,
            current.longitude,
        )
        if speed > 0.1
        else None
    )
    radial_speed = segment_radial_speeds[-1]
    radial_uncertainty = max(
        2.0,
        0.25 * abs(radial_speed),
        _scaled_mad(segment_radial_speeds),
    )

    # A very fast segment or nearly unchanged timestamps usually means bad
    # object association. Avoid turning it into a highly confident ETA.
    if any(segment_speed > 100.0 for segment_speed in segment_speeds):
        return _empty_estimate(StormMotionStatus.AMBIGUOUS, track_id=track_id)

    if distance_to_boundary <= 0:
        status = StormMotionStatus.INSIDE_POLICY
        eta: float | None = 0.0
        eta_lower: float | None = 0.0
        eta_upper: float | None = 0.0
    elif radial_speed > radial_uncertainty:
        status = StormMotionStatus.APPROACHING
        eta = distance_to_boundary / radial_speed * 60
        eta_lower = distance_to_boundary / (radial_speed + radial_uncertainty) * 60
        slower_speed = radial_speed - radial_uncertainty
        eta_upper = distance_to_boundary / slower_speed * 60 if slower_speed > 0 else None
    elif radial_speed < -radial_uncertainty:
        status = StormMotionStatus.MOVING_AWAY
        eta = eta_lower = eta_upper = None
    else:
        status = StormMotionStatus.UNCERTAIN
        eta = eta_lower = eta_upper = None

    return StormMotionEstimate(
        status=status,
        track_id=track_id,
        observed_at=latest.observed_at,
        observation_age_minutes=age_minutes,
        bearing_degrees=bearing,
        speed_mph=speed,
        radial_speed_toward_mph=radial_speed,
        radial_speed_uncertainty_mph=radial_uncertainty,
        distance_to_policy_boundary_miles=distance_to_boundary,
        eta_minutes=eta,
        eta_lower_minutes=eta_lower,
        eta_upper_minutes=eta_upper,
    )


def _boundary_distance(
    observation: StormCellCentroid, venue: Venue, policy: WeatherPolicy
) -> float:
    center_distance = distance_miles(
        venue.latitude,
        venue.longitude,
        observation.latitude,
        observation.longitude,
    )
    return max(
        0.0,
        center_distance - policy.trigger_radius_miles - observation.effective_radius_miles,
    )


def _bearing_degrees(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial great-circle bearing from point 1 to point 2 (clockwise from N)."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_lambda = math.radians((lon2 - lon1 + 180) % 360 - 180)
    east_component = math.sin(delta_lambda) * math.cos(phi2)
    north_component = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(
        delta_lambda
    )
    return math.degrees(math.atan2(east_component, north_component)) % 360


def _scaled_mad(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    center = median(values)
    return 1.4826 * median([abs(value - center) for value in values])


def _require_aware(name: str, value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a UTC offset")


def _empty_estimate(
    status: StormMotionStatus, *, track_id: str | None = None
) -> StormMotionEstimate:
    return StormMotionEstimate(
        status=status,
        track_id=track_id,
        observed_at=None,
        observation_age_minutes=None,
        bearing_degrees=None,
        speed_mph=None,
        radial_speed_toward_mph=None,
        radial_speed_uncertainty_mph=None,
        distance_to_policy_boundary_miles=None,
        eta_minutes=None,
        eta_lower_minutes=None,
        eta_upper_minutes=None,
    )
