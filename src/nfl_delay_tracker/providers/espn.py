"""Server-side ESPN public scoreboard adapter for schedules and live status."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from nfl_delay_tracker.models import Game, GameStatus, League, Venue
from nfl_delay_tracker.providers.http import get_json

_BASE = "https://site.api.espn.com/apis/site/v2/sports/football"


def _normalized(value: str) -> str:
    return " ".join("".join(char.lower() if char.isalnum() else " " for char in value).split())


def venue_lookup(venues: list[Venue]) -> dict[str, Venue]:
    lookup: dict[str, Venue] = {}
    for venue in venues:
        for team in venue.home_teams:
            lookup[_normalized(team)] = venue
    return lookup


def venue_name_lookup(venues: list[Venue]) -> dict[str, Venue]:
    lookup: dict[str, Venue] = {}
    for venue in venues:
        for name in (venue.name, *venue.aliases):
            lookup[_normalized(name)] = venue
    return lookup


def _status(event: dict[str, Any], competition: dict[str, Any]) -> tuple[GameStatus, bool]:
    status = event.get("status", {}).get("type", {})
    state = status.get("state")
    description = " ".join(
        str(value)
        for value in (
            status.get("description"),
            status.get("detail"),
            competition.get("status", {}).get("type", {}).get("description"),
        )
        if value
    ).lower()
    official_weather_delay = any(
        phrase in description
        for phrase in ("weather delay", "lightning delay", "weather suspension")
    )
    if official_weather_delay:
        return GameStatus.WEATHER_DELAY, True
    if state == "pre":
        return GameStatus.SCHEDULED, False
    if state == "in":
        return GameStatus.IN_PROGRESS, False
    if state == "post":
        if any(word in description for word in ("postponed", "cancelled", "canceled")):
            return (
                GameStatus.POSTPONED if "postponed" in description else GameStatus.CANCELLED
            ), False
        return GameStatus.COMPLETED, False
    if any(word in description for word in ("postponed", "cancelled", "canceled")):
        return (GameStatus.POSTPONED if "postponed" in description else GameStatus.CANCELLED), False
    return GameStatus.UNKNOWN, False


def normalize_event(
    event: dict[str, Any],
    league: League,
    by_team: dict[str, Venue],
    by_name: dict[str, Venue] | None = None,
) -> Game | None:
    competitions = event.get("competitions") or []
    if not competitions:
        return None
    competition = competitions[0]
    competitors = competition.get("competitors") or []
    home = next((item for item in competitors if item.get("homeAway") == "home"), None)
    away = next((item for item in competitors if item.get("homeAway") == "away"), None)
    if not home or not away:
        return None
    home_team = home.get("team", {}).get("displayName") or home.get("team", {}).get("name")
    away_team = away.get("team", {}).get("displayName") or away.get("team", {}).get("name")
    if not home_team or not away_team:
        return None
    home_venue = by_team.get(_normalized(home_team))
    if home_venue is None:
        abbreviation = home.get("team", {}).get("abbreviation")
        home_venue = by_team.get(_normalized(abbreviation)) if abbreviation else None
    venue_meta = competition.get("venue") or event.get("venue") or {}
    if isinstance(venue_meta, str):
        venue_name = venue_meta.strip() or None
        venue_city = None
    elif isinstance(venue_meta, dict):
        venue_name = (
            venue_meta.get("fullName") or venue_meta.get("displayName") or venue_meta.get("name")
        )
        address = venue_meta.get("address") or {}
        venue_city = address.get("city") if isinstance(address, dict) else None
    else:
        venue_name = None
        venue_city = None
    if venue_name is not None:
        venue_name = str(venue_name).strip() or None
    neutral_site = bool(competition.get("neutralSite") or event.get("neutralSite"))
    named_venue = (by_name or {}).get(_normalized(venue_name)) if venue_name else None
    venue = named_venue if named_venue is not None else (None if neutral_site else home_venue)
    try:
        kickoff = datetime.fromisoformat(str(event["date"]).replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=UTC)
    status, official_delay = _status(event, competition)
    season = int(event.get("season", {}).get("year") or kickoff.year)
    try:
        home_score = int(home["score"]) if home.get("score") not in (None, "") else None
    except (ValueError, KeyError):
        home_score = None
    try:
        away_score = int(away["score"]) if away.get("score") not in (None, "") else None
    except (ValueError, KeyError):
        away_score = None
    status_info = event.get("status", {})
    return Game(
        game_id=(
            f"{league.value.lower()}_{season}_{event.get('id') or kickoff.strftime('%Y%m%d%H%M')}"
        ),
        league=league,
        season=season,
        home_team=home_team,
        away_team=away_team,
        venue_id=venue.venue_id if venue else None,
        venue_name=venue.name if venue else venue_name,
        venue_city=str(venue_city) if venue_city else None,
        neutral_site=neutral_site,
        kickoff_utc=kickoff.astimezone(UTC),
        status=status,
        status_source="ESPN public scoreboard; unofficial",
        official_delay_active=official_delay,
        home_score=home_score,
        away_score=away_score,
        period=status_info.get("period"),
        clock=status_info.get("displayClock"),
    )


def fetch_scoreboards(
    venues: list[Venue], *, start: date | None = None, days: int = 14
) -> tuple[list[Game], dict[str, dict[str, str]]]:
    """Fetch daily NFL/FBS scoreboards; credentials never enter frontend."""
    if days < 1 or days > 32:
        raise ValueError("days must be between 1 and 32")
    start = start or (datetime.now(UTC).date() - timedelta(days=1))
    by_team = venue_lookup(venues)
    by_name = venue_name_lookup(venues)
    games: dict[str, Game] = {}
    health: dict[str, dict[str, str]] = {}
    for league, path, extra in (
        (League.NFL, "nfl", {}),
        (League.NCAA, "college-football", {"groups": "80"}),
    ):
        provider_name = "nfl_scoreboard" if league is League.NFL else "college_scoreboard"
        success_at = datetime.now(UTC).isoformat()
        failures: list[str] = []
        for day_index in range(days):
            target_day = start + timedelta(days=day_index)
            params = {"dates": target_day.strftime("%Y%m%d"), "limit": "1000", **extra}
            url = f"{_BASE}/{path}/scoreboard?{urlencode(params)}"
            try:
                response = get_json(url)
                for event in response.get("events", []):
                    game = normalize_event(event, league, by_team, by_name)
                    if game:
                        games[game.game_id] = game
            except Exception as exc:  # provider failure should not hide other days/leagues
                failures.append(f"{target_day.isoformat()}: {exc}")
        health[provider_name] = {
            "status": "ok" if not failures else ("degraded" if games else "error"),
            "updated_at": success_at,
            "message": "; ".join(failures[:3])
            if failures
            else "Public scoreboard; unofficial source",
        }
    ordered = sorted(games.values(), key=lambda game: game.kickoff_utc)
    return ordered, health
