"""Replaceable sports schedule/status providers."""

from __future__ import annotations

import csv
import io
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from nfl_delay_tracker.models import Game, GameStatus, League
from nfl_delay_tracker.providers.espn import _normalized, venue_lookup, venue_name_lookup
from nfl_delay_tracker.providers.http import get_json, get_text


class SportsProvider(Protocol):
    def fetch_games(self) -> list[Game]: ...


class LiveGameStatusProvider(Protocol):
    def fetch_status(self, game_ids: list[str]) -> dict[str, dict[str, Any]]: ...


class CollegeFootballDataProvider:
    """Authenticated CFBD schedule adapter. Never expose key to frontend."""

    def __init__(self, api_key: str) -> None:
        if not api_key.strip():
            raise ValueError("CFBD API key required")
        self.api_key = api_key.strip()

    def fetch_season(self, year: int) -> list[dict[str, Any]]:
        games: list[dict[str, Any]] = []
        for season_type in ("regular", "postseason"):
            url = "https://api.collegefootballdata.com/games?" + urlencode(
                {"year": year, "seasonType": season_type}
            )
            result = get_json(url, headers={"Authorization": f"Bearer {self.api_key}"})
            if isinstance(result, list):
                games.extend(result)
        return games


class NflverseScheduleProvider:
    """Public nflverse schedule metadata adapter; not an official NFL feed."""

    url = "https://github.com/nflverse/nfldata/raw/refs/heads/master/data/games.csv"

    def fetch_games(self, venues: list[Any], *, start: date, days: int) -> list[Game]:
        payload = get_text(self.url)
        reader = csv.DictReader(io.StringIO(payload))
        by_team = venue_lookup(venues)
        by_name = venue_name_lookup(venues)
        first_date = start
        last_date = start + timedelta(days=days)
        games = []
        for row in reader:
            raw_date = row.get("gameday") or ""
            try:
                game_date = date.fromisoformat(raw_date[:10])
            except ValueError:
                continue
            if not first_date <= game_date <= last_date:
                continue
            home = row.get("home_team") or ""
            away = row.get("away_team") or ""
            home_venue = by_team.get(_normalized(home))
            stadium_name = (row.get("stadium") or "").strip() or None
            neutral_site = (row.get("location") or "").strip().lower() == "neutral"
            named_venue = by_name.get(_normalized(stadium_name)) if stadium_name else None
            venue = (
                named_venue if named_venue is not None else (None if neutral_site else home_venue)
            )
            away_venue = by_team.get(_normalized(away))
            home_name = (
                venue.home_teams[0] if venue and venue.home_teams and not neutral_site else home
            )
            away_name = away_venue.home_teams[0] if away_venue and away_venue.home_teams else away
            raw_time = (row.get("gametime") or "").strip()
            try:
                local_kickoff = datetime.strptime(
                    f"{game_date.isoformat()} {raw_time}", "%Y-%m-%d %I:%M %p"
                ).replace(tzinfo=ZoneInfo(venue.timezone if venue else "America/New_York"))
            except ValueError:
                continue
            home_score = _optional_int(row.get("home_score"))
            away_score = _optional_int(row.get("away_score"))
            status = (
                GameStatus.COMPLETED
                if home_score is not None and away_score is not None
                else GameStatus.SCHEDULED
            )
            season = int(row.get("season") or game_date.year)
            games.append(
                Game(
                    game_id=f"nfl_{season}_{row.get('game_id') or game_date.strftime('%Y%m%d')}",
                    league=League.NFL,
                    season=season,
                    home_team=home_name,
                    away_team=away_name,
                    venue_id=venue.venue_id if venue else None,
                    venue_name=venue.name if venue else stadium_name,
                    neutral_site=neutral_site,
                    kickoff_utc=local_kickoff.astimezone(UTC),
                    status=status,
                    status_source="nflverse schedule metadata; not official live status",
                    home_score=home_score,
                    away_score=away_score,
                )
            )
        return games


def fetch_cfbd_games(api_key: str, venues: list[Any], *, year: int) -> list[Game]:
    """Normalize CFBD FBS schedule results to canonical Game values."""
    from nfl_delay_tracker.models import GameStatus, League

    provider = CollegeFootballDataProvider(api_key)
    by_team = venue_lookup(venues)
    by_name = venue_name_lookup(venues)
    games: list[Game] = []
    for row in provider.fetch_season(year):
        home = str(row.get("homeTeam") or "").strip()
        away = str(row.get("awayTeam") or "").strip()
        date_value = row.get("startDate") or row.get("startTime")
        if not home or not away or not date_value:
            continue
        try:
            kickoff = datetime.fromisoformat(str(date_value).replace("Z", "+00:00"))
        except ValueError:
            continue
        if kickoff.tzinfo is None:
            kickoff = kickoff.replace(tzinfo=UTC)
        home_venue = by_team.get(_normalized(home))
        raw_venue = row.get("venue")
        if isinstance(raw_venue, dict):
            venue_name = str(raw_venue.get("name") or raw_venue.get("fullName") or "").strip()
            venue_city = str(raw_venue.get("city") or "").strip() or None
        else:
            venue_name = str(raw_venue or "").strip()
            venue_city = None
        neutral_site = bool(row.get("neutralSite"))
        named_venue = by_name.get(_normalized(venue_name)) if venue_name else None
        venue = named_venue if named_venue is not None else (None if neutral_site else home_venue)
        state = row.get("status")
        status = GameStatus.COMPLETED if row.get("completed") else GameStatus.SCHEDULED
        if isinstance(state, str) and "suspend" in state.lower():
            status = GameStatus.WEATHER_DELAY if "weather" in state.lower() else GameStatus.UNKNOWN
        games.append(
            Game(
                game_id=f"ncaa_{year}_cfbd_{row.get('id', kickoff.strftime('%Y%m%d%H%M'))}",
                league=League.NCAA,
                season=year,
                home_team=home,
                away_team=away,
                venue_id=venue.venue_id if venue else None,
                venue_name=venue.name if venue else venue_name or None,
                venue_city=venue_city,
                neutral_site=neutral_site,
                kickoff_utc=kickoff.astimezone(UTC),
                status=status,
                status_source="CollegeFootballData.com schedule; unofficial status",
            )
        )
    return games


def _optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(float(value))
    except ValueError:
        return None
