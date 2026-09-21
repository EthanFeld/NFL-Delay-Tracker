import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from nfl_delay_tracker.models import (
    Game,
    GameStatus,
    HazardPoint,
    League,
    ManualGameOverride,
    Venue,
)
from nfl_delay_tracker.pipeline import (
    _apply_manual_override,
    _dedupe_schedule_games,
    _write_game_status,
    load_registry,
    refresh_live_status,
)

NFL_TEAM_NAMES = {
    "Arizona Cardinals",
    "Atlanta Falcons",
    "Baltimore Ravens",
    "Buffalo Bills",
    "Carolina Panthers",
    "Chicago Bears",
    "Cincinnati Bengals",
    "Cleveland Browns",
    "Dallas Cowboys",
    "Denver Broncos",
    "Detroit Lions",
    "Green Bay Packers",
    "Houston Texans",
    "Indianapolis Colts",
    "Jacksonville Jaguars",
    "Kansas City Chiefs",
    "Las Vegas Raiders",
    "Los Angeles Chargers",
    "Los Angeles Rams",
    "Miami Dolphins",
    "Minnesota Vikings",
    "New England Patriots",
    "New Orleans Saints",
    "New York Giants",
    "New York Jets",
    "Philadelphia Eagles",
    "Pittsburgh Steelers",
    "San Francisco 49ers",
    "Seattle Seahawks",
    "Tampa Bay Buccaneers",
    "Tennessee Titans",
    "Washington Commanders",
}


def test_registry_resolves_all_teams_and_policies() -> None:
    venues, policies = load_registry()
    listed_home_teams = {
        name for venue in venues if League.NFL in venue.leagues for name in venue.home_teams
    }
    fbs_teams = {venue.home_teams[0] for venue in venues if League.NCAA in venue.leagues}
    assert NFL_TEAM_NAMES <= listed_home_teams
    assert len(fbs_teams) == 138
    assert all(venue.policy_id in policies for venue in venues)


def test_probabilities_and_datetimes_are_validated() -> None:
    with pytest.raises(ValidationError):
        HazardPoint(offset_minutes=0, probability=1.01, source="fixture")
    with pytest.raises(ValidationError):
        Game(
            game_id="game",
            league=League.NFL,
            season=2026,
            home_team="Home",
            away_team="Away",
            kickoff_utc=datetime(2026, 9, 20),
        )


def test_venue_rejects_invalid_coordinates() -> None:
    with pytest.raises(ValidationError):
        Venue(
            venue_id="bad",
            name="Invalid",
            latitude=120,
            longitude=0,
            timezone="UTC",
            roof_type="outdoor",
            roof_weather_protection=False,
            policy_id="policy",
            league="NFL",
            home_teams=["Home"],
        )


def test_source_timestamps_are_timezone_aware() -> None:
    game = Game(
        game_id="game",
        league=League.NFL,
        season=2026,
        home_team="Home",
        away_team="Away",
        kickoff_utc=datetime(2026, 9, 20, tzinfo=UTC),
    )
    assert game.kickoff_utc.utcoffset().total_seconds() == 0


def test_verified_manual_override_supersedes_feed_status() -> None:
    game = Game(
        game_id="game",
        league=League.NFL,
        season=2026,
        home_team="Home",
        away_team="Away",
        kickoff_utc=datetime(2026, 9, 20, tzinfo=UTC),
        status=GameStatus.IN_PROGRESS,
        status_source="espn_public_scoreboard",
    )
    override = ManualGameOverride.model_validate(
        {
            "game_id": "game",
            "official_delay_active": True,
            "delay_started_at": "2026-09-20T00:15:00Z",
            "source_note": "Team announcement",
        }
    )
    resolved = _apply_manual_override(game, override)
    assert resolved.status == GameStatus.WEATHER_DELAY
    assert resolved.official_delay_active
    assert not resolved.model_delay_active
    assert resolved.status_source == "manual_verified_override"
    assert resolved.delay_source_note == "Team announcement"


def test_manual_override_does_not_reactivate_terminal_game() -> None:
    game = Game(
        game_id="game",
        league=League.NFL,
        season=2026,
        home_team="Home",
        away_team="Away",
        kickoff_utc=datetime(2026, 9, 20, tzinfo=UTC),
        status=GameStatus.COMPLETED,
        status_source="espn_public_scoreboard",
    )
    override = ManualGameOverride.model_validate(
        {
            "game_id": "game",
            "official_delay_active": False,
            "source_note": "Old delay override",
        }
    )

    resolved = _apply_manual_override(game, override)

    assert resolved.status == GameStatus.COMPLETED
    assert resolved.status_source == "espn_public_scoreboard"
    assert not resolved.official_delay_active


def test_duplicate_neutral_game_keeps_resolved_venue_and_latest_status() -> None:
    kickoff = datetime(2026, 9, 20, 17, tzinfo=UTC)
    unresolved = Game(
        game_id="espn_1",
        league=League.NCAA,
        season=2026,
        home_team="Home University",
        away_team="Away University",
        venue_name="Unknown Bowl",
        neutral_site=True,
        kickoff_utc=kickoff,
        status=GameStatus.IN_PROGRESS,
        status_source="ESPN public scoreboard",
        home_score=7,
        away_score=3,
        period=2,
        clock="8:00",
    )
    resolved = unresolved.model_copy(
        update={
            "game_id": "cfbd_1",
            "venue_id": "bowl",
            "venue_name": "Bowl Stadium",
            "status": GameStatus.SCHEDULED,
            "status_source": "CFBD schedule",
            "home_score": None,
            "away_score": None,
            "period": None,
            "clock": None,
        }
    )

    games = _dedupe_schedule_games(
        [resolved, unresolved],
        {"home university": "home", "away university": "away"},
    )

    assert len(games) == 1
    assert games[0].venue_id == "bowl"
    assert games[0].venue_name == "Bowl Stadium"
    assert games[0].neutral_site
    assert games[0].status == GameStatus.IN_PROGRESS
    assert games[0].home_score == 7


def test_live_refresh_does_not_apply_stale_override_to_completed_game(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nfl_delay_tracker import pipeline

    kickoff = datetime.now(UTC)
    game = Game(
        game_id="game",
        league=League.NFL,
        season=kickoff.year,
        home_team="Home",
        away_team="Away",
        kickoff_utc=kickoff,
        status=GameStatus.COMPLETED,
        status_source="ESPN public scoreboard",
    )
    games_path = tmp_path / "data" / "games" / "index.json"
    games_path.parent.mkdir(parents=True)
    games_path.write_text(json.dumps({"games": [game.model_dump(mode="json")]}))
    override = ManualGameOverride.model_validate(
        {
            "game_id": "game",
            "official_delay_active": False,
            "source_note": "Old delay override",
        }
    )
    monkeypatch.setattr(pipeline, "load_registry", lambda _root: ([], {}))
    monkeypatch.setattr(pipeline, "fetch_scoreboards", lambda *_args, **_kwargs: ([game], {}))
    monkeypatch.setattr(pipeline, "_manual_overrides_from_env", lambda: {"game": override})

    refresh_live_status(root=tmp_path)

    saved = json.loads(games_path.read_text())
    assert saved["games"][0]["status"] == GameStatus.COMPLETED.value
    assert saved["games"][0]["status_source"] == "ESPN public scoreboard"


def test_live_status_write_preserves_last_observed_delay_clock(tmp_path) -> None:
    game_path = tmp_path / "data" / "games" / "game-1.json"
    game_path.parent.mkdir(parents=True)
    delay = {
        "active": True,
        "last_qualifying_event_at": "2026-09-20T18:02:00Z",
        "resume_cdf": [{"at": "2026-09-20T18:32:00Z", "probability": 0.5}],
    }
    game_path.write_text(json.dumps({"game": {"game_id": "game-1"}, "delay": delay}))

    _write_game_status(tmp_path, "game-1", {"game_id": "game-1", "status": "in_progress"})

    saved = json.loads(game_path.read_text())
    assert saved["game"]["status"] == "in_progress"
    assert saved["delay"] == delay
