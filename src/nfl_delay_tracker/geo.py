"""Great-circle stadium geometry without external map services."""

from __future__ import annotations

import math

EARTH_RADIUS_MILES = 3958.7613


def distance_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Haversine great-circle distance between two WGS84 points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)
    value = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_MILES * math.asin(math.sqrt(min(1.0, value)))


def in_policy_circle(
    venue_latitude: float,
    venue_longitude: float,
    event_latitude: float,
    event_longitude: float,
    radius_miles: float,
) -> bool:
    if radius_miles < 0:
        raise ValueError("radius cannot be negative")
    return (
        distance_miles(venue_latitude, venue_longitude, event_latitude, event_longitude)
        <= radius_miles
    )


def point_on_ring(
    latitude: float, longitude: float, radius_miles: float, bearing_degrees: float
) -> tuple[float, float]:
    """Create a point at exact great-circle radius for maps and boundary checks."""
    angular_distance = radius_miles / EARTH_RADIUS_MILES
    bearing = math.radians(bearing_degrees)
    lat1 = math.radians(latitude)
    lon1 = math.radians(longitude)
    lat2 = math.asin(
        math.sin(lat1) * math.cos(angular_distance)
        + math.cos(lat1) * math.sin(angular_distance) * math.cos(bearing)
    )
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(angular_distance) * math.cos(lat1),
        math.cos(angular_distance) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), (math.degrees(lon2) + 540) % 360 - 180
