from __future__ import annotations

from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

from nfl_delay_tracker.models import Venue
from nfl_delay_tracker.providers import open_meteo
from nfl_delay_tracker.providers.http import ProviderError


@pytest.fixture
def venue() -> Venue:
    return Venue.model_validate(
        {
            "venue_id": "rio",
            "name": "Maracanã Stadium",
            "latitude": -22.9121619,
            "longitude": -43.2311861,
            "timezone": "America/Sao_Paulo",
            "roof_type": "outdoor",
            "roof_weather_protection": False,
            "policy_id": "venue_assumption_8mi_30min",
            "league": "NFL",
            "home_teams": ["NFL International Game"],
        }
    )


@pytest.mark.parametrize(("lead_hours", "expected_resolution"), [(120, 3), (240, 6)])
def test_fetch_conditions_outlook_counts_members_without_emitting_risk(
    venue: Venue,
    monkeypatch: pytest.MonkeyPatch,
    lead_hours: int,
    expected_resolution: int,
) -> None:
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = cls(2026, 9, 20, 12, tzinfo=UTC)
            return fixed.replace(tzinfo=None) if tz is None else fixed.astimezone(tz)

    monkeypatch.setattr(open_meteo, "datetime", FrozenDateTime)
    kickoff = datetime(2026, 9, 20, 12, tzinfo=UTC) + timedelta(
        hours=lead_hours, minutes=25
    )
    valid_at = kickoff.replace(minute=0, second=0, microsecond=0)
    step = expected_resolution
    times = [valid_at - timedelta(hours=step), valid_at, valid_at + timedelta(hours=step)]
    hourly: dict[str, object] = {"time": [at.isoformat() for at in times]}
    for member in range(1, 51):
        code = 0 if member <= 25 else 45 if member <= 30 else 61 if member <= 45 else 95
        hourly[f"weather_code_member{member:02d}"] = [
            code,
            None if member == 50 else code,
            code,
        ]
    requested_urls: list[str] = []

    def fake_get_json(url: str) -> dict[str, object]:
        requested_urls.append(url)
        return {"hourly": hourly, "latitude": -22.9, "longitude": -43.2}

    monkeypatch.setattr(open_meteo, "get_json", fake_get_json)
    provider = open_meteo.OpenMeteoEnsembleProvider()
    outlook, fetched_at, source_url = provider.fetch_conditions_outlook(
        venue, kickoff=kickoff
    )

    query = parse_qs(urlsplit(requested_urls[0]).query)
    assert query["models"] == ["ecmwf_ifs025_ensemble"]
    assert query["forecast_days"] == ["15"]
    assert query["timezone"] == ["GMT"]
    assert fetched_at.tzinfo is UTC
    assert source_url == requested_urls[0]
    assert outlook["valid_at"] == valid_at.isoformat()
    assert outlook["native_resolution_hours"] == expected_resolution
    assert outlook["member_count"] == 50
    assert outlook["valid_member_count"] == 49
    assert outlook["condition_member_counts"] == {
        "clear_or_cloudy": 25,
        "fog": 5,
        "precipitation_or_snow": 15,
        "other_or_unclassified": 4,
    }
    assert outlook["thunderstorm_probability"] is None
    assert outlook["storm_motion"] == "unavailable"

    provider.fetch_conditions_outlook(
        venue, kickoff=kickoff + timedelta(minutes=10)
    )
    assert len(requested_urls) == 1


def test_fetch_conditions_outlook_fails_closed_without_valid_members(
    venue: Venue, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        open_meteo,
        "get_json",
        lambda _url: {
            "hourly": {
                "time": ["2026-09-23T21:00"],
                "weather_code_member01": [None],
            }
        },
    )

    with pytest.raises(ProviderError, match="no valid member codes"):
        open_meteo.OpenMeteoEnsembleProvider().fetch_conditions_outlook(
            venue, kickoff=datetime(2026, 9, 23, 20, 25, tzinfo=UTC)
        )
