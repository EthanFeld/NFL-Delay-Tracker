from datetime import UTC, datetime, timedelta

import pytest

from nfl_delay_tracker.geo import point_on_ring
from nfl_delay_tracker.model.storm_motion import (
    StormCellCentroid,
    StormMotionStatus,
    estimate_storm_motion,
)
from nfl_delay_tracker.models import (
    League,
    PolicyVerification,
    RestartOverhead,
    RoofType,
    Venue,
    WeatherPolicy,
)

_VENUE = Venue(
    venue_id="test-stadium",
    name="Test Stadium",
    latitude=40.0,
    longitude=-75.0,
    timezone="America/New_York",
    roof_type=RoofType.OUTDOOR,
    roof_weather_protection=False,
    policy_id="test-policy",
    league=League.NFL,
    home_teams=["TEST"],
)
_POLICY = WeatherPolicy(
    policy_id="test-policy",
    trigger_radius_miles=8.0,
    monitor_radii_miles=[10.0, 15.0, 20.0],
    quiet_period_minutes=30,
    warmup_exposure_minutes=60,
    clearance_mode="fixed_quiet_period",
    restart_overhead=RestartOverhead(minutes=[5], weights=[1.0]),
    verification=PolicyVerification.UNKNOWN,
    notes="test fixture",
)
_NOW = datetime(2026, 9, 20, 18, 0, tzinfo=UTC)


def _centroid(bearing: float, radius: float, at: datetime, track_id: str = "cell-a"):
    latitude, longitude = point_on_ring(_VENUE.latitude, _VENUE.longitude, radius, bearing)
    return StormCellCentroid(
        track_id=track_id,
        observed_at=at,
        latitude=latitude,
        longitude=longitude,
    )


@pytest.mark.parametrize(
    ("origin_bearing", "expected_heading"),
    [(0.0, 180.0), (90.0, 270.0), (180.0, 0.0), (270.0, 90.0)],
    ids=["north-to-south", "east-to-west", "south-to-north", "west-to-east"],
)
def test_cardinal_cell_motion_has_correct_bearing_and_approach(
    origin_bearing: float, expected_heading: float
) -> None:
    observations = [
        _centroid(origin_bearing, 20, _NOW - timedelta(minutes=10)),
        _centroid(origin_bearing, 15, _NOW),
    ]

    estimate = estimate_storm_motion(observations, _VENUE, _POLICY, now=_NOW)

    bearing_error = (estimate.bearing_degrees - expected_heading + 180) % 360 - 180
    # East/west paths converge slightly on a sphere at this latitude.
    assert abs(bearing_error) < 0.3
    assert estimate.status == StormMotionStatus.APPROACHING
    assert estimate.radial_speed_toward_mph == pytest.approx(30.0, abs=0.1)


def test_eta_to_policy_boundary_includes_conservative_speed_range() -> None:
    observations = [
        _centroid(90, 20, _NOW - timedelta(minutes=10)),
        _centroid(90, 15, _NOW),
    ]

    estimate = estimate_storm_motion(observations, _VENUE, _POLICY, now=_NOW)

    assert estimate.distance_to_policy_boundary_miles == pytest.approx(7.0, abs=0.01)
    assert estimate.eta_minutes == pytest.approx(14.0, abs=0.1)
    assert estimate.eta_lower_minutes < estimate.eta_minutes
    assert estimate.eta_upper_minutes > estimate.eta_minutes


def test_no_history_returns_unknown_motion() -> None:
    estimate = estimate_storm_motion([], _VENUE, _POLICY, now=_NOW)

    assert estimate.status == StormMotionStatus.NO_HISTORY
    assert estimate.speed_mph is None
    assert estimate.eta_minutes is None


def test_one_centroid_reports_location_but_not_motion() -> None:
    estimate = estimate_storm_motion([_centroid(0, 20, _NOW)], _VENUE, _POLICY, now=_NOW)

    assert estimate.status == StormMotionStatus.NO_HISTORY
    assert estimate.distance_to_policy_boundary_miles == pytest.approx(12.0, abs=0.01)
    assert estimate.speed_mph is None


def test_stale_track_preserves_observed_boundary_distance_only() -> None:
    stale_at = _NOW - timedelta(minutes=20)
    estimate = estimate_storm_motion(
        [
            _centroid(90, 21, stale_at - timedelta(minutes=5)),
            _centroid(90, 20, stale_at),
        ],
        _VENUE,
        _POLICY,
        now=_NOW,
    )

    assert estimate.status == StormMotionStatus.STALE
    assert estimate.distance_to_policy_boundary_miles == pytest.approx(12.0, abs=0.01)
    assert estimate.observation_age_minutes == pytest.approx(20)
    assert estimate.radial_speed_toward_mph is None
    assert estimate.eta_minutes is None


def test_mixed_track_ids_are_ambiguous() -> None:
    observations = [
        _centroid(90, 20, _NOW - timedelta(minutes=5), "cell-a"),
        _centroid(90, 18, _NOW, "cell-b"),
    ]

    estimate = estimate_storm_motion(observations, _VENUE, _POLICY, now=_NOW)

    assert estimate.status == StormMotionStatus.AMBIGUOUS
    assert estimate.bearing_degrees is None
    assert estimate.eta_minutes is None


def test_large_gap_is_ambiguous_instead_of_projected() -> None:
    observations = [
        _centroid(90, 20, _NOW - timedelta(minutes=30)),
        _centroid(90, 15, _NOW),
    ]

    estimate = estimate_storm_motion(observations, _VENUE, _POLICY, now=_NOW)

    assert estimate.status == StormMotionStatus.AMBIGUOUS
    assert estimate.speed_mph is None
    assert estimate.eta_minutes is None


def test_inside_policy_circle_has_zero_eta() -> None:
    observations = [
        _centroid(90, 10, _NOW - timedelta(minutes=5)),
        _centroid(90, 7, _NOW),
    ]

    estimate = estimate_storm_motion(observations, _VENUE, _POLICY, now=_NOW)

    assert estimate.status == StormMotionStatus.INSIDE_POLICY
    assert estimate.distance_to_policy_boundary_miles == 0.0
    assert estimate.eta_minutes == 0.0


def test_moving_away_does_not_get_an_arrival_eta() -> None:
    observations = [
        _centroid(90, 15, _NOW - timedelta(minutes=10)),
        _centroid(90, 20, _NOW),
    ]

    estimate = estimate_storm_motion(observations, _VENUE, _POLICY, now=_NOW)

    assert estimate.status == StormMotionStatus.MOVING_AWAY
    assert estimate.radial_speed_toward_mph < 0
    assert estimate.eta_minutes is None


def test_effective_object_radius_reduces_distance_to_boundary() -> None:
    first = _centroid(90, 20, _NOW - timedelta(minutes=5))
    second = StormCellCentroid(
        track_id="cell-a",
        observed_at=_NOW,
        latitude=point_on_ring(_VENUE.latitude, _VENUE.longitude, 15, 90)[0],
        longitude=point_on_ring(_VENUE.latitude, _VENUE.longitude, 15, 90)[1],
        effective_radius_miles=3.0,
    )

    estimate = estimate_storm_motion([first, second], _VENUE, _POLICY, now=_NOW)

    assert estimate.distance_to_policy_boundary_miles == pytest.approx(4.0, abs=0.01)


def test_naive_now_is_rejected() -> None:
    with pytest.raises(ValueError, match="UTC offset"):
        estimate_storm_motion([], _VENUE, _POLICY, now=datetime(2026, 9, 20, 18, 0))
