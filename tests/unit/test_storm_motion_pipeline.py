from datetime import UTC, datetime, timedelta

from nfl_delay_tracker.geo import point_on_ring
from nfl_delay_tracker.models import HazardPoint, League
from nfl_delay_tracker.pipeline import (
    _motion_adjusted_hazards,
    _storm_motion_features,
    load_registry,
)


def test_recent_reflectivity_centroids_are_matched_and_track_eta() -> None:
    venues, policies = load_registry()
    venue = next(item for item in venues if League.NFL in item.leagues)
    policy = policies[venue.policy_id]
    now = datetime(2026, 9, 20, 18, 0, tzinfo=UTC)
    previous_at = now - timedelta(minutes=10)
    old_lat, old_lon = point_on_ring(venue.latitude, venue.longitude, 30, 90)
    new_lat, new_lon = point_on_ring(venue.latitude, venue.longitude, 25, 90)
    previous = {
        "observed_at": previous_at.isoformat(),
        "objects": [{"track_id": "radar-cell", "latitude": old_lat, "longitude": old_lon}],
        "track_history": {
            "radar-cell": [
                {
                    "track_id": "radar-cell",
                    "observed_at": previous_at.isoformat(),
                    "latitude": old_lat,
                    "longitude": old_lon,
                    "effective_radius_miles": 0.3,
                }
            ]
        },
    }
    current = {
        "valid_at": now,
        "coverage_cells": 300,
        "objects": [
            {
                "latitude": new_lat,
                "longitude": new_lon,
                "effective_radius_miles": 0.3,
                "max_reflectivity_dbz": 45.0,
                "cells": 4,
            }
        ],
    }

    motion = _storm_motion_features(previous, current, venue, policy, now=now)

    assert motion["status"] == "approaching"
    assert motion["track_id"] == "radar-cell"
    assert motion["eta_lower_minutes"] < motion["eta_minutes"] < motion["eta_upper_minutes"]
    assert motion["observation_age_minutes"] == 0


def test_approaching_echo_adds_capped_risk_only_inside_priority_window() -> None:
    now = datetime(2026, 9, 20, 18, 0, tzinfo=UTC)
    hazards = [
        HazardPoint(
            offset_minutes=minute,
            probability=0.01,
            source="SPC HREF CT",
            valid_at=now + timedelta(minutes=minute),
        )
        for minute in range(0, 181, 5)
    ]
    motion = {
        "tracks": [
            {
                "track_id": "approaching-cell",
                "status": "approaching",
                "observed_at": now.isoformat(),
                "eta_minutes": 90.0,
                "eta_lower_minutes": 70.0,
                "eta_upper_minutes": 110.0,
                "max_reflectivity_dbz": 45.0,
            }
        ]
    }

    adjusted, audit = _motion_adjusted_hazards(hazards, motion, now=now)
    values = {point.offset_minutes: point.probability for point in adjusted}

    assert audit["applied"]
    assert audit["tracks_used"] == 1
    assert values[0] == 0.01
    assert values[30] == 0.01
    assert values[60] > 0.01
    assert values[90] > 0.01
    assert values[120] > 0.01
    assert values[125] == 0.01
    assert values[180] == 0.01
    assert audit["maximum_added_hazard_probability"] < 0.15


def test_no_motion_adjustment_without_fresh_approaching_track() -> None:
    now = datetime(2026, 9, 20, 18, 0, tzinfo=UTC)
    hazards = [
        HazardPoint(
            offset_minutes=minute,
            probability=0.01,
            source="SPC HREF CT",
            valid_at=now + timedelta(minutes=minute),
        )
        for minute in range(0, 181, 5)
    ]
    motion = {
        "tracks": [
            {
                "track_id": "outgoing-cell",
                "status": "moving_away",
                "observed_at": now.isoformat(),
                "eta_minutes": None,
            }
        ]
    }

    adjusted, audit = _motion_adjusted_hazards(hazards, motion, now=now)

    assert not audit["applied"]
    assert adjusted == hazards
