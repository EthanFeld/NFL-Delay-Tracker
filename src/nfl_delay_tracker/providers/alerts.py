"""Active NWS severe-thunderstorm warnings, mapped to venue coordinates.

The provider makes one filtered active-alert request per 30-second refresh window,
then checks warning polygons against every requested venue. If an alert has no
polygon, its NWS affected-zone geometries are fetched and cached for the lifetime
of the provider. Set ``NWS_CONTACT_EMAIL`` to give NWS a direct contact in the
required identifying User-Agent header.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode, urlparse

from nfl_delay_tracker.models import Venue
from nfl_delay_tracker.providers.http import ProviderError, get_json

API_ROOT = "https://api.weather.gov"
ACTIVE_URL = f"{API_ROOT}/alerts/active"
EVENT = "Severe Thunderstorm Warning"
ALERT_REFRESH_INTERVAL = timedelta(seconds=30)
ALERT_FRESHNESS_LIMIT = timedelta(seconds=60)


@dataclass(frozen=True)
class NwsAlert:
    """Active warning metadata relevant to one venue."""

    alert_id: str
    event: str
    headline: str | None
    severity: str | None
    certainty: str | None
    urgency: str | None
    area_description: str | None
    sender_name: str | None
    effective_at: datetime | None
    onset_at: datetime | None
    expires_at: datetime | None
    sent_at: datetime | None
    web_url: str | None
    matched_by: str


@dataclass(frozen=True)
class NwsAlertSnapshot:
    """Warnings mapped to a venue, plus feed timestamp and freshness."""

    venue_id: str
    fetched_at: datetime
    alerts: tuple[NwsAlert, ...]
    source_url: str
    freshness_seconds: int
    is_fresh: bool
    notes: tuple[str, ...] = ()

    @property
    def has_active_warning(self) -> bool:
        return bool(self.alerts)


class NwsSevereThunderstormWarningProvider:
    """Fetch and spatially match active NWS Severe Thunderstorm Warnings.

    Reuse one provider instance and call :meth:`fetch_for_venues` for a slate.
    This keeps alert polling to one request per refresh window instead of one
    request per venue. The NWS recommends polling its alerts service no more
    often than every 30 seconds.
    """

    def __init__(self, *, api_root: str = API_ROOT, contact_email: str | None = None) -> None:
        self.api_root = api_root.rstrip("/")
        self.active_url = f"{self.api_root}/alerts/active"
        self.contact_email = contact_email or os.getenv("NWS_CONTACT_EMAIL")
        self._features: tuple[dict[str, Any], ...] | None = None
        self._features_fetched_at: datetime | None = None
        self._zone_geometry: dict[str, dict[str, Any] | None] = {}

    @property
    def _headers(self) -> dict[str, str]:
        user_agent = "NFL Delay Tracker/0.1"
        if self.contact_email:
            user_agent += f" ({self.contact_email})"
        return {"Accept": "application/geo+json", "User-Agent": user_agent}

    def fetch_for_venue(self, venue: Venue, *, now: datetime | None = None) -> NwsAlertSnapshot:
        """Fetch active warnings and match them to one stadium."""

        return self.fetch_for_venues([venue], now=now)[venue.venue_id]

    def fetch_for_venues(
        self, venues: list[Venue], *, now: datetime | None = None
    ) -> dict[str, NwsAlertSnapshot]:
        """Return active warning metadata keyed by venue id after one alert poll."""

        observed_at = _as_utc(now or datetime.now(UTC))
        features, fetched_at = self._get_active_features(observed_at)
        results: dict[str, NwsAlertSnapshot] = {}
        for venue in venues:
            matches: list[NwsAlert] = []
            notes: list[str] = []
            for feature in features:
                properties = feature.get("properties")
                if not isinstance(properties, dict) or not _is_current_warning(
                    properties, observed_at
                ):
                    continue
                geometry = feature.get("geometry")
                match_method = "geometry"
                if geometry:
                    if not _geometry_contains(geometry, venue.longitude, venue.latitude):
                        continue
                else:
                    match_method = "affected_zone"
                    zone_match, zone_lookup_failed = self._matches_affected_zone(
                        properties, venue.longitude, venue.latitude
                    )
                    if not zone_match:
                        if zone_lookup_failed:
                            notes.append(
                                "An active warning had no polygon and its affected-zone "
                                "boundary could not be retrieved."
                            )
                        continue
                matches.append(
                    _alert_from_properties(
                        feature,
                        properties,
                        match_method,
                    )
                )
            age = max(0, int((observed_at - fetched_at).total_seconds()))
            results[venue.venue_id] = NwsAlertSnapshot(
                venue_id=venue.venue_id,
                fetched_at=fetched_at,
                alerts=tuple(sorted(matches, key=lambda alert: alert.alert_id)),
                source_url=self._request_url(),
                freshness_seconds=age,
                is_fresh=age <= int(ALERT_FRESHNESS_LIMIT.total_seconds()),
                notes=tuple(dict.fromkeys(notes)),
            )
        return results

    def _get_active_features(self, now: datetime) -> tuple[tuple[dict[str, Any], ...], datetime]:
        fetched_at = self._features_fetched_at
        if (
            self._features is not None
            and fetched_at is not None
            and timedelta(0) <= now - fetched_at < ALERT_REFRESH_INTERVAL
        ):
            return self._features, fetched_at

        payload = get_json(self._request_url(), headers=self._headers)
        if not isinstance(payload, dict):
            raise ProviderError("NWS active alerts response was not a JSON object")
        raw_features = payload.get("features")
        if raw_features is None:
            # Keep the adapter tolerant of JSON-LD indexes should NWS alter the
            # default representation despite the explicit GeoJSON Accept header.
            raw_features = payload.get("@graph", [])
        if not isinstance(raw_features, list):
            raise ProviderError("NWS active alerts response had no feature list")
        features = tuple(item for item in raw_features if isinstance(item, dict))
        self._features = features
        self._features_fetched_at = now
        return features, now

    def _request_url(self) -> str:
        query = urlencode({"event": EVENT})
        return f"{self.active_url}?{query}"

    def _matches_affected_zone(
        self, properties: dict[str, Any], longitude: float, latitude: float
    ) -> tuple[bool, bool]:
        zones = properties.get("affectedZones") or []
        lookup_failed = False
        for zone_url in zones:
            if not isinstance(zone_url, str) or not _is_nws_api_url(zone_url):
                lookup_failed = True
                continue
            if zone_url not in self._zone_geometry:
                try:
                    payload = get_json(zone_url, headers=self._headers)
                except ProviderError:
                    self._zone_geometry[zone_url] = None
                else:
                    geometry = payload.get("geometry") if isinstance(payload, dict) else None
                    self._zone_geometry[zone_url] = geometry if isinstance(geometry, dict) else None
            geometry = self._zone_geometry[zone_url]
            if geometry is None:
                lookup_failed = True
            elif _geometry_contains(geometry, longitude, latitude):
                return True, lookup_failed
        return False, lookup_failed


def _is_nws_api_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and parsed.netloc.lower() == "api.weather.gov"


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("NWS alert timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _is_current_warning(properties: dict[str, Any], now: datetime) -> bool:
    event = properties.get("event")
    if not isinstance(event, str) or event.casefold() != EVENT.casefold():
        return False
    status = properties.get("status")
    if isinstance(status, str) and status.casefold() != "actual":
        return False
    message_type = properties.get("messageType")
    if isinstance(message_type, str) and message_type.casefold() in {"cancel", "expire"}:
        return False
    start = _parse_timestamp(properties.get("effective")) or _parse_timestamp(
        properties.get("onset")
    )
    end = _parse_timestamp(properties.get("ends")) or _parse_timestamp(properties.get("expires"))
    return (start is None or start <= now) and (end is None or end > now)


def _alert_from_properties(
    feature: dict[str, Any], properties: dict[str, Any], matched_by: str
) -> NwsAlert:
    alert_id = feature.get("id") or properties.get("id") or "unknown"
    return NwsAlert(
        alert_id=str(alert_id),
        event=str(properties.get("event") or EVENT),
        headline=_as_optional_str(properties.get("headline")),
        severity=_as_optional_str(properties.get("severity")),
        certainty=_as_optional_str(properties.get("certainty")),
        urgency=_as_optional_str(properties.get("urgency")),
        area_description=_as_optional_str(properties.get("areaDesc")),
        sender_name=_as_optional_str(properties.get("senderName")),
        effective_at=_parse_timestamp(properties.get("effective")),
        onset_at=_parse_timestamp(properties.get("onset")),
        expires_at=_parse_timestamp(properties.get("ends"))
        or _parse_timestamp(properties.get("expires")),
        sent_at=_parse_timestamp(properties.get("sent")),
        web_url=_as_optional_str(properties.get("web")),
        matched_by=matched_by,
    )


def _as_optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _geometry_contains(geometry: dict[str, Any], longitude: float, latitude: float) -> bool:
    """Test a WGS84 GeoJSON Polygon or MultiPolygon, including its boundary."""

    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list):
        return False
    if geometry_type == "Polygon":
        return _polygon_contains(coordinates, longitude, latitude)
    if geometry_type == "MultiPolygon":
        return any(
            _polygon_contains(polygon, longitude, latitude)
            for polygon in coordinates
            if isinstance(polygon, list)
        )
    if geometry_type == "GeometryCollection":
        geometries = geometry.get("geometries", [])
        return isinstance(geometries, list) and any(
            isinstance(child, dict) and _geometry_contains(child, longitude, latitude)
            for child in geometries
        )
    return False


def _polygon_contains(rings: list[Any], longitude: float, latitude: float) -> bool:
    if not rings or not isinstance(rings[0], list):
        return False
    if not _ring_contains(rings[0], longitude, latitude):
        return False
    # GeoJSON rings after the first are holes.
    return not any(
        isinstance(hole, list) and _ring_contains(hole, longitude, latitude) for hole in rings[1:]
    )


def _ring_contains(ring: list[Any], longitude: float, latitude: float) -> bool:
    points: list[tuple[float, float]] = []
    for coordinate in ring:
        if not isinstance(coordinate, (list, tuple)) or len(coordinate) < 2:
            continue
        try:
            point = float(coordinate[0]), float(coordinate[1])
        except (TypeError, ValueError, OverflowError):
            continue
        points.append(point)
    if len(points) < 3:
        return False
    inside = False
    previous = points[-1]
    for current in points:
        x1, y1 = previous
        x2, y2 = current
        if _point_on_segment(longitude, latitude, x1, y1, x2, y2):
            return True
        if (y1 > latitude) != (y2 > latitude):
            crossing_longitude = (x2 - x1) * (latitude - y1) / (y2 - y1) + x1
            if longitude < crossing_longitude:
                inside = not inside
        previous = current
    return inside


def _point_on_segment(
    longitude: float,
    latitude: float,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
) -> bool:
    cross = (longitude - x1) * (y2 - y1) - (latitude - y1) * (x2 - x1)
    if abs(cross) > 1e-10:
        return False
    return (
        min(x1, x2) - 1e-10 <= longitude <= max(x1, x2) + 1e-10
        and min(y1, y2) - 1e-10 <= latitude <= max(y1, y2) + 1e-10
    )
