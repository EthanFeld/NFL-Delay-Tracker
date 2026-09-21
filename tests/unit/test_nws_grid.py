from __future__ import annotations

from datetime import UTC, datetime

from nfl_delay_tracker.models import League, RoofType, Venue
from nfl_delay_tracker.providers import nws


def test_nws_grid_uses_valid_times_when_update_time_is_missing(monkeypatch) -> None:
    valid_start = datetime(2026, 9, 20, 19, tzinfo=UTC)
    valid_end = datetime(2026, 9, 21, 7, tzinfo=UTC)
    venue = Venue(
        venue_id="test-stadium",
        name="Test Stadium",
        latitude=39.9,
        longitude=-75.1,
        timezone="America/New_York",
        roof_type=RoofType.OUTDOOR,
        roof_weather_protection=False,
        policy_id="test-policy",
        leagues=[League.NFL],
        home_teams=["Test Team"],
    )
    provider = nws.NwsGridProvider()
    provider._grid_url_by_venue[venue.venue_id] = "https://example.test/grid"
    calls: list[bool] = []

    def fake_get_json(*_args: object, **_kwargs: object) -> dict[str, object]:
        calls.append(True)
        return {
            "properties": {
                "validTimes": "2026-09-20T19:00:00+00:00/PT12H",
                "probabilityOfThunder": {
                    "values": [
                        {
                            "value": 50,
                            "validTime": "2026-09-20T19:00:00+00:00/P7D",
                        }
                    ]
                },
            }
        }

    monkeypatch.setattr(nws, "get_json", fake_get_json)

    hazards, fetched_at, _ = provider.fetch_hazards(
        venue, kickoff=datetime(2026, 9, 20, 20, tzinfo=UTC)
    )

    assert fetched_at == valid_start
    assert hazards
    assert hazards[0].probability > 0
    assert min(point.offset_minutes for point in hazards) >= -90
    assert max(point.offset_minutes for point in hazards) <= 540
    assert valid_end > fetched_at
    provider.fetch_hazards(venue, kickoff=datetime(2026, 9, 20, 21, tzinfo=UTC))
    assert len(calls) == 1
