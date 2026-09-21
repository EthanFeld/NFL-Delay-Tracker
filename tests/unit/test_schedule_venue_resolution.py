from __future__ import annotations

from datetime import date

import pytest

from nfl_delay_tracker.models import League, Venue
from nfl_delay_tracker.pipeline import load_registry
from nfl_delay_tracker.providers.espn import normalize_event, venue_lookup, venue_name_lookup
from nfl_delay_tracker.providers.sports import (
    CollegeFootballDataProvider,
    NflverseScheduleProvider,
    fetch_cfbd_games,
)


@pytest.fixture
def venues() -> list[Venue]:
    return [
        Venue.model_validate(
            {
                "venue_id": "home",
                "name": "Home Stadium",
                "latitude": 40.0,
                "longitude": -75.0,
                "timezone": "America/New_York",
                "roof_type": "outdoor",
                "roof_weather_protection": False,
                "policy_id": "policy",
                "home_teams": ["Home University", "HOME"],
            }
        ),
        Venue.model_validate(
            {
                "venue_id": "neutral",
                "name": "Neutral Bowl Stadium",
                "latitude": 33.0,
                "longitude": -84.0,
                "timezone": "America/New_York",
                "roof_type": "outdoor",
                "roof_weather_protection": False,
                "policy_id": "policy",
                "home_teams": ["Bowl Host University"],
            }
        ),
    ]


def _espn_event(venue_name: str, *, neutral: bool) -> dict[str, object]:
    return {
        "id": "event-1",
        "date": "2026-09-20T17:00:00Z",
        "season": {"year": 2026},
        "competitions": [
            {
                "neutralSite": neutral,
                "venue": {"fullName": venue_name, "address": {"city": "Atlanta"}},
                "competitors": [
                    {"homeAway": "home", "team": {"displayName": "Home University"}},
                    {"homeAway": "away", "team": {"displayName": "Away University"}},
                ],
            }
        ],
        "status": {"type": {"state": "pre"}},
    }


def test_espn_resolves_named_neutral_site_and_preserves_teams(venues: list[Venue]) -> None:
    game = normalize_event(
        _espn_event("Neutral Bowl Stadium", neutral=True),
        league=League.NCAA,
        by_team=venue_lookup(venues),
        by_name=venue_name_lookup(venues),
    )

    assert game is not None
    assert game.venue_id == "neutral"
    assert game.neutral_site is True
    assert game.home_team == "Home University"
    assert game.venue_city == "Atlanta"


def test_espn_does_not_assign_home_stadium_to_unknown_neutral_site(
    venues: list[Venue],
) -> None:
    game = normalize_event(
        _espn_event("Unregistered Bowl Stadium", neutral=True),
        league=League.NCAA,
        by_team=venue_lookup(venues),
        by_name=venue_name_lookup(venues),
    )

    assert game is not None
    assert game.venue_id is None
    assert game.venue_name == "Unregistered Bowl Stadium"
    assert game.neutral_site is True


def test_espn_resolves_neutral_nfl_rio_game_to_maracana() -> None:
    venues, _ = load_registry()
    event = {
        "id": "401872960",
        "date": "2026-09-27T20:25:00Z",
        "season": {"year": 2026},
        "neutralSite": True,
        "competitions": [
            {
                "neutralSite": True,
                "venue": {"fullName": "Maracanã Stadium", "address": {"city": "Rio de Janeiro"}},
                "competitors": [
                    {
                        "homeAway": "home",
                        "team": {"displayName": "Dallas Cowboys", "abbreviation": "DAL"},
                    },
                    {
                        "homeAway": "away",
                        "team": {"displayName": "Baltimore Ravens", "abbreviation": "BAL"},
                    },
                ],
            }
        ],
        "status": {"type": {"state": "pre"}},
    }

    game = normalize_event(
        event,
        league=League.NFL,
        by_team=venue_lookup(venues),
        by_name=venue_name_lookup(venues),
    )

    assert game is not None
    assert game.game_id == "nfl_2026_401872960"
    assert game.venue_id == "venue_maracana_stadium"
    assert game.venue_name == "Maracanã Stadium"
    assert game.venue_city == "Rio de Janeiro"
    assert game.neutral_site is True
    assert game.home_team == "Dallas Cowboys"
    assert game.away_team == "Baltimore Ravens"


def test_nflverse_preserves_unresolved_neutral_venue(
    venues: list[Venue], monkeypatch: pytest.MonkeyPatch
) -> None:
    from nfl_delay_tracker.providers import sports

    monkeypatch.setattr(
        sports,
        "get_text",
        lambda _url: (
            "game_id,season,gameday,gametime,home_team,away_team,location,stadium\n"
            "2026_01_X,2026,2026-09-20,1:00 PM,HOME,AWAY,Neutral,Unknown Bowl Stadium\n"
        ),
    )

    games = NflverseScheduleProvider().fetch_games(venues, start=date(2026, 9, 20), days=1)

    assert len(games) == 1
    assert games[0].venue_id is None
    assert games[0].venue_name == "Unknown Bowl Stadium"
    assert games[0].neutral_site is True


def test_cfbd_preserves_unresolved_neutral_venue(
    venues: list[Venue], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        CollegeFootballDataProvider,
        "fetch_season",
        lambda _self, _year: [
            {
                "id": 1,
                "homeTeam": "Home University",
                "awayTeam": "Away University",
                "startDate": "2026-09-20T17:00:00Z",
                "neutralSite": True,
                "venue": "Unknown Bowl Stadium",
            }
        ],
    )

    games = fetch_cfbd_games("test-key", venues, year=2026)

    assert len(games) == 1
    assert games[0].venue_id is None
    assert games[0].venue_name == "Unknown Bowl Stadium"
    assert games[0].neutral_site is True
