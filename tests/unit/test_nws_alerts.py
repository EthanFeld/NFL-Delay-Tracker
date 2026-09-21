from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from nfl_delay_tracker.models import RoofType, Venue
from nfl_delay_tracker.providers import alerts
from nfl_delay_tracker.providers.alerts import NwsSevereThunderstormWarningProvider

NOW = datetime(2026, 9, 20, 20, 0, tzinfo=UTC)
POLYGON = {
    "type": "Polygon",
    "coordinates": [[[-76.0, 39.0], [-74.0, 39.0], [-74.0, 41.0], [-76.0, 41.0], [-76.0, 39.0]]],
}


def _venue(venue_id: str, latitude: float, longitude: float) -> Venue:
    return Venue(
        venue_id=venue_id,
        name=venue_id,
        latitude=latitude,
        longitude=longitude,
        timezone="America/New_York",
        roof_type=RoofType.OUTDOOR,
        roof_weather_protection=False,
        policy_id="policy",
        home_teams=["TEAM"],
    )


def _feature(
    *,
    alert_id: str = "https://api.weather.gov/alerts/1",
    geometry: dict[str, Any] | None = POLYGON,
    event: str = "Severe Thunderstorm Warning",
    expires: datetime = NOW + timedelta(minutes=20),
    affected_zones: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": alert_id,
        "geometry": geometry,
        "properties": {
            "event": event,
            "status": "Actual",
            "messageType": "Alert",
            "effective": (NOW - timedelta(minutes=5)).isoformat(),
            "onset": (NOW - timedelta(minutes=5)).isoformat(),
            "expires": expires.isoformat(),
            "sent": (NOW - timedelta(minutes=5)).isoformat(),
            "headline": "Severe thunderstorm warning until 4:20 PM EDT",
            "severity": "Severe",
            "certainty": "Observed",
            "urgency": "Immediate",
            "areaDesc": "Central County",
            "senderName": "NWS Example Office",
            "web": alert_id,
            "affectedZones": affected_zones or [],
        },
    }


def test_active_warning_geometry_maps_to_requested_venue_and_reports_metadata(
    monkeypatch: Any,
) -> None:
    calls: list[tuple[str, dict[str, str]]] = []

    def fake_get_json(url: str, *, headers: dict[str, str]) -> dict[str, Any]:
        calls.append((url, headers))
        return {"features": [_feature()]}

    monkeypatch.setattr(alerts, "get_json", fake_get_json)
    provider = NwsSevereThunderstormWarningProvider(contact_email="ops@example.org")
    venue = _venue("near", 40.0, -75.0)

    result = provider.fetch_for_venue(venue, now=NOW)

    assert result.has_active_warning
    assert result.is_fresh
    assert result.freshness_seconds == 0
    assert result.alerts[0].matched_by == "geometry"
    assert result.alerts[0].severity == "Severe"
    assert result.alerts[0].certainty == "Observed"
    assert result.alerts[0].expires_at == NOW + timedelta(minutes=20)
    assert len(calls) == 1
    assert "event=Severe+Thunderstorm+Warning" in calls[0][0]
    assert calls[0][1]["Accept"] == "application/geo+json"
    assert calls[0][1]["User-Agent"] == "NFL Delay Tracker/0.1 (ops@example.org)"


def test_geometry_excludes_other_venues_and_noncurrent_events(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        alerts,
        "get_json",
        lambda *_args, **_kwargs: {
            "features": [
                _feature(),
                _feature(alert_id="expired", expires=NOW),
                _feature(alert_id="watch", event="Severe Thunderstorm Watch"),
            ]
        },
    )
    provider = NwsSevereThunderstormWarningProvider()
    nearby = _venue("near", 40.0, -75.0)
    far_away = _venue("far", 33.0, -84.0)

    result = provider.fetch_for_venues([nearby, far_away], now=NOW)

    assert [item.alert_id for item in result["near"].alerts] == ["https://api.weather.gov/alerts/1"]
    assert result["far"].alerts == ()
    assert result["far"].is_fresh


def test_missing_alert_polygon_uses_affected_zone_geometry(monkeypatch: Any) -> None:
    zone_url = "https://api.weather.gov/zones/county/NYC001"
    calls: list[str] = []

    def fake_get_json(url: str, *, headers: dict[str, str]) -> dict[str, Any]:
        calls.append(url)
        if url == zone_url:
            return {"type": "Feature", "geometry": POLYGON}
        return {
            "features": [
                _feature(geometry=None, affected_zones=[zone_url]),
                _feature(alert_id="second", geometry=None, affected_zones=[zone_url]),
            ]
        }

    monkeypatch.setattr(alerts, "get_json", fake_get_json)
    provider = NwsSevereThunderstormWarningProvider()
    first = _venue("one", 40.0, -75.0)
    second = _venue("two", 40.2, -74.9)

    results = provider.fetch_for_venues([first, second], now=NOW)

    assert [item.matched_by for item in results["one"].alerts] == [
        "affected_zone",
        "affected_zone",
    ]
    assert len([url for url in calls if url == zone_url]) == 1
    assert (
        len(
            [
                url
                for url in calls
                if url.endswith("/alerts/active?event=Severe+Thunderstorm+Warning")
            ]
        )
        == 1
    )


def test_provider_reuses_cached_alert_index_for_thirty_seconds(monkeypatch: Any) -> None:
    calls = 0

    def fake_get_json(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"features": []}

    monkeypatch.setattr(alerts, "get_json", fake_get_json)
    provider = NwsSevereThunderstormWarningProvider()
    venue = _venue("venue", 40.0, -75.0)

    first = provider.fetch_for_venue(venue, now=NOW)
    second = provider.fetch_for_venue(venue, now=NOW + timedelta(seconds=29))

    assert calls == 1
    assert first.fetched_at == second.fetched_at == NOW
    assert second.freshness_seconds == 29
    assert second.is_fresh
