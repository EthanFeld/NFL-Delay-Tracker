"""Validated canonical data contracts used by the pipeline and website."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class League(StrEnum):
    NFL = "NFL"
    NCAA = "NCAA"


class GameStatus(StrEnum):
    SCHEDULED = "scheduled"
    PREGAME = "pregame"
    IN_PROGRESS = "in_progress"
    WEATHER_DELAY = "weather_delay"
    COMPLETED = "completed"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class RoofType(StrEnum):
    OUTDOOR = "outdoor"
    FIXED_DOME = "fixed_dome"
    RETRACTABLE = "retractable"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


class PolicyVerification(StrEnum):
    VERIFIED_PUBLIC = "verified_public"
    LEAGUE_GUIDANCE = "league_guidance"
    VENUE_ASSUMPTION = "venue_assumption"
    UNKNOWN = "unknown"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Game(StrictModel):
    game_id: str = Field(min_length=1)
    league: League
    season: int = Field(ge=1900, le=2200)
    home_team: str = Field(min_length=1)
    away_team: str = Field(min_length=1)
    venue_id: str | None = None
    venue_name: str | None = None
    venue_city: str | None = None
    neutral_site: bool = False
    kickoff_utc: datetime
    status: GameStatus = GameStatus.SCHEDULED
    status_source: str = "unknown"
    official_delay_active: bool = False
    model_delay_active: bool = False
    delay_started_at: datetime | None = None
    official_resume_at: datetime | None = None
    delay_source_note: str | None = None
    home_score: int | None = Field(default=None, ge=0)
    away_score: int | None = Field(default=None, ge=0)
    period: int | None = Field(default=None, ge=0)
    clock: str | None = None

    @field_validator("kickoff_utc", "delay_started_at", "official_resume_at")
    @classmethod
    def datetime_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("datetime must include a UTC offset")
        return value


class ManualGameOverride(StrictModel):
    game_id: str = Field(min_length=1)
    official_delay_active: bool
    delay_started_at: datetime | None = None
    official_resume_at: datetime | None = None
    source_note: str = Field(min_length=1, max_length=500)

    @field_validator("delay_started_at", "official_resume_at")
    @classmethod
    def override_times_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("manual override times must include a UTC offset")
        return value

    @model_validator(mode="after")
    def active_delay_needs_a_start(self) -> ManualGameOverride:
        if self.official_delay_active and self.delay_started_at is None:
            raise ValueError("an active manual delay override requires delay_started_at")
        return self


class Venue(StrictModel):
    venue_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    timezone: str
    roof_type: RoofType
    roof_weather_protection: bool
    policy_id: str
    league: League | None = None
    leagues: list[League] = Field(default_factory=list)
    home_teams: list[str] = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)
    source_url: str | None = None
    coordinate_source_url: str | None = None
    roof_source_url: str | None = None
    source_note: str | None = None
    source_checked_at: date | None = None
    city: str | None = None
    state: str | None = None
    fbs_source_url: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_leagues(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = dict(value)
        raw_leagues = data.get("leagues", data.get("league"))
        if isinstance(raw_leagues, str):
            raw_leagues = [raw_leagues]
        if isinstance(raw_leagues, list):
            normalized = [
                "NCAA" if str(item).strip().upper() in {"NCAA FBS", "FBS", "COLLEGE"} else str(item)
                for item in raw_leagues
            ]
            data["leagues"] = normalized
            if normalized:
                data["league"] = normalized[0]
        return data

    @field_validator("timezone")
    @classmethod
    def timezone_must_exist(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {value}") from exc
        return value


class RestartOverhead(StrictModel):
    type: str = "distribution"
    minutes: list[int] = Field(min_length=1)
    weights: list[float] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_distribution(self) -> RestartOverhead:
        if len(self.minutes) != len(self.weights):
            raise ValueError("restart minutes and weights must have equal lengths")
        if any(value < 0 for value in self.minutes):
            raise ValueError("restart overhead cannot be negative")
        if any(value < 0 for value in self.weights) or sum(self.weights) <= 0:
            raise ValueError("restart weights must be nonnegative and have positive sum")
        return self


class WeatherPolicy(StrictModel):
    policy_id: str
    trigger_radius_miles: float = Field(ge=0)
    monitor_radii_miles: list[float]
    quiet_period_minutes: int = Field(ge=0)
    warmup_exposure_minutes: int = Field(ge=0)
    clearance_mode: str
    restart_overhead: RestartOverhead
    verification: PolicyVerification
    source_checked_at: date | None = None
    source_url: str | None = None
    notes: str

    @field_validator("monitor_radii_miles")
    @classmethod
    def monitor_radii_nonnegative(cls, values: list[float]) -> list[float]:
        if any(value < 0 for value in values):
            raise ValueError("monitor radii cannot be negative")
        return values


class HazardPoint(StrictModel):
    offset_minutes: int
    probability: float = Field(ge=0, le=1)
    source: str
    valid_at: datetime | None = None

    @field_validator("valid_at")
    @classmethod
    def valid_time_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("valid_at must include a UTC offset")
        return value


class WeatherSnapshot(StrictModel):
    venue_id: str
    fetched_at: datetime
    source: str
    hazards: list[HazardPoint]
    venue_features: dict[str, Any] = Field(default_factory=dict)
    missing_sources: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @field_validator("fetched_at")
    @classmethod
    def fetched_time_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fetched_at must include a UTC offset")
        return value


class HoldInterval(StrictModel):
    started_at: datetime
    weather_clear_at: datetime
    football_resume_at: datetime
    phase: str
    reset_count: int = Field(ge=0)


class ForecastQuality(StrictModel):
    weather_data_age_seconds: int | None = Field(default=None, ge=0)
    sports_data_age_seconds: int | None = Field(default=None, ge=0)
    policy_verification: PolicyVerification
    forecast_lead_minutes: int | None = None
    missing_sources: list[str] = Field(default_factory=list)
    degraded: bool = False
    status: str = "experimental"


class DelayForecast(StrictModel):
    active: bool = False
    officially_confirmed: bool = False
    model_delay_active: bool = False
    last_qualifying_event_at: datetime | None = None
    earliest_weather_clear_at: datetime | None = None
    weather_clear_cdf: list[dict[str, Any]] = Field(default_factory=list)
    resume_cdf: list[dict[str, Any]] = Field(default_factory=list)
    resume_p50: datetime | None = None
    resume_p75: datetime | None = None
    resume_p90: datetime | None = None
    probability_additional_minutes: dict[int, float] = Field(default_factory=dict)
    holds: list[HoldInterval] = Field(default_factory=list)
    source: str = "policy simulation"
    notes: list[str] = Field(default_factory=list)


class PregameForecast(StrictModel):
    delay_probability: float = Field(ge=0, le=1)
    kickoff_delay_probability: float = Field(ge=0, le=1)
    in_game_delay_probability: float = Field(ge=0, le=1)
    multiple_delay_probability: float = Field(ge=0, le=1)
    expected_delay_minutes: float = Field(ge=0)
    hourly_delay_hazard: list[HazardPoint]
    simulation_count: int = Field(ge=0)


class GameForecast(StrictModel):
    generated_at: datetime
    model_version: str
    game: Game
    venue: Venue | None = None
    policy: WeatherPolicy | None = None
    weather: WeatherSnapshot | None = None
    pregame: PregameForecast
    delay: DelayForecast = Field(default_factory=DelayForecast)
    quality: ForecastQuality


class DataManifest(StrictModel):
    version: str
    generated_at: datetime
    sources: dict[str, dict[str, Any]]
    game_index_url: str = "games/index.json"
