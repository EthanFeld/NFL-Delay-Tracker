"""Schedule ingestion, forecast refresh and static JSON publication."""

from __future__ import annotations

import gzip
import json
import math
import os
import shutil
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError
from pydantic_core import to_jsonable_python

from nfl_delay_tracker.geo import distance_miles
from nfl_delay_tracker.model.hazard import blend_hazards, probability_per_bin
from nfl_delay_tracker.model.simulator import (
    DEFAULT_GAME_DURATION_MINUTES,
    historical_delay_duration_prior,
    load_historical_delay_durations,
    simulate_active_delay,
    simulate_pregame,
    simulate_remaining_game,
)
from nfl_delay_tracker.model.storm_motion import (
    StormCellCentroid,
    StormMotionStatus,
    estimate_storm_motion,
)
from nfl_delay_tracker.models import (
    DataManifest,
    DelayForecast,
    ForecastQuality,
    Game,
    GameForecast,
    GameStatus,
    HazardPoint,
    ManualGameOverride,
    PolicyVerification,
    PregameForecast,
    RoofType,
    Venue,
    WeatherPolicy,
    WeatherSnapshot,
)
from nfl_delay_tracker.providers.alerts import NwsSevereThunderstormWarningProvider
from nfl_delay_tracker.providers.espn import _normalized, fetch_scoreboards
from nfl_delay_tracker.providers.glm import GoesGlmProvider
from nfl_delay_tracker.providers.href import HrefCtProvider
from nfl_delay_tracker.providers.hrrr import HrrrPointProvider
from nfl_delay_tracker.providers.http import ProviderError
from nfl_delay_tracker.providers.mrms import MrmsSnapshot
from nfl_delay_tracker.providers.nws import NwsGridProvider
from nfl_delay_tracker.providers.open_meteo import OpenMeteoEnsembleProvider
from nfl_delay_tracker.providers.sports import NflverseScheduleProvider, fetch_cfbd_games

ROOT = Path(__file__).resolve().parents[2]
MODEL_VERSION = "engineering-baseline-0.2.3"
_TERMINAL_GAME_STATUSES = {
    GameStatus.COMPLETED,
    GameStatus.POSTPONED,
    GameStatus.CANCELLED,
}
_GLOBAL_OUTLOOK_HORIZON_HOURS = 14 * 24
_GLOBAL_OUTLOOK_MISSING_SOURCES = [
    "Thunder-capable thunderstorm probability forecast",
    "Venue-area lightning observations",
    "Observed radar storm-motion track",
    "Verified venue lightning policy",
]


def _clock_remaining_seconds(clock: str | None) -> int | None:
    """Parse a scoreboard clock such as ``12:34`` into seconds."""
    if not clock:
        return None
    parts = clock.strip().split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    minutes, seconds = (int(part) for part in parts)
    if seconds >= 60:
        return None
    return minutes * 60 + seconds


def _remaining_game_exposure_minutes(game: Game, now: datetime) -> int:
    """Estimate live forecast horizon from scoreboard clock, in model minutes.

    The model's 210-minute full-game exposure is spread over the 60-minute
    regulation clock. If scoreboard clock data is missing, elapsed wall time
    reduces the horizon while a game is in progress. For a weather hold, the
    game clock is paused, so missing clock data uses the full current period as
    a conservative bound (or the full-game bound if the period is unknown). A
    tied NFL game in the final minute gets one 10-minute overtime allowance;
    college overtime uses a short capped horizon because its possession
    periods have no continuous clock.
    """
    elapsed = max(0.0, (now - game.kickoff_utc).total_seconds() / 60)
    fallback_minutes = max(
        0, math.ceil(DEFAULT_GAME_DURATION_MINUTES - elapsed)
    )
    period = game.period
    seconds_left = _clock_remaining_seconds(game.clock)
    if period is None:
        return (
            DEFAULT_GAME_DURATION_MINUTES
            if game.status == GameStatus.WEATHER_DELAY
            else fallback_minutes
        )

    minutes_per_game_clock_minute = DEFAULT_GAME_DURATION_MINUTES / 60
    if seconds_left is None:
        if 1 <= period <= 4:
            # During a paused weather hold, missing clock data cannot tell us
            # how much of the current quarter remains. Use the full quarter.
            regulation_quarters_left = 5 - period
            if game.status == GameStatus.WEATHER_DELAY:
                regulation_quarters_left += 1
            remaining = math.ceil(
                regulation_quarters_left * 15 * minutes_per_game_clock_minute
            )
            if (
                game.league.value == "NFL"
                and period == 4
                and game.home_score is not None
                and game.away_score is not None
                and game.home_score == game.away_score
            ):
                remaining += math.ceil(10 * minutes_per_game_clock_minute)
            return min(DEFAULT_GAME_DURATION_MINUTES, remaining)
        if period >= 5:
            return 35 if game.league.value == "NFL" else 30
        return (
            DEFAULT_GAME_DURATION_MINUTES
            if game.status == GameStatus.WEATHER_DELAY
            else fallback_minutes
        )
    if 1 <= period <= 4:
        regulation_seconds_left = (4 - period) * 15 * 60 + seconds_left
        remaining = math.ceil(
            regulation_seconds_left / 60 * minutes_per_game_clock_minute
        )
        tied_late = (
            game.league.value == "NFL"
            and period == 4
            and seconds_left <= 60
            and game.home_score is not None
            and game.away_score is not None
            and game.home_score == game.away_score
        )
        if tied_late:
            remaining += math.ceil(10 * minutes_per_game_clock_minute)
        return min(DEFAULT_GAME_DURATION_MINUTES, remaining)

    if period >= 5:
        if game.league.value == "NFL":
            overtime_seconds_left = min(10 * 60, seconds_left)
            return math.ceil(overtime_seconds_left / 60 * minutes_per_game_clock_minute)
        # College overtime periods have no reliable continuous game clock.
        return 30

    return fallback_minutes


def _team_identity(team: str, team_owner: dict[str, str]) -> str:
    normalized = _normalized(team)
    return team_owner.get(normalized, normalized)


def _same_schedule_game(first: Game, second: Game, team_owner: dict[str, str]) -> bool:
    return (
        first.league == second.league
        and _team_identity(first.home_team, team_owner)
        == _team_identity(second.home_team, team_owner)
        and _team_identity(first.away_team, team_owner)
        == _team_identity(second.away_team, team_owner)
        and abs((first.kickoff_utc - second.kickoff_utc).total_seconds()) <= 6 * 3600
    )


def _merge_duplicate_schedule_game(first: Game, second: Game) -> Game:
    """Keep resolved event venue details while retaining the best live status."""
    venue_game = max(
        (first, second),
        key=lambda game: (
            game.venue_id is not None,
            game.venue_name is not None,
            game.venue_city is not None,
            game.neutral_site,
        ),
    )
    status_rank = {
        GameStatus.UNKNOWN: 0,
        GameStatus.SCHEDULED: 1,
        GameStatus.PREGAME: 1,
        GameStatus.IN_PROGRESS: 2,
        GameStatus.WEATHER_DELAY: 3,
        GameStatus.COMPLETED: 4,
        GameStatus.POSTPONED: 4,
        GameStatus.CANCELLED: 4,
    }
    status_game = max((first, second), key=lambda game: status_rank[game.status])
    if venue_game is status_game:
        return venue_game
    return venue_game.model_copy(
        update={
            "status": status_game.status,
            "status_source": status_game.status_source,
            "official_delay_active": status_game.official_delay_active,
            "model_delay_active": status_game.model_delay_active,
            "home_score": status_game.home_score,
            "away_score": status_game.away_score,
            "period": status_game.period,
            "clock": status_game.clock,
        }
    )


def _dedupe_schedule_games(games: list[Game], team_owner: dict[str, str]) -> list[Game]:
    deduped: list[Game] = []
    for game in games:
        match_index = next(
            (
                index
                for index, existing in enumerate(deduped)
                if _same_schedule_game(existing, game, team_owner)
            ),
            None,
        )
        if match_index is None:
            deduped.append(game)
        else:
            deduped[match_index] = _merge_duplicate_schedule_game(deduped[match_index], game)
    return deduped


def _records(document: Any, root_key: str, id_key: str) -> list[dict[str, Any]]:
    if isinstance(document, list):
        return document
    if not isinstance(document, dict):
        raise ValueError(f"expected YAML mapping or list for {root_key}")
    items = document.get(root_key, document)
    if isinstance(items, list):
        return items
    if isinstance(items, dict):
        result = []
        for identifier, item in items.items():
            if not isinstance(item, dict):
                continue
            record = dict(item)
            record.setdefault(id_key, str(identifier))
            result.append(record)
        return result
    raise ValueError(f"{root_key} must contain records")


def load_registry(root: Path = ROOT) -> tuple[list[Venue], dict[str, WeatherPolicy]]:
    venues_document = yaml.safe_load((root / "config" / "venues.yaml").read_text(encoding="utf-8"))
    policies_document = yaml.safe_load(
        (root / "config" / "policies.yaml").read_text(encoding="utf-8")
    )
    venues = [
        Venue.model_validate(item) for item in _records(venues_document, "venues", "venue_id")
    ]
    policies = [
        WeatherPolicy.model_validate(item)
        for item in _records(policies_document, "policies", "policy_id")
    ]
    policy_by_id = {policy.policy_id: policy for policy in policies}
    if len({venue.venue_id for venue in venues}) != len(venues):
        raise ValueError("duplicate venue_id in config/venues.yaml")
    if len(policy_by_id) != len(policies):
        raise ValueError("duplicate policy_id in config/policies.yaml")
    missing = sorted({venue.policy_id for venue in venues} - set(policy_by_id))
    if missing:
        raise ValueError(f"venues reference missing policies: {', '.join(missing)}")
    return venues, policy_by_id


def _dump_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = to_jsonable_python(data)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(serialized, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _write_game_status(root: Path, game_id: str, game: Any) -> None:
    """Update schedule status without discarding a published forecast snapshot."""
    path = root / "data" / "games" / f"{game_id}.json"
    previous = _load_json(path, {})
    payload: dict[str, Any] = dict(previous) if isinstance(previous, dict) else {}
    payload["game"] = game
    _dump_json(path, payload)


def _load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _manual_overrides_from_env() -> dict[str, ManualGameOverride]:
    raw = os.environ.get("GAME_OVERRIDES_JSON", "").strip()
    if not raw:
        return {}
    document = json.loads(raw)
    if isinstance(document, dict):
        records = []
        for game_id, value in document.items():
            if not isinstance(value, dict):
                raise ValueError(f"manual override for {game_id} must be an object")
            record = dict(value)
            record.setdefault("game_id", game_id)
            records.append(record)
    elif isinstance(document, list):
        records = document
    else:
        raise ValueError("GAME_OVERRIDES_JSON must be a JSON object or list")

    overrides: dict[str, ManualGameOverride] = {}
    for record in records:
        override = ManualGameOverride.model_validate(record)
        if override.game_id in overrides:
            raise ValueError(f"duplicate manual override for {override.game_id}")
        overrides[override.game_id] = override
    return overrides


def _apply_manual_override(game: Game, override: ManualGameOverride | None) -> Game:
    if override is None or game.status in _TERMINAL_GAME_STATUSES:
        return game
    return game.model_copy(
        update={
            "status": GameStatus.WEATHER_DELAY
            if override.official_delay_active
            else GameStatus.IN_PROGRESS,
            "status_source": "manual_verified_override",
            "official_delay_active": override.official_delay_active,
            "model_delay_active": False,
            "delay_started_at": override.delay_started_at,
            "official_resume_at": override.official_resume_at,
            "delay_source_note": override.source_note,
        }
    )


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return (
            value.astimezone(UTC)
            if value.tzinfo is not None and value.utcoffset() is not None
            else None
        )
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo and parsed.utcoffset() is not None else None


def _blend_weather_hazards(
    nws_hazards: list[HazardPoint],
    href_hazards: list[HazardPoint],
    mrms_features: dict[str, Any] | None,
    *,
    now: datetime,
    kickoff: datetime,
    warmup_minutes: int,
    nowcast_weight_at_zero: float = 0.9,
    nowcast_weight_at_60: float = 0.2,
    href_weight: float = 1.0,
    nws_weight: float = 0.0,
) -> list[HazardPoint]:
    """Blend MRMS nowcasts with HREF calibrated thunder and NWS forecast bins."""
    nws_by_offset = {point.offset_minutes: point.probability for point in nws_hazards}
    href_by_offset = {point.offset_minutes: point.probability for point in href_hazards}
    forecast_offsets = set(nws_by_offset) | set(href_by_offset)
    current_offset = int((now - kickoff).total_seconds() // 300) * 5
    first = min(forecast_offsets, default=current_offset - warmup_minutes)
    last = max(forecast_offsets, default=current_offset + 60)
    if mrms_features:
        first = min(first, current_offset)
        last = max(last, current_offset + 60)
    p30 = mrms_features.get("probability_next_30min") if mrms_features else None
    p60 = mrms_features.get("probability_next_60min") if mrms_features else None
    second_half = None
    if p60 is not None and p30 is not None:
        second_half = max(0.0, (float(p60) - float(p30)) / max(1e-9, 1 - float(p30)))
    elif p60 is not None:
        second_half = float(p60)

    if not forecast_offsets and p30 is None and second_half is None:
        return []
    points = []
    for offset in range(first, last + 1, 5):
        nws_probability = nws_by_offset.get(offset, 0.0)
        href_probability = href_by_offset.get(offset)
        if href_probability is None:
            forecast_probability = nws_probability
        elif offset not in nws_by_offset:
            forecast_probability = href_probability
        elif href_weight + nws_weight > 0:
            forecast_probability = (
                href_probability * href_weight + nws_probability * nws_weight
            ) / (href_weight + nws_weight)
        else:
            forecast_probability = href_probability
        valid_at = kickoff + timedelta(minutes=offset)
        lead_minutes = (valid_at - now).total_seconds() / 60
        nowcast_probability: float | None = None
        if 0 <= lead_minutes < 30 and p30 is not None:
            nowcast_probability = probability_per_bin(float(p30), 30)
        elif 30 <= lead_minutes <= 60 and second_half is not None:
            nowcast_probability = probability_per_bin(second_half, 30)
        if nowcast_probability is not None:
            probability = blend_hazards(
                nowcast_probability,
                forecast_probability,
                int(lead_minutes),
                nowcast_weight_at_zero=nowcast_weight_at_zero,
                nowcast_weight_at_60=nowcast_weight_at_60,
            )
        else:
            probability = forecast_probability
        point_sources = []
        if nowcast_probability is not None:
            point_sources.append("MRMS nowcast")
        if href_probability is not None:
            point_sources.append("SPC HREF CT")
        if offset in nws_by_offset and (href_probability is None or nws_weight > 0):
            point_sources.append("NWS regional thunder proxy")
        points.append(
            HazardPoint(
                offset_minutes=offset,
                probability=min(1.0, max(0.0, probability)),
                source=" + ".join(point_sources) or "weather forecast input",
                valid_at=valid_at,
            )
        )
    return points


def _hazard_window_state(
    hazards: list[HazardPoint], *, start_offset: int, end_offset: int
) -> str:
    """Classify a five-minute game window as storm, clear, or incompletely covered."""
    if end_offset <= start_offset:
        return "clear"
    by_offset: dict[int, HazardPoint] = {}
    for point in hazards:
        if start_offset <= point.offset_minutes < end_offset:
            previous = by_offset.get(point.offset_minutes)
            if previous is None or point.probability > previous.probability:
                by_offset[point.offset_minutes] = point
    missing = False
    for offset in range(start_offset, end_offset, 5):
        bin_point = by_offset.get(offset)
        if bin_point is None:
            missing = True
        elif bin_point.probability > 0:
            return "storm"
    return "incomplete" if missing else "clear"


def _is_numeric_zero(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, int | float)
        and math.isfinite(float(value))
        and float(value) == 0.0
    )


def _forecast_confirms_clear_window(
    *,
    start_offset: int,
    end_offset: int,
    forecast_hazards: list[HazardPoint],
    nws_hazards: list[HazardPoint],
    nws_fresh: bool,
    href_hazards: list[HazardPoint],
    href_fresh: bool,
    window_is_current: bool,
    mrms_features: dict[str, Any] | None,
    alert_features: dict[str, Any],
    storm_motion: dict[str, Any],
    delay_active: bool,
) -> bool:
    """Require complete zero thunder input before publishing a hard zero risk."""
    if end_offset <= start_offset:
        return True
    exposure_state = _hazard_window_state(
        forecast_hazards, start_offset=start_offset, end_offset=end_offset
    )
    if exposure_state != "clear":
        return False

    source_states = []
    if nws_fresh:
        source_states.append(
            _hazard_window_state(nws_hazards, start_offset=start_offset, end_offset=end_offset)
        )
    if href_fresh:
        source_states.append(
            _hazard_window_state(href_hazards, start_offset=start_offset, end_offset=end_offset)
        )
    if "storm" in source_states or "clear" not in source_states:
        return False

    if delay_active:
        return False
    tracks = storm_motion.get("tracks", [])
    if isinstance(tracks, list) and any(
        isinstance(track, dict)
        and str(track.get("status", "")).lower() == StormMotionStatus.APPROACHING.value
        for track in tracks
    ):
        return False

    if not window_is_current:
        return True
    if alert_features.get("status") != "ok" or alert_features.get("has_active_warning"):
        return False
    if not isinstance(mrms_features, dict):
        return False
    coverage = mrms_features.get("coverage")
    if not isinstance(coverage, dict) or not all(
        coverage.get(name)
        for name in ("probability_next_30min", "probability_next_60min", "cg_density_1min")
    ):
        return False
    if any(
        not _is_numeric_zero(mrms_features.get(name))
        for name in ("probability_next_30min", "probability_next_60min")
    ):
        return False
    density = mrms_features.get("cg_density_per_km2_min")
    if not isinstance(density, dict):
        return False
    if not _is_numeric_zero(density.get("fraction_positive")) or not _is_numeric_zero(
        density.get("max")
    ):
        return False
    return True


def _href_rows_to_hazards(rows: list[dict[str, Any]], kickoff: datetime) -> list[HazardPoint]:
    by_offset: dict[int, HazardPoint] = {}
    for row in rows:
        valid_start = _timestamp(row.get("valid_start"))
        valid_end = _timestamp(row.get("valid_end"))
        try:
            probability = float(row["probability"])
        except (KeyError, TypeError, ValueError):
            continue
        if valid_start is None or valid_end is None or valid_end <= valid_start:
            continue
        duration_minutes = max(1, int((valid_end - valid_start).total_seconds() // 60))
        probability_per_five = probability_per_bin(probability, duration_minutes, 5)
        first_offset = math.floor((valid_start - kickoff).total_seconds() / 300) * 5
        end_offset = math.ceil((valid_end - kickoff).total_seconds() / 300) * 5
        for offset in range(first_offset, end_offset, 5):
            point = HazardPoint(
                offset_minutes=offset,
                probability=probability_per_five,
                source="SPC HREF CT calibrated one-hour thunder probability",
                valid_at=kickoff + timedelta(minutes=offset),
            )
            previous = by_offset.get(offset)
            if previous is None or point.probability > previous.probability:
                by_offset[offset] = point
    return [by_offset[offset] for offset in sorted(by_offset)]


def _href_targets_for_game(
    *, kickoff: datetime, warmup_minutes: int, now: datetime
) -> list[datetime]:
    first = max(kickoff - timedelta(minutes=warmup_minutes), now)
    cursor = first.replace(minute=0, second=0, microsecond=0)
    horizon_end = min(kickoff + timedelta(hours=10), now + timedelta(hours=48))
    targets = []
    while cursor < horizon_end:
        targets.append(cursor + timedelta(minutes=30))
        cursor += timedelta(hours=1)
    return targets


def _href_sample_row(sample: Any) -> dict[str, Any]:
    row = asdict(sample)
    for key in ("issued_at", "valid_start", "valid_end"):
        row[key] = row[key].isoformat()
    return row


def _prefetch_href_rows(
    provider: HrefCtProvider,
    raw_games: list[dict[str, Any]],
    *,
    venue_by_id: dict[str, Venue],
    policy_by_id: dict[str, WeatherPolicy],
    now: datetime,
    limit: datetime,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[str]]]:
    """Decode each hourly HREF field once, then sample all games' venues."""

    venues_by_target: dict[datetime, dict[str, Venue]] = {}
    games_by_target_venue: dict[datetime, dict[str, list[str]]] = {}
    game_rows: dict[str, list[dict[str, Any]]] = {}
    game_errors: dict[str, list[str]] = {}
    for summary in raw_games:
        game_id_value = summary.get("game_id", summary.get("id"))
        venue_id = summary.get("venue_id")
        venue = venue_by_id.get(str(venue_id)) if venue_id else None
        policy = policy_by_id.get(venue.policy_id) if venue else None
        kickoff = _timestamp(summary.get("kickoff_utc", summary.get("date")))
        status = str(summary.get("status", "unknown")).lower()
        if (
            not game_id_value
            or not venue
            or not policy
            or kickoff is None
            or kickoff > limit
            or status in {"completed", "cancelled", "postponed"}
            or (venue.roof_type == RoofType.FIXED_DOME and venue.roof_weather_protection)
        ):
            continue
        game_id = str(game_id_value)
        game_rows.setdefault(game_id, [])
        game_errors.setdefault(game_id, [])
        for target in _href_targets_for_game(
            kickoff=kickoff,
            warmup_minutes=policy.warmup_exposure_minutes,
            now=now,
        ):
            venues_by_target.setdefault(target, {})[venue.venue_id] = venue
            games_by_target_venue.setdefault(target, {}).setdefault(venue.venue_id, []).append(
                game_id
            )

    for target, target_venues in sorted(venues_by_target.items()):
        points = {
            venue_id: (venue.latitude, venue.longitude) for venue_id, venue in target_venues.items()
        }
        try:
            samples, point_errors = provider.sample_points(points, at=target)
        except Exception as exc:
            for game_ids in games_by_target_venue[target].values():
                for game_id in game_ids:
                    game_errors[game_id].append(str(exc))
            continue
        for venue_id, game_ids in games_by_target_venue[target].items():
            sample = samples.get(venue_id)
            if sample is None:
                error = point_errors.get(venue_id, "HREF CT returned no point sample")
                for game_id in game_ids:
                    game_errors[game_id].append(error)
                continue
            if now - sample.issued_at > timedelta(hours=18):
                for game_id in game_ids:
                    game_errors[game_id].append("HREF CT model initialization is over 18 hours old")
                continue
            row = _href_sample_row(sample)
            for game_id in game_ids:
                game_rows[game_id].append(row)
    return game_rows, game_errors


def _cached_href_rows(game_record: dict[str, Any], *, now: datetime) -> list[dict[str, Any]]:
    weather = game_record.get("weather", {})
    features = weather.get("venue_features", {}) if isinstance(weather, dict) else {}
    rows = features.get("href_hourly", []) if isinstance(features, dict) else []
    if not isinstance(rows, list):
        return []
    valid_rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        issued_at = _timestamp(row.get("issued_at"))
        valid_end = _timestamp(row.get("valid_end"))
        if (
            issued_at is not None
            and valid_end is not None
            and timedelta(0) <= now - issued_at <= timedelta(hours=18)
            and valid_end > now
        ):
            valid_rows.append(row)
    return valid_rows


def _cached_context(game_record: dict[str, Any], feature_name: str) -> dict[str, Any] | None:
    weather = game_record.get("weather", {})
    features = weather.get("venue_features", {}) if isinstance(weather, dict) else {}
    feature = features.get(feature_name) if isinstance(features, dict) else None
    return feature if isinstance(feature, dict) else None


def _context_is_fresh(
    context: dict[str, Any], timestamp_field: str, now: datetime, maximum_age: timedelta
) -> bool:
    timestamp = _timestamp(context.get(timestamp_field))
    return timestamp is not None and timedelta(minutes=-2) <= now - timestamp <= maximum_age


def _storm_motion_features(
    previous: dict[str, Any] | None,
    storm_echoes: dict[str, Any] | None,
    venue: Venue,
    policy: WeatherPolicy,
    *,
    now: datetime,
) -> dict[str, Any]:
    """Match recent MRMS reflectivity objects and estimate venue-relative motion."""
    if not isinstance(storm_echoes, dict):
        return {"status": "unavailable", "tracks": []}
    observed_at = _timestamp(storm_echoes.get("valid_at"))
    if observed_at is None:
        return {"status": "unavailable", "tracks": []}
    age = now - observed_at
    if age < timedelta(minutes=-2) or age > timedelta(minutes=15):
        return {
            "status": "stale",
            "observed_at": observed_at.isoformat(),
            "observation_age_minutes": age.total_seconds() / 60,
            "tracks": [],
        }
    try:
        coverage_cells = int(storm_echoes.get("coverage_cells", 0))
    except (TypeError, ValueError):
        coverage_cells = 0
    if coverage_cells <= 0:
        return {
            "status": "unavailable",
            "observed_at": observed_at.isoformat(),
            "tracks": [],
        }
    echoes = [item for item in storm_echoes.get("objects", []) if isinstance(item, dict)]
    if not echoes:
        return {
            "status": "no_echoes",
            "observed_at": observed_at.isoformat(),
            "observation_age_minutes": age.total_seconds() / 60,
            "coverage_cells": coverage_cells,
            "tracks": [],
        }

    previous_at = _timestamp(previous.get("observed_at")) if isinstance(previous, dict) else None
    previous_objects = (
        [item for item in previous.get("objects", []) if isinstance(item, dict)]
        if isinstance(previous, dict)
        else []
    )
    previous_histories = previous.get("track_history", {}) if isinstance(previous, dict) else {}
    if not isinstance(previous_histories, dict):
        previous_histories = {}
    elapsed_minutes = (
        (observed_at - previous_at).total_seconds() / 60 if previous_at is not None else 0.0
    )
    can_match = 0.0 < elapsed_minutes <= 15.0 and previous_at is not None

    candidate_pairs: list[tuple[float, int, int]] = []
    if can_match:
        maximum_displacement = 2.0 + 100.0 * elapsed_minutes / 60.0
        for old_index, previous_object in enumerate(previous_objects):
            try:
                old_latitude = float(previous_object["latitude"])
                old_longitude = float(previous_object["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
            for new_index, current in enumerate(echoes):
                try:
                    distance = distance_miles(
                        old_latitude,
                        old_longitude,
                        float(current["latitude"]),
                        float(current["longitude"]),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                if distance <= maximum_displacement:
                    candidate_pairs.append((distance, old_index, new_index))

    old_matches: dict[int, list[int]] = {}
    new_matches: dict[int, list[int]] = {}
    for _, old_index, new_index in candidate_pairs:
        old_matches.setdefault(old_index, []).append(new_index)
        new_matches.setdefault(new_index, []).append(old_index)
    matched_current: dict[int, dict[str, Any]] = {}
    used_old: set[int] = set()
    for _distance, old_index, new_index in sorted(candidate_pairs):
        if (
            old_index in used_old
            or new_index in matched_current
            or len(old_matches.get(old_index, [])) != 1
            or len(new_matches.get(new_index, [])) != 1
        ):
            continue
        matched_current[new_index] = previous_objects[old_index]
        used_old.add(old_index)

    output_objects: list[dict[str, Any]] = []
    output_histories: dict[str, list[dict[str, Any]]] = {}
    track_rows: list[dict[str, Any]] = []
    for index, echo in enumerate(echoes):
        matched_object = matched_current.get(index)
        old_track_id = str(matched_object.get("track_id", "")) if matched_object else ""
        track_id = old_track_id or f"mrms:{observed_at.strftime('%Y%m%dT%H%M%S')}:{index}"
        current_observation = {
            "track_id": track_id,
            "observed_at": observed_at.isoformat(),
            "latitude": float(echo["latitude"]),
            "longitude": float(echo["longitude"]),
            "effective_radius_miles": float(echo.get("effective_radius_miles", 0.0)),
        }
        history = previous_histories.get(track_id, []) if matched_object else []
        if not isinstance(history, list):
            history = []
        valid_history = [
            item
            for item in history
            if isinstance(item, dict)
            and (item_at := _timestamp(item.get("observed_at"))) is not None
            and observed_at - timedelta(minutes=15) <= item_at < observed_at
        ]
        observations: list[StormCellCentroid] = []
        for item in [*valid_history[-3:], current_observation]:
            item_observed_at = _timestamp(item.get("observed_at"))
            if item_observed_at is None:
                continue
            try:
                observations.append(
                    StormCellCentroid(
                        track_id=str(item["track_id"]),
                        observed_at=item_observed_at,
                        latitude=float(item["latitude"]),
                        longitude=float(item["longitude"]),
                        effective_radius_miles=float(item.get("effective_radius_miles", 0.0)),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        estimate = estimate_storm_motion(observations, venue, policy, now=now)
        estimate_row = asdict(estimate)
        estimate_row["status"] = estimate.status.value
        if estimate.observed_at is not None:
            estimate_row["observed_at"] = estimate.observed_at.isoformat()
        estimate_row.update(
            {
                "max_reflectivity_dbz": float(echo.get("max_reflectivity_dbz", 0.0)),
                "cells": int(echo.get("cells", 0)),
                "latitude": current_observation["latitude"],
                "longitude": current_observation["longitude"],
                "track_id": track_id,
            }
        )
        track_rows.append(estimate_row)
        output_objects.append({**echo, "track_id": track_id})
        output_histories[track_id] = [*valid_history[-3:], current_observation]

    status_priority = {
        StormMotionStatus.INSIDE_POLICY.value: 0,
        StormMotionStatus.APPROACHING.value: 1,
        StormMotionStatus.UNCERTAIN.value: 2,
        StormMotionStatus.NO_HISTORY.value: 3,
        StormMotionStatus.MOVING_AWAY.value: 4,
        StormMotionStatus.STALE.value: 5,
        StormMotionStatus.AMBIGUOUS.value: 6,
    }
    track_rows.sort(
        key=lambda item: (
            status_priority.get(str(item.get("status")), 7),
            float(item.get("eta_minutes") or float("inf")),
            float(item.get("distance_to_policy_boundary_miles") or float("inf")),
        )
    )
    primary = track_rows[0]
    return {
        **primary,
        "observed_at": observed_at.isoformat(),
        "observation_age_minutes": age.total_seconds() / 60,
        "coverage_cells": coverage_cells,
        "source_url": storm_echoes.get("source_url"),
        "threshold_dbz": storm_echoes.get("threshold_dbz"),
        "tracks": track_rows,
        "objects": output_objects,
        "track_history": output_histories,
    }


def _motion_adjusted_hazards(
    hazards: list[HazardPoint],
    storm_motion: dict[str, Any],
    *,
    now: datetime,
) -> tuple[list[HazardPoint], dict[str, Any]]:
    """Add a capped, uncalibrated arrival pulse for fresh approaching radar echoes."""
    tracks = storm_motion.get("tracks", [])
    if not isinstance(tracks, list) or not hazards:
        return hazards, {"applied": False, "tracks_used": 0}

    def finite_number(value: Any) -> float | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if math.isfinite(number) else None

    motion_mass_by_lead: dict[int, float] = {}
    used_track_ids: set[str] = set()
    for track in tracks:
        if (
            not isinstance(track, dict)
            or track.get("status") != StormMotionStatus.APPROACHING.value
        ):
            continue
        observed_at = _timestamp(track.get("observed_at"))
        eta = finite_number(track.get("eta_minutes"))
        eta_lower = finite_number(track.get("eta_lower_minutes"))
        eta_upper = finite_number(track.get("eta_upper_minutes"))
        reflectivity = finite_number(track.get("max_reflectivity_dbz"))
        if (
            observed_at is None
            or now - observed_at < timedelta(minutes=-2)
            or now - observed_at > timedelta(minutes=15)
            or eta is None
            or not 30 <= eta <= 180
        ):
            continue
        lower = max(30, int(math.floor((eta_lower if eta_lower is not None else eta) / 5) * 5) - 10)
        upper = min(180, int(math.ceil((eta_upper if eta_upper is not None else eta) / 5) * 5) + 10)
        if upper < lower:
            continue
        duration_minutes = max(5, upper - lower + 5)
        echo_strength = min(1.0, max(0.0, ((reflectivity or 35.0) - 35.0) / 15.0))
        horizon_factor = max(0.35, 1.0 - (eta - 30.0) / 230.0)
        arrival_mass = min(0.15, (0.06 + 0.09 * echo_strength) * horizon_factor)
        per_bin = probability_per_bin(arrival_mass, duration_minutes, 5)
        for lead in range(lower, upper + 1, 5):
            motion_mass_by_lead[lead] = min(
                0.15,
                1 - (1 - motion_mass_by_lead.get(lead, 0.0)) * (1 - per_bin),
            )
        used_track_ids.add(str(track.get("track_id", "unknown")))

    if not motion_mass_by_lead:
        return hazards, {"applied": False, "tracks_used": 0}
    adjusted: list[HazardPoint] = []
    max_added_probability = 0.0
    for point in hazards:
        valid_at = point.valid_at
        lead = (
            int((valid_at - now).total_seconds() // 300) * 5
            if valid_at is not None
            else point.offset_minutes
        )
        motion_probability = motion_mass_by_lead.get(lead, 0.0)
        if not motion_probability:
            adjusted.append(point)
            continue
        probability = 1 - (1 - point.probability) * (1 - motion_probability)
        max_added_probability = max(max_added_probability, probability - point.probability)
        adjusted.append(
            point.model_copy(
                update={
                    "probability": min(1.0, probability),
                    "source": f"{point.source} + experimental MRMS storm-motion arrival",
                }
            )
        )
    return adjusted, {
        "applied": bool(used_track_ids),
        "tracks_used": len(used_track_ids),
        "track_ids": sorted(used_track_ids),
        "maximum_added_hazard_probability": max_added_probability,
        "horizon_minutes": [min(motion_mass_by_lead), max(motion_mass_by_lead)],
        "calibrated": False,
    }


def _alert_snapshot_features(snapshot: Any | None) -> dict[str, Any]:
    if snapshot is None:
        return {"status": "unavailable", "alerts": []}
    return {
        "status": "ok" if snapshot.is_fresh else "stale",
        "fetched_at": snapshot.fetched_at,
        "freshness_seconds": snapshot.freshness_seconds,
        "source_url": snapshot.source_url,
        "has_active_warning": snapshot.has_active_warning,
        "notes": list(snapshot.notes),
        "alerts": [asdict(alert) for alert in snapshot.alerts],
    }


def _compact_archived_features(features: Any) -> dict[str, Any]:
    if not isinstance(features, dict):
        return {}
    compact = {
        key: value for key, value in features.items() if key not in {"storm_echoes", "storm_motion"}
    }
    href_rows = features.get("href_hourly")
    if isinstance(href_rows, list):
        compact["href_hourly"] = [
            {
                key: row.get(key)
                for key in (
                    "probability",
                    "issued_at",
                    "valid_start",
                    "valid_end",
                    "forecast_hour",
                    "grid_latitude",
                    "grid_longitude",
                    "grid_distance_km",
                )
            }
            for row in href_rows
            if isinstance(row, dict)
        ]
    alerts = features.get("nws_alerts")
    if isinstance(alerts, dict):
        compact["nws_alerts"] = {
            key: alerts.get(key)
            for key in (
                "status",
                "fetched_at",
                "freshness_seconds",
                "has_active_warning",
                "alerts",
            )
        }
    hrrr = features.get("hrrr_point")
    if isinstance(hrrr, dict):
        compact["hrrr_point"] = {
            key: hrrr.get(key)
            for key in (
                "source_url",
                "field_source_urls",
                "issued_at",
                "valid_at",
                "forecast_hour",
                "reflectivity_dbz",
                "precipitation_rate_kg_m2_s",
                "cape_j_kg",
                "u_wind_10m_ms",
                "v_wind_10m_ms",
                "wind_speed_10m_ms",
                "grid_distance_km",
            )
        }
    glm = features.get("glm_observation")
    if isinstance(glm, dict):
        compact["glm_observation"] = {
            key: glm.get(key)
            for key in (
                "source_url",
                "satellite",
                "flash_count",
                "duration_seconds",
                "flash_rate_per_minute",
                "flash_density_per_km2_min",
                "valid_start",
                "valid_end",
            )
        }
    motion = features.get("storm_motion")
    if isinstance(motion, dict):
        motion_fields = (
            "status",
            "track_id",
            "observed_at",
            "observation_age_minutes",
            "bearing_degrees",
            "speed_mph",
            "radial_speed_toward_mph",
            "radial_speed_uncertainty_mph",
            "distance_to_policy_boundary_miles",
            "eta_minutes",
            "eta_lower_minutes",
            "eta_upper_minutes",
            "max_reflectivity_dbz",
            "cells",
            "model_adjustment",
        )
        track_fields = (
            "status",
            "track_id",
            "observed_at",
            "bearing_degrees",
            "speed_mph",
            "radial_speed_toward_mph",
            "radial_speed_uncertainty_mph",
            "distance_to_policy_boundary_miles",
            "eta_minutes",
            "eta_lower_minutes",
            "eta_upper_minutes",
            "max_reflectivity_dbz",
            "cells",
            "latitude",
            "longitude",
        )
        compact["storm_motion"] = {
            **{key: motion.get(key) for key in motion_fields if key in motion},
            "tracks": [
                {key: track.get(key) for key in track_fields if key in track}
                for track in motion.get("tracks", [])
                if isinstance(track, dict)
            ],
        }
    echoes = features.get("storm_echoes")
    if isinstance(echoes, dict):
        compact["storm_echoes"] = {
            key: echoes.get(key)
            for key in ("valid_at", "source_url", "threshold_dbz", "coverage_cells")
        }
    return compact


def _card_forecast_projection(forecast: dict[str, Any]) -> dict[str, Any]:
    """Return the small detail-shaped forecast subset used by scoreboard cards."""
    pregame = forecast.get("pregame")
    pregame_fields = (
        "delay_probability",
        "kickoff_delay_probability",
        "in_game_delay_probability",
        "expected_delay_minutes",
    )
    pregame_summary = (
        {key: pregame.get(key) for key in pregame_fields if key in pregame}
        if isinstance(pregame, dict)
        else None
    )
    delay = forecast.get("delay")
    delay_fields = (
        "active",
        "officially_confirmed",
        "model_delay_active",
        "last_qualifying_event_at",
        "earliest_weather_clear_at",
        "weather_clear_cdf",
        "resume_cdf",
        "resume_p50",
        "resume_p75",
        "resume_p90",
        "probability_additional_minutes",
        "source",
        "notes",
    )
    delay_summary = (
        {key: delay.get(key) for key in delay_fields if key in delay}
        if isinstance(delay, dict)
        else {}
    )
    weather = forecast.get("weather")
    weather_summary: dict[str, Any] | None = None
    if isinstance(weather, dict):
        features = weather.get("venue_features")
        alerts = features.get("nws_alerts") if isinstance(features, dict) else None
        if isinstance(alerts, dict):
            alert_fields = (
                "status",
                "fetched_at",
                "freshness_seconds",
                "has_active_warning",
                "alerts",
            )
            weather_summary = {
                "venue_features": {
                    "nws_alerts": {key: alerts.get(key) for key in alert_fields if key in alerts}
                }
            }
        global_outlook = (
            features.get("global_weather_outlook") if isinstance(features, dict) else None
        )
        if isinstance(global_outlook, dict):
            if weather_summary is None:
                weather_summary = {"venue_features": {}}
            weather_summary["venue_features"]["global_weather_outlook"] = global_outlook
        motion = features.get("storm_motion") if isinstance(features, dict) else None
        if isinstance(motion, dict):
            if weather_summary is None:
                weather_summary = {"venue_features": {}}
            weather_summary["venue_features"]["storm_motion"] = {
                key: motion.get(key)
                for key in (
                    "status",
                    "track_id",
                    "observed_at",
                    "observation_age_minutes",
                    "bearing_degrees",
                    "speed_mph",
                    "radial_speed_toward_mph",
                    "distance_to_policy_boundary_miles",
                    "eta_minutes",
                    "eta_lower_minutes",
                    "eta_upper_minutes",
                    "max_reflectivity_dbz",
                    "model_adjustment",
                    "tracks",
                )
                if key in motion
            }
    return {
        "generated_at": forecast.get("generated_at"),
        "model_version": forecast.get("model_version"),
        "venue": forecast.get("venue"),
        "policy": forecast.get("policy"),
        "pregame": pregame_summary,
        "delay": delay_summary,
        "quality": forecast.get("quality"),
        "weather": weather_summary,
    }


def _archive_issuances(
    root: Path,
    game_index: list[dict[str, Any]],
    source_health: dict[str, dict[str, Any]],
    *,
    generated_at: datetime,
) -> int:
    """Append compact forecast inputs/outputs so future runs can be calibrated."""
    archive_dir = root / "data" / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    archive_path = archive_dir / f"issued-forecasts-{generated_at.date().isoformat()}.jsonl"
    latest_path = archive_dir / f"issued-forecasts-{generated_at.date().isoformat()}.latest.json"
    latest_issued = _load_json(latest_path, {})
    if not isinstance(latest_issued, dict):
        latest_issued = {}

    for path in archive_dir.glob("issued-forecasts-*.jsonl"):
        if path != archive_path:
            date_part = path.name.removeprefix("issued-forecasts-").removesuffix(".jsonl")
            try:
                archived_date = date.fromisoformat(date_part)
            except ValueError:
                continue
            if archived_date < generated_at.date():
                compressed_path = path.with_suffix(path.suffix + ".gz")
                with (
                    path.open("rb") as source,
                    gzip.open(compressed_path, "wb", compresslevel=6) as target,
                ):
                    shutil.copyfileobj(source, target)
                path.unlink()

    if archive_path.exists() and not latest_issued:
        with archive_path.open(encoding="utf-8") as archive:
            for line in archive:
                try:
                    prior = json.loads(line)
                except json.JSONDecodeError:
                    continue
                prior_game_id = str((prior.get("game") or {}).get("game_id", ""))
                if prior_game_id and isinstance(prior.get("issued_at"), str):
                    latest_issued[prior_game_id] = prior["issued_at"]

    written = 0
    with archive_path.open("a", encoding="utf-8", newline="\n") as archive:
        for summary in game_index:
            game_id = str(summary.get("game_id", summary.get("id", "")))
            if not game_id:
                continue
            forecast = _load_json(root / "data" / "games" / f"{game_id}.json", {})
            if not isinstance(forecast, dict):
                continue
            pregame = forecast.get("pregame")
            delay = forecast.get("delay") or {}
            if not isinstance(pregame, dict) and not (
                isinstance(delay, dict) and delay.get("active")
            ):
                continue
            issued_at = _timestamp(forecast.get("generated_at")) or generated_at
            is_active_delay = bool(isinstance(delay, dict) and delay.get("active"))
            previous_issued_at = _timestamp(latest_issued.get(game_id))
            if (
                not is_active_delay
                and previous_issued_at is not None
                and issued_at - previous_issued_at < timedelta(hours=1)
            ):
                continue
            game = forecast.get("game") or summary
            weather = forecast.get("weather") or {}
            quality = forecast.get("quality") or {}
            hazard_curve = [
                [
                    point.get("offset_minutes"),
                    round(float(point.get("probability", 0)), 6),
                ]
                for point in weather.get("hazards", [])
                if isinstance(point, dict)
            ]
            pregame_summary = (
                {
                    key: pregame.get(key)
                    for key in (
                        "delay_probability",
                        "kickoff_delay_probability",
                        "in_game_delay_probability",
                        "multiple_delay_probability",
                        "expected_delay_minutes",
                        "simulation_count",
                    )
                }
                if isinstance(pregame, dict)
                else None
            )
            row = {
                "schema_version": 3,
                "issued_at": issued_at.isoformat(),
                "game": {
                    key: game.get(key)
                    for key in (
                        "game_id",
                        "league",
                        "season",
                        "home_team",
                        "away_team",
                        "kickoff_utc",
                        "venue_id",
                        "venue_name",
                        "venue_city",
                        "neutral_site",
                        "status",
                        "official_delay_active",
                        "model_delay_active",
                    )
                },
                "policy": {
                    key: (forecast.get("policy") or {}).get(key)
                    for key in (
                        "policy_id",
                        "trigger_radius_miles",
                        "quiet_period_minutes",
                        "clearance_mode",
                    )
                },
                "weather_source": weather.get("source"),
                "weather_fetched_at": weather.get("fetched_at"),
                "venue_features": _compact_archived_features(weather.get("venue_features", {})),
                "hazard_curve_5m": hazard_curve,
                "pregame": pregame_summary,
                "delay": delay,
                "quality": quality,
                "sources": source_health,
                "model_version": forecast.get("model_version"),
            }
            archive.write(json.dumps(row, separators=(",", ":"), ensure_ascii=False) + "\n")
            written += 1
            latest_issued[game_id] = issued_at.isoformat()
    _dump_json(latest_path, latest_issued)
    return written


def sync_schedules(
    *, root: Path = ROOT, start: date | None = None, days: int = 14
) -> dict[str, Any]:
    venues, _ = load_registry(root)
    start = start or (datetime.now(UTC).date() - timedelta(days=1))
    scoreboard_games, health = fetch_scoreboards(venues, start=start, days=days)
    team_owner = {
        _normalized(team): venue.venue_id
        for venue in venues
        for team in [*venue.home_teams, *venue.aliases]
    }

    def matchup_key(game: Game) -> tuple[str, str]:
        return (
            _team_identity(game.home_team, team_owner),
            _team_identity(game.away_team, team_owner),
        )

    def overlay_status(schedule_game: Game) -> Game:
        schedule_home, schedule_away = matchup_key(schedule_game)
        for live_game in scoreboard_games:
            if live_game.league != schedule_game.league:
                continue
            live_home, live_away = matchup_key(live_game)
            if (live_home, live_away) == (schedule_home, schedule_away) and abs(
                (live_game.kickoff_utc - schedule_game.kickoff_utc).total_seconds()
            ) <= 6 * 3600:
                return schedule_game.model_copy(
                    update={
                        "status": live_game.status,
                        "status_source": live_game.status_source,
                        "official_delay_active": live_game.official_delay_active,
                        "home_score": live_game.home_score,
                        "away_score": live_game.away_score,
                        "period": live_game.period,
                        "clock": live_game.clock,
                    }
                )
        return schedule_game

    try:
        nfl_schedule = NflverseScheduleProvider().fetch_games(venues, start=start, days=days)
        nfl_schedule = [overlay_status(game) for game in nfl_schedule]
        scoreboard_nfl = [game for game in scoreboard_games if game.league.value == "NFL"]
        nfl_schedule.extend(
            game
            for game in scoreboard_nfl
            if not any(
                _same_schedule_game(game, scheduled, team_owner) for scheduled in nfl_schedule
            )
        )
        games = nfl_schedule
        health["nfl_schedule"] = {
            "status": "ok",
            "updated_at": datetime.now(UTC).isoformat(),
            "message": "nflverse schedule metadata with ESPN public live status; "
            "unofficial sources",
            "url": NflverseScheduleProvider.url,
        }
    except Exception as exc:
        games = [game for game in scoreboard_games if game.league.value == "NFL"]
        health["nfl_schedule"] = {
            "status": "degraded",
            "updated_at": datetime.now(UTC).isoformat(),
            "message": f"Using ESPN public schedule fallback: {exc}",
        }

    scoreboard_college = [game for game in scoreboard_games if game.league.value == "NCAA"]
    api_key = os.environ.get("CFBD_API_KEY", "").strip()
    if api_key:
        try:
            year = datetime.now(UTC).year
            college_schedule = fetch_cfbd_games(api_key, venues, year=year)
            end_day = start + timedelta(days=days)
            college_schedule = [
                overlay_status(game)
                for game in college_schedule
                if start <= game.kickoff_utc.date() <= end_day
            ]
            college_schedule.extend(
                game
                for game in scoreboard_college
                if not any(
                    _same_schedule_game(game, scheduled, team_owner)
                    for scheduled in college_schedule
                )
            )
            health["college_schedule"] = {
                "status": "ok",
                "updated_at": datetime.now(UTC).isoformat(),
                "message": "CFBD authenticated schedule with ESPN public live status",
            }
        except Exception as exc:
            college_schedule = scoreboard_college
            health["college_schedule"] = {
                "status": "degraded",
                "updated_at": datetime.now(UTC).isoformat(),
                "message": f"Using ESPN public schedule fallback: {exc}",
            }
    else:
        college_schedule = scoreboard_college
        health["college_schedule"] = {
            **health.get("college_scoreboard", {}),
            "message": "ESPN public scoreboard fallback; set CFBD_API_KEY for "
            "authenticated schedule feed",
        }

    games.extend(college_schedule)
    # Venue can differ across providers for the same neutral-site matchup.
    deduped = _dedupe_schedule_games(games, team_owner)
    overrides = _manual_overrides_from_env()
    games = [_apply_manual_override(game, overrides.get(game.game_id)) for game in deduped]
    generated_at = datetime.now(UTC)
    index = {
        "generated_at": generated_at.isoformat(),
        "games": [
            {
                "id": game.game_id,
                "game_id": game.game_id,
                "league": game.league.value,
                "season": game.season,
                "date": game.kickoff_utc.isoformat(),
                "kickoff_utc": game.kickoff_utc.isoformat(),
                "status": game.status.value,
                "home_team": game.home_team,
                "away_team": game.away_team,
                "venue_id": game.venue_id,
                "venue_name": game.venue_name,
                "venue_city": game.venue_city,
                "neutral_site": game.neutral_site,
                "official_delay_active": game.official_delay_active,
                "model_delay_active": game.model_delay_active,
                "delay_started_at": game.delay_started_at,
                "official_resume_at": game.official_resume_at,
                "delay_source_note": game.delay_source_note,
                "status_source": game.status_source,
                "home_score": game.home_score,
                "away_score": game.away_score,
                "period": game.period,
                "clock": game.clock,
            }
            for game in games
        ],
    }
    _dump_json(root / "data" / "games" / "index.json", index)
    for game in games:
        _write_game_status(root, game.game_id, game)
    _dump_json(
        root / "data" / "manifest.json",
        {
            "version": generated_at.strftime("%Y%m%dT%H%M%SZ"),
            "generated_at": generated_at.isoformat(),
            "sources": health,
            "game_index_url": "games/index.json",
        },
    )
    return {"games": len(games), "sources": health, "generated_at": generated_at.isoformat()}


def refresh_live_status(*, root: Path = ROOT, days: int = 3) -> dict[str, Any]:
    """Refresh recent scoreboard status while retaining the full schedule window."""
    from nfl_delay_tracker.providers.espn import _normalized

    venues, _ = load_registry(root)
    start = datetime.now(UTC).date() - timedelta(days=1)
    live_games, health = fetch_scoreboards(venues, start=start, days=days)
    index_path = root / "data" / "games" / "index.json"
    index = _load_json(index_path, {"games": []})
    existing = index.get("games", []) if isinstance(index, dict) else []
    now = datetime.now(UTC)

    def key(row: dict[str, Any]) -> tuple[str, str, str, str]:
        kickoff = str(row.get("kickoff_utc", row.get("date", "")))[:10]
        return (
            str(row.get("league", "")),
            _normalized(str(row.get("home_team", ""))),
            _normalized(str(row.get("away_team", ""))),
            kickoff,
        )

    live_by_key = {key(game.model_dump(mode="json")): game for game in live_games}
    retained: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for row in existing:
        try:
            kickoff = datetime.fromisoformat(
                str(row.get("kickoff_utc", row.get("date"))).replace("Z", "+00:00")
            )
        except ValueError:
            continue
        if kickoff >= now - timedelta(days=1) and kickoff <= now + timedelta(days=14):
            live_game = live_by_key.get(key(row))
            if live_game:
                merged = live_game.model_dump(mode="json")
                merged["id"] = live_game.game_id
                merged["game_id"] = live_game.game_id
                merged["date"] = live_game.kickoff_utc.isoformat()
                retained[key(merged)] = merged
            else:
                retained[key(row)] = row
    for game in live_games:
        merged = game.model_dump(mode="json")
        merged.update({"id": game.game_id, "date": game.kickoff_utc.isoformat()})
        retained[key(merged)] = merged

    rows = sorted(
        retained.values(), key=lambda row: str(row.get("kickoff_utc", row.get("date", "")))
    )
    overrides = _manual_overrides_from_env()
    for row in rows:
        override = overrides.get(str(row.get("game_id", row.get("id", ""))))
        if override is None or row.get("status") in {
            status.value for status in _TERMINAL_GAME_STATUSES
        }:
            continue
        row.update(
            {
                "status": (
                    GameStatus.WEATHER_DELAY.value
                    if override.official_delay_active
                    else GameStatus.IN_PROGRESS.value
                ),
                "status_source": "manual_verified_override",
                "official_delay_active": override.official_delay_active,
                "model_delay_active": False,
                "delay_started_at": (
                    override.delay_started_at.isoformat() if override.delay_started_at else None
                ),
                "official_resume_at": (
                    override.official_resume_at.isoformat() if override.official_resume_at else None
                ),
                "delay_source_note": override.source_note,
            }
        )
    generated_at = datetime.now(UTC)
    _dump_json(index_path, {"generated_at": generated_at.isoformat(), "games": rows})
    for row in rows:
        game_id = row.get("game_id", row.get("id"))
        if game_id:
            _write_game_status(root, str(game_id), row)
    manifest = _load_json(root / "data" / "manifest.json", {})
    sources = manifest.get("sources", {}) if isinstance(manifest, dict) else {}
    sources.update(health)
    _dump_json(
        root / "data" / "manifest.json",
        {
            "version": generated_at.strftime("%Y%m%dT%H%M%SZ"),
            "generated_at": generated_at.isoformat(),
            "sources": sources,
            "game_index_url": "games/index.json",
        },
    )
    return {"games": len(rows), "live_window_games": len(live_games), "sources": health}


def _unavailable_forecast(
    game: Game,
    venue: Venue | None,
    policy: WeatherPolicy | None,
    *,
    generated_at: datetime,
    note: str,
    forecast_scope: str = "weather_unavailable",
) -> dict[str, Any]:
    missing_sources = {
        "archive_missing": ["Archived pregame forecast"],
        "forecast_pending": [
            "NWS regional thunder outlook is outside the seven-day window",
            "Global ECMWF IFS conditions outlook is unavailable for this kickoff",
        ],
        "venue_unresolved": ["Venue registry or weather policy"],
        "game_unavailable": ["Active game schedule"],
    }.get(
        forecast_scope,
        [
            "SPC HREF CT thunder probabilities",
            "NWS probability of thunder",
            "MRMS lightning observations",
        ],
    )
    return {
        "generated_at": generated_at.isoformat(),
        "model_version": MODEL_VERSION,
        "game": game.model_dump(mode="json"),
        "venue": venue.model_dump(mode="json") if venue else None,
        "policy": policy.model_dump(mode="json") if policy else None,
        "weather": None,
        "pregame": None,
        "delay": {
            "active": game.official_delay_active,
            "officially_confirmed": game.official_delay_active,
            "model_delay_active": False,
            "resume_cdf": [],
            "notes": [note],
        },
        "quality": {
            "weather_data_age_seconds": None,
            "sports_data_age_seconds": 0,
            "policy_verification": policy.verification.value if policy else "unknown",
            "forecast_scope": forecast_scope,
            "missing_sources": missing_sources,
            "degraded": True,
            "status": "unavailable",
            "message": note,
        },
    }


def _global_weather_outlook_forecast(
    game: Game,
    venue: Venue,
    policy: WeatherPolicy,
    *,
    generated_at: datetime,
    outlook: dict[str, Any],
    fetched_at: datetime,
) -> dict[str, Any]:
    """Build conditions-only outlook without inferring thunder or delay odds."""
    resolution_hours = outlook.get("native_resolution_hours", 3)
    source = (
        "Open-Meteo ECMWF IFS ensemble conditions only; "
        f"{resolution_hours}-hour native resolution; no thunder estimate"
    )
    forecast = _unavailable_forecast(
        game,
        venue,
        policy,
        generated_at=generated_at,
        note=(
            "Global ECMWF IFS ensemble provides weather conditions only. "
            "Thunderstorm and venue-delay probabilities, local lightning observations, "
            "and observed storm movement are unavailable."
        ),
        forecast_scope="global_weather_outlook",
    )
    snapshot = WeatherSnapshot(
        venue_id=venue.venue_id,
        fetched_at=fetched_at,
        source=source,
        hazards=[],
        venue_features={"global_weather_outlook": outlook},
        missing_sources=_GLOBAL_OUTLOOK_MISSING_SOURCES,
        notes=[
            "ECMWF IFS ensemble weather codes summarize broad conditions; they lack "
            "the atmospheric-stability detail required to estimate thunderstorms.",
            "No thunder/lightning delay probability or storm-motion estimate is produced "
            "from this global outlook.",
        ],
    )
    forecast["weather"] = snapshot.model_dump(mode="json")
    forecast["quality"].update(
        {
            "weather_data_age_seconds": max(
                0, int((generated_at - fetched_at).total_seconds())
            ),
            "forecast_scope": "global_weather_outlook",
            "missing_sources": _GLOBAL_OUTLOOK_MISSING_SOURCES,
            "status": "global conditions outlook only",
            "message": (
                "Open-Meteo ECMWF IFS ensemble provides global conditions at about 25 km "
                f"with {resolution_hours}-hour native resolution. Its weather-code field "
                "cannot estimate thunderstorms. Venue-specific delay odds, local lightning "
                "observations, and observed storm movement are unavailable; no numeric "
                "delay risk is published."
            ),
        }
    )
    forecast["delay"].setdefault("notes", []).append(
        "Global weather codes provide a conditions outlook only; they do not provide "
        "thunderstorm or delay probabilities."
    )
    return forecast


def _carry_forward_future_forecast(
    game_record: dict[str, Any], game: Game, *, now: datetime
) -> dict[str, Any] | None:
    """Keep a fresh forecast or conditions outlook within the published schedule."""
    pregame = game_record.get("pregame")
    quality = game_record.get("quality")
    weather = game_record.get("weather")
    features = weather.get("venue_features") if isinstance(weather, dict) else None
    global_outlook = (
        features.get("global_weather_outlook") if isinstance(features, dict) else None
    )
    forecast_scope = quality.get("forecast_scope") if isinstance(quality, dict) else None
    has_regional_outlook = forecast_scope == "regional_outlook" and isinstance(pregame, dict)
    has_global_outlook = forecast_scope == "global_weather_outlook" and isinstance(
        global_outlook, dict
    )
    generated_at = _timestamp(game_record.get("generated_at"))
    cached_game = game_record.get("game")
    cached_kickoff = (
        _timestamp(cached_game.get("kickoff_utc"))
        if isinstance(cached_game, dict)
        else None
    )
    if (
        not (has_regional_outlook or has_global_outlook)
        or generated_at is None
        or cached_kickoff is None
        or abs(cached_kickoff - game.kickoff_utc) > timedelta(minutes=90)
        or not timedelta(minutes=-5) <= now - generated_at <= timedelta(hours=24)
        or game.kickoff_utc > now + timedelta(hours=_GLOBAL_OUTLOOK_HORIZON_HOURS)
    ):
        return None
    if has_global_outlook and isinstance(global_outlook, dict):
        valid_at = _timestamp(global_outlook.get("valid_at"))
        expected_resolution_hours = (
            6
            if game.kickoff_utc - now > timedelta(hours=144)
            else 3
        )
        if (
            valid_at is None
            or abs(valid_at - game.kickoff_utc) > timedelta(minutes=90)
            or global_outlook.get("native_resolution_hours") != expected_resolution_hours
        ):
            return None

    carried = dict(game_record)
    carried["game"] = game.model_dump(mode="json")
    weather = carried.get("weather")
    if isinstance(weather, dict):
        weather = dict(weather)
        carried["weather"] = weather
        features = weather.get("venue_features")
        if isinstance(features, dict):
            features = dict(features)
            weather["venue_features"] = features
            alerts = features.get("nws_alerts")
            if isinstance(alerts, dict):
                features["nws_alerts"] = {**alerts, "status": "stale", "alerts": []}
    return carried


def refresh_forecasts(
    *,
    root: Path = ROOT,
    forecast_horizon_hours: int = 168,
    simulation_count: int = 20_000,
    refresh_href: bool = True,
    refresh_hrrr: bool = True,
) -> dict[str, Any]:
    model_config = yaml.safe_load((root / "config" / "model.yaml").read_text(encoding="utf-8"))
    # Keep production forecasts independent until serial dependence is fit on
    # a representative archive. Period probabilities are already calibrated
    # as source-window marginals; an unfit AR prior would shrink their union.
    model_rho = float(model_config.get("initial_latent_correlation", 0.0))
    blending_config = model_config.get("temporal_blending", {})
    venues, policy_by_id = load_registry(root)
    venue_by_id = {venue.venue_id: venue for venue in venues}
    raw_index = _load_json(root / "data" / "games" / "index.json", {"games": []})
    raw_games = raw_index.get("games", []) if isinstance(raw_index, dict) else raw_index
    manual_overrides = _manual_overrides_from_env()
    storm_motion_needed = any(
        str(item.get("status", "")).lower()
        in {GameStatus.IN_PROGRESS.value, GameStatus.WEATHER_DELAY.value}
        or bool(item.get("official_delay_active"))
        or bool(item.get("model_delay_active"))
        for item in raw_games
        if isinstance(item, dict)
    ) or any(override.official_delay_active for override in manual_overrides.values())
    now = datetime.now(UTC)
    forecast_horizon_hours = min(168, max(1, forecast_horizon_hours))
    limit = now + timedelta(hours=forecast_horizon_hours)
    provider = NwsGridProvider()
    global_provider = OpenMeteoEnsembleProvider()
    try:
        historical_durations = load_historical_delay_durations(
            root / "data" / "historical_delay_events.csv"
        )
    except (OSError, ValueError):
        historical_durations = []
    game_index: list[dict[str, Any]] = []
    source_health: dict[str, dict[str, Any]] = {
        "nws_forecast": {"status": "not_requested", "updated_at": now.isoformat()},
        "nws_alerts": {"status": "not_requested", "updated_at": None},
        "mrms_lightning": {"status": "not_requested", "updated_at": None},
        "mrms_storm_motion": {"status": "not_requested", "updated_at": None},
        "href_calibrated_thunder": {"status": "unavailable", "updated_at": None},
        "open_meteo_ifs_ensemble": {"status": "not_requested", "updated_at": None},
        "hrrr": {"status": "not_requested", "updated_at": None},
        "glm": {
            "status": "unavailable" if find_spec("netCDF4") is None else "not_requested",
            "updated_at": None,
            **(
                {"message": "Install the optional glm extra to decode GOES NetCDF granules."}
                if find_spec("netCDF4") is None
                else {}
            ),
        },
    }
    scheduled = 0
    forecasted = 0
    errors: list[str] = []
    alert_snapshots: dict[str, Any] = {}
    nws_venue_status: dict[str, dict[str, Any]] = {}
    hrrr_provider = HrrrPointProvider() if refresh_hrrr else None
    glm_provider = GoesGlmProvider() if find_spec("netCDF4") is not None else None
    hrrr_results: list[dict[str, Any]] = []
    glm_results: list[dict[str, Any]] = []
    try:
        alert_venues = [
            venue
            for venue in venues
            if not (venue.roof_type == RoofType.FIXED_DOME and venue.roof_weather_protection)
        ]
        alert_snapshots = NwsSevereThunderstormWarningProvider().fetch_for_venues(
            alert_venues, now=now
        )
        source_health["nws_alerts"] = {
            "status": (
                "ok" if all(snapshot.is_fresh for snapshot in alert_snapshots.values()) else "stale"
            ),
            "updated_at": now.isoformat(),
            "url": next(iter(alert_snapshots.values())).source_url if alert_snapshots else None,
            "venues_checked": len(alert_snapshots),
            "venue_alert_matches": sum(
                len(snapshot.alerts) for snapshot in alert_snapshots.values()
            ),
            "venues_with_active_warnings": sum(
                snapshot.has_active_warning for snapshot in alert_snapshots.values()
            ),
            "message": "Official NWS severe thunderstorm warning feed; polygons matched to venues",
        }
    except Exception as exc:
        source_health["nws_alerts"] = {
            "status": "error",
            "updated_at": now.isoformat(),
            "message": str(exc),
        }
    mrms_snapshot: MrmsSnapshot | None = None
    mrms_attempted = False
    mrms_features_by_venue: dict[tuple[str, str, bool], dict[str, Any]] = {}
    mrms_venue_coverage: dict[str, str] = {}
    href_provider = HrefCtProvider() if refresh_href else None
    href_venue_coverage: dict[str, str] = {}
    href_rows_by_game: dict[str, list[dict[str, Any]]] = {}
    href_errors_by_game: dict[str, list[str]] = {}
    href_issue_times: list[datetime] = []
    global_outlook_results: list[dict[str, Any]] = []
    if href_provider is not None:
        href_rows_by_game, href_errors_by_game = _prefetch_href_rows(
            href_provider,
            raw_games,
            venue_by_id=venue_by_id,
            policy_by_id=policy_by_id,
            now=now,
            limit=limit,
        )

    for summary in raw_games:
        mrms_features: dict[str, Any] | None = None
        storm_motion: dict[str, Any] = {"status": "unavailable", "tracks": []}
        hrrr_context: dict[str, Any] | None = None
        glm_context: dict[str, Any] | None = None
        hrrr_needed = False
        glm_needed = False
        href_rows: list[dict[str, Any]] = []
        href_errors: list[str] = []
        latest_qualifying_event_at: datetime | None = None
        current_density_covered = False
        forecast_hazards: list[HazardPoint] = []
        global_outlook: dict[str, Any] | None = None
        global_outlook_fetched_at: datetime | None = None
        global_outlook_url: str | None = None
        try:
            game = Game.model_validate(
                {
                    "game_id": summary.get("game_id", summary.get("id")),
                    "league": summary["league"],
                    "season": summary["season"],
                    "home_team": summary["home_team"],
                    "away_team": summary["away_team"],
                    "venue_id": summary.get("venue_id"),
                    "venue_name": summary.get("venue_name"),
                    "venue_city": summary.get("venue_city"),
                    "neutral_site": summary.get("neutral_site", False),
                    "kickoff_utc": summary.get("kickoff_utc", summary.get("date")),
                    "status": summary.get("status", "unknown"),
                    "status_source": summary.get("status_source", "unknown"),
                    "official_delay_active": summary.get("official_delay_active", False),
                    "model_delay_active": summary.get("model_delay_active", False),
                    "delay_started_at": summary.get("delay_started_at"),
                    "official_resume_at": summary.get("official_resume_at"),
                    "delay_source_note": summary.get("delay_source_note"),
                    "home_score": summary.get("home_score"),
                    "away_score": summary.get("away_score"),
                    "period": summary.get("period"),
                    "clock": summary.get("clock"),
                }
            )
            game = _apply_manual_override(game, manual_overrides.get(game.game_id))
            summary.update(game.model_dump(mode="json"))
        except (ValidationError, KeyError, TypeError) as exc:
            errors.append(f"invalid game row: {exc}")
            continue
        venue = venue_by_id.get(game.venue_id or "")
        policy = policy_by_id.get(venue.policy_id) if venue else None
        storm_motion_applicable = bool(
            game.status in {GameStatus.IN_PROGRESS, GameStatus.WEATHER_DELAY}
            or game.official_delay_active
            or game.model_delay_active
        )
        if not storm_motion_applicable:
            storm_motion = {}
        generated_at = datetime.now(UTC)
        scheduled += 1
        game_record = _load_json(root / "data" / "games" / f"{game.game_id}.json", {})
        game_record.setdefault("game", game.model_dump(mode="json"))

        forecast: dict[str, Any]
        if venue and venue.roof_type == RoofType.FIXED_DOME and venue.roof_weather_protection:
            forecast = {
                "generated_at": generated_at.isoformat(),
                "model_version": MODEL_VERSION,
                "game": game.model_dump(mode="json"),
                "venue": venue.model_dump(mode="json"),
                "policy": policy.model_dump(mode="json") if policy else None,
                "weather": None,
                "pregame": {
                    "delay_probability": 0.0,
                    "kickoff_delay_probability": 0.0,
                    "in_game_delay_probability": 0.0,
                    "multiple_delay_probability": 0.0,
                    "expected_delay_minutes": 0.0,
                    "hourly_delay_hazard": [],
                    "simulation_count": 0,
                },
                "delay": {
                    "active": False,
                    "officially_confirmed": False,
                    "model_delay_active": False,
                    "resume_cdf": [],
                },
                "quality": {
                    "weather_data_age_seconds": None,
                    "sports_data_age_seconds": 0,
                    "policy_verification": policy.verification.value if policy else "unknown",
                    "forecast_scope": "indoor",
                    "missing_sources": [],
                    "degraded": False,
                    "status": "indoor",
                    "message": "Indoor — lightning delay model disabled",
                },
            }
            forecasted += 1
        elif not venue or not policy:
            forecast = _unavailable_forecast(
                game,
                venue,
                policy,
                generated_at=generated_at,
                note="Venue or policy is unresolved.",
                forecast_scope="venue_unresolved",
            )
        elif game.status.value in ("completed", "cancelled", "postponed"):
            forecast = _unavailable_forecast(
                game,
                venue,
                policy,
                generated_at=generated_at,
                note=(
                    "Game completed; no pregame forecast snapshot was archived."
                    if game.status.value == "completed"
                    else f"Game is {game.status.value}; no active forecast is available."
                ),
                forecast_scope=(
                    "archive_missing"
                    if game.status.value == "completed"
                    else "game_unavailable"
                ),
            )
        elif game.kickoff_utc > limit:
            carried_forecast = _carry_forward_future_forecast(
                game_record, game, now=generated_at
            )
            if carried_forecast is not None:
                forecast = carried_forecast
                forecasted += 1
            elif game.kickoff_utc <= now + timedelta(
                hours=_GLOBAL_OUTLOOK_HORIZON_HOURS
            ):
                try:
                    (
                        global_outlook,
                        global_outlook_fetched_at,
                        global_outlook_url,
                    ) = global_provider.fetch_conditions_outlook(
                        venue, kickoff=game.kickoff_utc
                    )
                    global_outlook_results.append(
                        {
                            "status": "ok",
                            "venue_id": venue.venue_id,
                            "updated_at": global_outlook_fetched_at,
                            "url": global_outlook_url,
                        }
                    )
                    forecast = _global_weather_outlook_forecast(
                        game,
                        venue,
                        policy,
                        generated_at=generated_at,
                        outlook=global_outlook,
                        fetched_at=global_outlook_fetched_at,
                    )
                    forecasted += 1
                except Exception as exc:
                    global_outlook_results.append(
                        {
                            "status": "error",
                            "venue_id": venue.venue_id,
                            "updated_at": generated_at,
                            "message": str(exc),
                        }
                    )
                    forecast = _unavailable_forecast(
                        game,
                        venue,
                        policy,
                        generated_at=generated_at,
                        note=(
                            "The seven-day thunder outlook is not available this far ahead, "
                            "and the global conditions outlook could not be fetched."
                        ),
                        forecast_scope="forecast_pending",
                    )
            else:
                forecast = _unavailable_forecast(
                    game,
                    venue,
                    policy,
                    generated_at=generated_at,
                    note="Weather outlook opens within the published 14-day schedule window.",
                    forecast_scope="forecast_pending",
                )
        else:
            try:
                hrrr_needed = (
                    now - timedelta(hours=6)
                    <= game.kickoff_utc
                    <= now + timedelta(hours=48)
                )
                if hrrr_needed:
                    hrrr_cached = _cached_context(game_record, "hrrr_point")
                    if hrrr_provider is not None:
                        try:
                            hrrr_sample = hrrr_provider.sample_point(
                                venue.latitude,
                                venue.longitude,
                                at=max(game.kickoff_utc, now),
                                now=now,
                            )
                            hrrr_context = asdict(hrrr_sample)
                            hrrr_results.append(
                                {"status": "ok", "issued_at": hrrr_sample.issued_at}
                            )
                        except Exception as exc:
                            hrrr_results.append({"status": "error", "message": str(exc)})
                    elif hrrr_cached and _context_is_fresh(
                        hrrr_cached, "issued_at", now, timedelta(hours=12)
                    ):
                        hrrr_context = hrrr_cached
                        hrrr_results.append(
                            {
                                "status": "cached",
                                "issued_at": _timestamp(hrrr_cached.get("issued_at")),
                            }
                        )
                    else:
                        hrrr_results.append({"status": "unavailable"})

                glm_needed = (
                    game.kickoff_utc >= now - timedelta(hours=6)
                    and game.kickoff_utc <= now + timedelta(hours=6)
                ) or game.status.value in ("in_progress", "weather_delay")
                if glm_needed:
                    if glm_provider is None:
                        glm_results.append({"status": "unavailable"})
                    else:
                        try:
                            glm_sample = glm_provider.sample_point(
                                venue.latitude,
                                venue.longitude,
                                radius_miles=policy.trigger_radius_miles,
                                at=now,
                            )
                            glm_age = now - glm_sample.valid_end
                            if glm_age > timedelta(minutes=30) or glm_age < -timedelta(minutes=2):
                                raise ProviderError(
                                    "Latest GLM granule is "
                                    f"{int(max(0, glm_age.total_seconds()))} seconds old; "
                                    "observation suppressed"
                                )
                            glm_context = asdict(glm_sample)
                            glm_results.append({"status": "ok", "valid_at": glm_sample.valid_end})
                        except Exception as exc:
                            glm_results.append({"status": "error", "message": str(exc)})

                nws_error: str | None = None
                try:
                    nws_hazards, weather_fetched_at, weather_url = provider.fetch_hazards(
                        venue, kickoff=game.kickoff_utc
                    )
                    nws_age_seconds = max(
                        0, int((generated_at - weather_fetched_at).total_seconds())
                    )
                    nws_venue_status[f"{venue.venue_id}:{game.game_id}"] = {
                        "status": (
                            "stale"
                            if nws_age_seconds > 12 * 3600
                            else "ok"
                            if nws_hazards
                            else "degraded"
                        ),
                        "updated_at": weather_fetched_at.isoformat(),
                        "url": weather_url,
                        "age_seconds": nws_age_seconds,
                    }
                except Exception as exc:
                    nws_hazards = []
                    weather_fetched_at = generated_at
                    weather_url = ""
                    nws_error = str(exc)
                    nws_venue_status[f"{venue.venue_id}:{game.game_id}"] = {
                        "status": "error",
                        "updated_at": generated_at.isoformat(),
                        "message": nws_error,
                    }
                    if not href_rows_by_game.get(game.game_id):
                        try:
                            (
                                global_outlook,
                                global_outlook_fetched_at,
                                global_outlook_url,
                            ) = global_provider.fetch_conditions_outlook(
                                venue, kickoff=game.kickoff_utc
                            )
                            global_outlook_results.append(
                                {
                                    "status": "ok",
                                    "venue_id": venue.venue_id,
                                    "updated_at": global_outlook_fetched_at,
                                    "url": global_outlook_url,
                                }
                            )
                        except Exception as global_exc:
                            global_outlook_results.append(
                                {
                                    "status": "error",
                                    "venue_id": venue.venue_id,
                                    "updated_at": generated_at,
                                    "message": str(global_exc),
                                }
                            )

                near_term_exposure = (
                    game.kickoff_utc <= now + timedelta(hours=6)
                    or storm_motion_applicable
                )
                if not mrms_attempted and near_term_exposure:
                    mrms_attempted = True
                    try:
                        mrms_snapshot = MrmsSnapshot(include_storm_motion=storm_motion_needed)
                        source_health["mrms_lightning"] = {
                            "status": "ok",
                            "updated_at": mrms_snapshot.latest_valid_at.isoformat(),
                            "url": next(iter(mrms_snapshot.sources.values())),
                            "message": (
                                "Public MRMS probability grids and one-minute NLDN CG density; "
                                "regional observation proxy, not the venue's operational network"
                            ),
                        }
                        reflectivity_grid = getattr(mrms_snapshot, "_reflectivity_grid", None)
                        source_health["mrms_storm_motion"] = {
                            "status": (
                                "ok"
                                if reflectivity_grid is not None
                                else "unavailable"
                                if storm_motion_needed
                                else "not_requested"
                            ),
                            "updated_at": (
                                reflectivity_grid.valid_at.isoformat()
                                if reflectivity_grid is not None
                                else generated_at.isoformat()
                            ),
                            "url": reflectivity_grid.url if reflectivity_grid is not None else None,
                            "message": (
                                "MRMS reflectivity object motion is experimental and does not "
                                "identify lightning."
                                if reflectivity_grid is not None
                                else (
                                    "Reflectivity tracking is sampled only during active games "
                                    "or holds."
                                )
                            ),
                        }
                    except Exception as exc:
                        source_health["mrms_lightning"] = {
                            "status": "error",
                            "updated_at": generated_at.isoformat(),
                            "message": str(exc),
                        }
                        mrms_snapshot = None
                        source_health["mrms_storm_motion"] = {
                            "status": "error" if storm_motion_needed else "not_requested",
                            "updated_at": generated_at.isoformat(),
                            "message": str(exc),
                        }

                if mrms_snapshot and near_term_exposure:
                    cache_key = (venue.venue_id, policy.policy_id, storm_motion_applicable)
                    if cache_key not in mrms_features_by_venue:
                        mrms_features_by_venue[cache_key] = mrms_snapshot.sample(
                            venue,
                            policy,
                            include_storm_motion=storm_motion_applicable,
                        )
                    candidate_features = mrms_features_by_venue[cache_key]
                    mrms_age = (generated_at - mrms_snapshot.latest_valid_at).total_seconds()
                    if -300 <= mrms_age <= 600:
                        mrms_features = dict(candidate_features)
                        coverage = dict(mrms_features.get("coverage", {}))
                        product_valid_at = mrms_features.get("product_valid_at", {})
                        for product, valid_at in product_valid_at.items():
                            product_time = (
                                valid_at if isinstance(valid_at, datetime) else _timestamp(valid_at)
                            )
                            product_age = (
                                (generated_at - product_time).total_seconds()
                                if product_time is not None
                                else float("inf")
                            )
                            coverage[product] = bool(coverage.get(product)) and (
                                -300 <= product_age <= 600
                            )
                            if not coverage[product] and product.startswith("probability_"):
                                mrms_features[product] = None
                        if not coverage.get("cg_density_1min"):
                            mrms_features["cg_density_per_km2_min"] = {
                                "max": None,
                                "mean": None,
                                "p90": None,
                                "fraction_positive": None,
                                "cells": 0,
                            }
                            mrms_features["last_qualifying_event_at"] = None
                        mrms_features["coverage"] = coverage
                        mrms_features["has_venue_data"] = any(coverage.values())
                        current_density_covered = coverage.get("cg_density_1min", False)
                        if not mrms_features["has_venue_data"]:
                            mrms_features = None
                            mrms_venue_coverage[venue.venue_id] = "no_coverage"
                            source_health["mrms_lightning"]["status"] = "no_venue_data"
                            source_health["mrms_lightning"]["message"] = (
                                f"MRMS has no valid cells in the trigger radius for {venue.name}"
                            )
                        else:
                            mrms_venue_coverage[venue.venue_id] = (
                                "covered" if all(coverage.values()) else "partial"
                            )
                            source_health["mrms_lightning"]["status"] = (
                                "ok" if all(coverage.values()) else "partial"
                            )
                    else:
                        mrms_venue_coverage[venue.venue_id] = "stale"
                        source_health["mrms_lightning"]["status"] = "stale"
                        source_health["mrms_lightning"]["message"] = (
                            f"Latest MRMS product is {int(max(0, mrms_age))} seconds old; "
                            "live strike timing is suppressed"
                        )

                if mrms_features:
                    storm_echoes = mrms_features.get("storm_echoes")
                    reflectivity_time = (
                        _timestamp(storm_echoes.get("valid_at"))
                        if isinstance(storm_echoes, dict)
                        else None
                    )
                    reflectivity_age = (
                        (generated_at - reflectivity_time).total_seconds()
                        if reflectivity_time is not None
                        else float("inf")
                    )
                    if not storm_motion_applicable or not -120 <= reflectivity_age <= 900:
                        mrms_features["storm_echoes"] = None
                    if storm_motion_applicable:
                        storm_motion = _storm_motion_features(
                            _cached_context(game_record, "storm_motion"),
                            mrms_features.get("storm_echoes"),
                            venue,
                            policy,
                            now=generated_at,
                        )
                    prior_delay = game_record.get("delay", {})
                    previous_event = (
                        _timestamp(prior_delay.get("last_qualifying_event_at"))
                        if isinstance(prior_delay, dict)
                        else None
                    )
                    current_event = mrms_features.get("last_qualifying_event_at")
                    event_times = [
                        event
                        for event in (previous_event, current_event)
                        if isinstance(event, datetime)
                        and event <= generated_at + timedelta(minutes=2)
                    ]
                    latest_qualifying_event_at = max(event_times, default=None)
                elif venue and policy and storm_motion_applicable:
                    storm_motion = _storm_motion_features(
                        _cached_context(game_record, "storm_motion"),
                        None,
                        venue,
                        policy,
                        now=generated_at,
                    )

                if href_provider is not None:
                    href_rows = href_rows_by_game.get(game.game_id, [])
                    href_errors = href_errors_by_game.get(game.game_id, [])
                    href_venue_coverage[venue.venue_id] = (
                        "covered"
                        if href_rows and not href_errors
                        else "partial"
                        if href_rows
                        else "no_coverage"
                    )
                else:
                    href_rows = _cached_href_rows(game_record, now=generated_at)
                    href_venue_coverage[venue.venue_id] = (
                        "cached" if href_rows else "no_cached_data"
                    )
                href_issue_times.extend(
                    issued_at
                    for row in href_rows
                    if (issued_at := _timestamp(row.get("issued_at"))) is not None
                )
                href_hazards = _href_rows_to_hazards(href_rows, game.kickoff_utc)
                forecast_hazards = _blend_weather_hazards(
                    nws_hazards,
                    href_hazards,
                    mrms_features,
                    now=generated_at,
                    kickoff=game.kickoff_utc,
                    warmup_minutes=policy.warmup_exposure_minutes,
                    nowcast_weight_at_zero=float(
                        blending_config.get("nowcast_weight_at_zero", 0.9)
                    ),
                    nowcast_weight_at_60=float(blending_config.get("nowcast_weight_at_60", 0.2)),
                    href_weight=float(blending_config.get("href_weight", 1.0)),
                    nws_weight=float(blending_config.get("nws_weight", 0.0)),
                )
                forecast_hazards = [
                    point
                    for point in forecast_hazards
                    if point.valid_at is None or point.valid_at >= generated_at
                ]
                model_active = False
                officially_resumed = bool(
                    game.official_resume_at is not None and generated_at >= game.official_resume_at
                )
                if (
                    current_density_covered
                    and latest_qualifying_event_at
                    and not game.official_delay_active
                    and not officially_resumed
                ):
                    last_resume_window = latest_qualifying_event_at + timedelta(
                        minutes=policy.quiet_period_minutes + max(policy.restart_overhead.minutes)
                    )
                    within_game = (
                        game.kickoff_utc - timedelta(minutes=policy.warmup_exposure_minutes)
                        <= generated_at
                        <= game.kickoff_utc + timedelta(hours=6)
                    )
                    model_active = within_game and generated_at < last_resume_window
                game = game.model_copy(
                    update={
                        "model_delay_active": model_active,
                        "official_delay_active": (
                            False if officially_resumed else game.official_delay_active
                        ),
                        "status": (GameStatus.IN_PROGRESS if officially_resumed else game.status),
                        "delay_started_at": (
                            latest_qualifying_event_at
                            if model_active and not game.delay_started_at
                            else game.delay_started_at
                        ),
                    }
                )
                summary.update(game.model_dump(mode="json"))

                alert_features = _alert_snapshot_features(alert_snapshots.get(venue.venue_id))
                elapsed_seconds = (generated_at - game.kickoff_utc).total_seconds()
                floor_now_offset = int(elapsed_seconds // 300) * 5
                ceil_now_offset = math.ceil(elapsed_seconds / 300) * 5
                live_forecast = (
                    game.status in {GameStatus.IN_PROGRESS, GameStatus.WEATHER_DELAY}
                    and generated_at >= game.kickoff_utc
                )
                remaining_minutes: int | None = None
                if live_forecast:
                    remaining_minutes = _remaining_game_exposure_minutes(game, generated_at)
                    risk_start_offset = floor_now_offset + 5
                    risk_end_offset = floor_now_offset + remaining_minutes
                    window_is_current = True
                else:
                    risk_start_offset = max(
                        -policy.warmup_exposure_minutes, ceil_now_offset
                    )
                    risk_end_offset = DEFAULT_GAME_DURATION_MINUTES
                    window_is_current = risk_start_offset <= ceil_now_offset
                nws_age = generated_at - weather_fetched_at
                nws_fresh = timedelta(minutes=-5) <= nws_age <= timedelta(hours=12)
                href_fresh = bool(href_rows) and all(
                    (issued_at := _timestamp(row.get("issued_at"))) is not None
                    and timedelta(minutes=-5) <= generated_at - issued_at <= timedelta(hours=18)
                    for row in href_rows
                )
                clear_window_confirmed = _forecast_confirms_clear_window(
                    start_offset=risk_start_offset,
                    end_offset=risk_end_offset,
                    forecast_hazards=forecast_hazards,
                    nws_hazards=nws_hazards,
                    nws_fresh=nws_fresh,
                    href_hazards=href_hazards,
                    href_fresh=href_fresh,
                    window_is_current=window_is_current,
                    mrms_features=mrms_features,
                    alert_features=alert_features,
                    storm_motion=storm_motion,
                    delay_active=game.official_delay_active or game.model_delay_active,
                )
                exposure_state = _hazard_window_state(
                    forecast_hazards,
                    start_offset=risk_start_offset,
                    end_offset=risk_end_offset,
                )

                if not forecast_hazards:
                    href_error_note = href_errors[0] if href_errors else "no usable hourly fields"
                    forecast = _unavailable_forecast(
                        game,
                        venue,
                        policy,
                        generated_at=generated_at,
                        note=(
                            "Weather probability inputs unavailable. "
                            f"NWS: {nws_error or 'no usable forecast bins'}. "
                            f"HREF: {href_error_note}. "
                            "MRMS grid has no usable venue-radius data."
                        ),
                    )
                    if (
                        global_outlook is not None
                        and global_outlook_fetched_at is not None
                        and global_outlook_url is not None
                    ):
                        forecast = _global_weather_outlook_forecast(
                            game,
                            venue,
                            policy,
                            generated_at=generated_at,
                            outlook=global_outlook,
                            fetched_at=global_outlook_fetched_at,
                        )
                        forecasted += 1
                elif exposure_state != "storm" and not clear_window_confirmed:
                    forecast = _unavailable_forecast(
                        game,
                        venue,
                        policy,
                        generated_at=generated_at,
                        note=(
                            "Thunder inputs do not cover the full game exposure as clear. "
                            "A zero delay probability cannot be confirmed."
                        ),
                        forecast_scope="weather_unavailable",
                    )
                else:
                    if live_forecast:
                        result = simulate_remaining_game(
                            now=generated_at,
                            kickoff=game.kickoff_utc,
                            policy=policy,
                            hazards=forecast_hazards,
                            remaining_game_minutes=remaining_minutes or 0,
                            simulation_count=simulation_count,
                            rho=model_rho,
                            seed=2026,
                        )
                    else:
                        result = simulate_pregame(
                            kickoff=game.kickoff_utc,
                            policy=policy,
                            hazards=forecast_hazards,
                            simulation_count=simulation_count,
                            rho=model_rho,
                            seed=2026,
                        )
                    used_source_times = [weather_fetched_at] if nws_hazards else []
                    if hrrr_context:
                        hrrr_issued_at = _timestamp(hrrr_context.get("issued_at"))
                        if hrrr_issued_at is not None:
                            used_source_times.append(hrrr_issued_at)
                    if glm_context:
                        glm_valid_end = _timestamp(glm_context.get("valid_end"))
                        if glm_valid_end is not None:
                            used_source_times.append(glm_valid_end)
                    used_source_times.extend(
                        issued_at
                        for row in href_rows
                        if (issued_at := _timestamp(row.get("issued_at"))) is not None
                    )
                    if mrms_features:
                        for product, covered in mrms_features.get("coverage", {}).items():
                            product_time = mrms_features.get("product_valid_at", {}).get(product)
                            if covered and isinstance(product_time, datetime):
                                used_source_times.append(product_time)
                    snapshot_fetched_at = min(used_source_times, default=weather_fetched_at)
                    missing_sources = []
                    if hrrr_needed and hrrr_context is None:
                        missing_sources.append("HRRR point context")
                    if glm_needed and glm_context is None:
                        missing_sources.append("GOES GLM flash observations")
                    if not nws_hazards:
                        missing_sources.append("NWS probabilityOfThunder")
                    forecast_lead_minutes = int(
                        (game.kickoff_utc - generated_at).total_seconds() / 60
                    )
                    if not href_rows and forecast_lead_minutes <= 48 * 60:
                        missing_sources.insert(0, "SPC HREF CT calibrated thunder")
                    elif href_errors:
                        missing_sources.insert(0, "SPC HREF CT hourly coverage gaps")
                    mrms_coverage = mrms_features.get("coverage", {}) if mrms_features else {}
                    needs_near_term_observations = forecast_lead_minutes <= 6 * 60
                    if needs_near_term_observations and not any(
                        mrms_coverage.get(name, False)
                        for name in ("probability_next_30min", "probability_next_60min")
                    ):
                        missing_sources.insert(0, "MRMS near-term probability grids")
                    if needs_near_term_observations and not mrms_coverage.get(
                        "cg_density_1min", False
                    ):
                        missing_sources.insert(0, "MRMS current lightning density")
                    if alert_features.get("status") != "ok":
                        missing_sources.append("NWS severe thunderstorm warning feed")
                    snapshot = WeatherSnapshot(
                        venue_id=venue.venue_id,
                        fetched_at=snapshot_fetched_at,
                        source=(
                            " + ".join(
                                source_name
                                for source_name, present in (
                                    ("MRMS local lightning", bool(mrms_features)),
                                    ("SPC HREF CT thunder", bool(href_rows)),
                                    ("NWS regional thunder proxy", bool(nws_hazards)),
                                    ("HRRR point context", bool(hrrr_context)),
                                    ("GOES GLM flash observations", bool(glm_context)),
                                )
                                if present
                            )
                        ),
                        hazards=forecast_hazards,
                        venue_features={
                            **(mrms_features or {}),
                            **({"storm_motion": storm_motion} if storm_motion else {}),
                            "href_hourly": href_rows,
                            "nws_alerts": alert_features,
                            "hrrr_point": hrrr_context,
                            "glm_observation": glm_context,
                        },
                        missing_sources=missing_sources,
                        notes=(
                            [
                                "MRMS probabilities are sampled conservatively within the "
                                "configured radius; NLDN CG density is a one-minute public "
                                "proxy, not the venue operational lightning network.",
                                "NWS thunder probability is a regional forecast proxy, not a "
                                "calibrated venue-specific policy probability.",
                            ]
                            if mrms_features
                            else [
                                "NWS thunder probability is a regional forecast proxy; it is not "
                                "calibrated to venue policy radii."
                            ]
                        ),
                    )
                    pregame = PregameForecast(
                        delay_probability=result["delay_probability"],
                        kickoff_delay_probability=result["kickoff_delay_probability"],
                        in_game_delay_probability=result["in_game_delay_probability"],
                        multiple_delay_probability=result["multiple_delay_probability"],
                        expected_delay_minutes=result["expected_delay_minutes"],
                        hourly_delay_hazard=forecast_hazards,
                        simulation_count=simulation_count,
                    )
                    age = max(0, int((generated_at - snapshot_fetched_at).total_seconds()))
                    delay = DelayForecast(
                        active=game.official_delay_active or model_active,
                        officially_confirmed=game.official_delay_active,
                        model_delay_active=model_active,
                        last_qualifying_event_at=latest_qualifying_event_at,
                    )
                    local_nowcast_available = any(
                        mrms_coverage.get(name, False)
                        for name in ("probability_next_30min", "probability_next_60min")
                    )
                    long_range_outlook = forecast_lead_minutes > 48 * 60
                    forecast_scope = (
                        "regional_outlook"
                        if long_range_outlook
                        else "venue_nowcast_model"
                        if local_nowcast_available
                        else "regional_proxy"
                    )
                    quality_message = (
                        "Low-confidence NWS 3–7 day regional thunder outlook. Local radar, venue "
                        "lightning observations, and HREF coverage are unavailable at this lead; "
                        "the displayed delay-risk simulation is not calibrated to venue outcomes."
                        if long_range_outlook
                        else "HREF/NWS thunder probabilities are regional proxies. Local MRMS "
                        "lightning grids are unavailable; displayed venue-delay odds are "
                        "experimental and uncalibrated."
                        if not local_nowcast_available
                        else "Local MRMS nowcast contributes to this experimental delay estimate; "
                        "venue-level probabilities are not calibrated to representative outcomes."
                    )
                    quality = ForecastQuality(
                        weather_data_age_seconds=age,
                        sports_data_age_seconds=0,
                        policy_verification=policy.verification,
                        forecast_lead_minutes=forecast_lead_minutes,
                        forecast_scope=forecast_scope,
                        missing_sources=missing_sources,
                        degraded=True,
                        status=(
                            "NWS week-ahead regional outlook"
                            if long_range_outlook
                            else "experimental HREF/MRMS/NWS weather-delay model"
                        ),
                        message=quality_message,
                    )
                    forecast = GameForecast(
                        generated_at=generated_at,
                        model_version=MODEL_VERSION,
                        game=game,
                        venue=venue,
                        policy=policy,
                        weather=snapshot,
                        pregame=pregame,
                        delay=delay,
                        quality=quality,
                    ).model_dump(mode="json")
                    forecasted += 1
            except Exception as exc:
                errors.append(f"{game.game_id}: {exc}")
                forecast = _unavailable_forecast(
                    game,
                    venue,
                    policy,
                    generated_at=generated_at,
                    note=f"Weather provider unavailable: {exc}",
                )

        if venue and venue.venue_id in alert_snapshots:
            weather = forecast.get("weather")
            if not isinstance(weather, dict):
                weather = {
                    "venue_id": venue.venue_id,
                    "fetched_at": alert_snapshots[venue.venue_id].fetched_at.isoformat(),
                    "source": "NWS severe thunderstorm warning feed",
                    "hazards": [],
                    "venue_features": {},
                    "missing_sources": ["Weather forecast probability inputs"],
                    "notes": [],
                }
                forecast["weather"] = weather
            features = weather.setdefault("venue_features", {})
            if isinstance(features, dict):
                features["nws_alerts"] = _alert_snapshot_features(alert_snapshots[venue.venue_id])
        if hrrr_context is not None or glm_context is not None:
            weather = forecast.get("weather")
            if not isinstance(weather, dict):
                weather = {
                    "venue_id": venue.venue_id if venue else "unknown",
                    "fetched_at": generated_at.isoformat(),
                    "source": "supporting storm context",
                    "hazards": [],
                    "venue_features": {},
                    "missing_sources": [],
                    "notes": [],
                }
                forecast["weather"] = weather
            features = weather.setdefault("venue_features", {})
            if isinstance(features, dict):
                features["hrrr_point"] = hrrr_context
                features["glm_observation"] = glm_context
            forecast_quality = forecast.get("quality")
            if isinstance(forecast_quality, dict) and isinstance(
                forecast_quality.get("missing_sources"), list
            ):
                if hrrr_needed and hrrr_context is None:
                    if "HRRR point context" not in forecast_quality["missing_sources"]:
                        forecast_quality["missing_sources"].append("HRRR point context")
                if glm_needed and glm_context is None:
                    if "GOES GLM flash observations" not in forecast_quality["missing_sources"]:
                        forecast_quality["missing_sources"].append("GOES GLM flash observations")
        _dump_json(root / "data" / "games" / f"{game.game_id}.json", forecast)
        if game.official_delay_active or game.model_delay_active:
            if latest_qualifying_event_at and current_density_covered and policy:
                future_hazards = []
                for point in forecast_hazards:
                    valid_at = point.valid_at or (
                        game.kickoff_utc + timedelta(minutes=point.offset_minutes)
                    )
                    minutes_ahead = int((valid_at - generated_at).total_seconds() // 60)
                    if 0 <= minutes_ahead <= 180:
                        future_hazards.append(
                            point.model_copy(update={"offset_minutes": minutes_ahead})
                        )
                future_hazards, motion_adjustment = _motion_adjusted_hazards(
                    future_hazards,
                    storm_motion,
                    now=generated_at,
                )
                weather = forecast.get("weather")
                if isinstance(weather, dict):
                    features = weather.get("venue_features")
                    if isinstance(features, dict):
                        features["storm_motion"] = {
                            **storm_motion,
                            "model_adjustment": motion_adjustment,
                        }
                active_result = simulate_active_delay(
                    now=generated_at,
                    last_qualifying_event_at=latest_qualifying_event_at,
                    policy=policy,
                    future_hazards=future_hazards,
                    simulation_count=simulation_count,
                    rho=model_rho,
                    seed=2026,
                    horizon_minutes=180,
                )
                prior = {
                    "last_qualifying_event_at": latest_qualifying_event_at,
                    "earliest_weather_clear_at": active_result["earliest_weather_clear_at"],
                    "weather_clear_cdf": [
                        {
                            "at": (
                                generated_at + timedelta(minutes=point["minutes_from_now"])
                            ).isoformat(),
                            "probability": point["probability"],
                        }
                        for point in active_result["weather_clear_cdf"]
                    ],
                    "resume_cdf": [
                        {
                            "at": (
                                generated_at + timedelta(minutes=point["minutes_from_now"])
                            ).isoformat(),
                            "probability": point["probability"],
                        }
                        for point in active_result["resume_cdf"]
                    ],
                    "resume_p50": (
                        generated_at + timedelta(minutes=active_result["resume_p50_minutes"])
                        if active_result["resume_p50_minutes"] is not None
                        else None
                    ),
                    "resume_p75": (
                        generated_at + timedelta(minutes=active_result["resume_p75_minutes"])
                        if active_result["resume_p75_minutes"] is not None
                        else None
                    ),
                    "resume_p90": (
                        generated_at + timedelta(minutes=active_result["resume_p90_minutes"])
                        if active_result["resume_p90_minutes"] is not None
                        else None
                    ),
                    "probability_additional_minutes": active_result[
                        "probability_additional_minutes"
                    ],
                    "source": "MRMS observed qualifying lightning + conditional policy simulation",
                    "notes": [
                        "MRMS NLDN cloud-to-ground density gives a one-minute observation time, "
                        "not an exact strike time; the venue may use another lightning network.",
                        "NWS/MRMS future hazards are experimental regional inputs, not a restart "
                        "decision or an official venue all-clear.",
                    ],
                }
                if motion_adjustment.get("applied"):
                    prior["source"] += " + experimental MRMS storm motion"
                    prior["notes"].append(
                        "A small, capped future-hazard pulse uses a fresh approaching MRMS "
                        "reflectivity track for 30–180 minutes; echoes are not lightning and "
                        "this adjustment has not been calibrated on historical storm tracks."
                    )
            elif historical_durations:
                prior = historical_delay_duration_prior(
                    now=generated_at,
                    durations_minutes=historical_durations,
                    delay_started_at=game.delay_started_at,
                )
            else:
                prior = {
                    "resume_cdf": [],
                    "source": "resume estimate unavailable",
                    "notes": [
                        "No fresh local lightning observation or historical duration prior "
                        "is available for this active delay."
                    ],
                }
            forecast.setdefault("delay", {}).update(prior)
            forecast["delay"]["active"] = True
            forecast["delay"]["officially_confirmed"] = game.official_delay_active
            forecast["delay"]["model_delay_active"] = game.model_delay_active
            forecast["quality"]["degraded"] = True
            if not latest_qualifying_event_at or not current_density_covered:
                if historical_durations:
                    forecast["quality"]["status"] = (
                        "historical duration prior; live lightning unavailable"
                    )
                    forecast["quality"]["message"] = (
                        "Resume distribution is a broad historical prior. Latest qualifying "
                        "lightning and the all-clear timer are unknown."
                    )
                else:
                    forecast["quality"]["status"] = "active delay; resume estimate unavailable"
                    forecast["quality"]["message"] = (
                        "No fresh local lightning observation or historical duration prior "
                        "is available to estimate resumption."
                    )
                forecast["quality"].setdefault("missing_sources", []).append(
                    "MRMS latest qualifying lightning"
                )
            else:
                forecast["quality"]["status"] = "experimental MRMS conditional resume simulation"
                forecast["quality"]["message"] = (
                    "Resume CDF conditions on public MRMS cloud-to-ground density. "
                    "The venue's operational lightning feed may differ."
                )
            _dump_json(root / "data" / "games" / f"{game.game_id}.json", forecast)
        summary["forecast"] = {
            "delay_probability": forecast.get("pregame", {}).get("delay_probability")
            if forecast.get("pregame")
            else None,
            "quality": forecast.get("quality", {}).get("status"),
            "generated_at": forecast.get("generated_at"),
        }
        summary.update(_card_forecast_projection(forecast))
        game_index.append(summary)

    if nws_venue_status:
        nws_counts = {
            status: sum(item.get("status") == status for item in nws_venue_status.values())
            for status in ("ok", "degraded", "stale", "error")
        }
        nws_total = len(nws_venue_status)
        if nws_counts["ok"] == nws_total:
            nws_status = "ok"
        elif nws_counts["error"] == nws_total:
            nws_status = "error"
        elif nws_counts["stale"] == nws_total:
            nws_status = "stale"
        else:
            nws_status = "partial"
        age_values = [
            int(item["age_seconds"])
            for item in nws_venue_status.values()
            if isinstance(item.get("age_seconds"), (int, float))
        ]
        updated_values = [
            parsed
            for item in nws_venue_status.values()
            if (parsed := _timestamp(item.get("updated_at"))) is not None
        ]
        source_health["nws_forecast"] = {
            "status": nws_status,
            "updated_at": min(updated_values).isoformat() if updated_values else now.isoformat(),
            "age_seconds": max(age_values, default=None),
            "age_minutes": max(age_values, default=0) / 60,
            "freshness_limit_minutes": 720,
            "venues_considered": nws_total,
            "venue_status": nws_counts,
            "message": (
                "NWS probabilityOfThunder regional proxy; not venue-radius observations. "
                f"Venue results: {nws_counts['ok']} current, {nws_counts['degraded']} empty, "
                f"{nws_counts['stale']} stale, {nws_counts['error']} errors."
            ),
        }

    if global_outlook_results:
        successful_outlooks = [
            item for item in global_outlook_results if item.get("status") == "ok"
        ]
        global_times = [
            value
            for item in successful_outlooks
            if (value := item.get("updated_at")) is not None
        ]
        last_global_time = max(global_times, default=None)
        global_age_seconds = (
            max(0, int((datetime.now(UTC) - last_global_time).total_seconds()))
            if isinstance(last_global_time, datetime)
            else None
        )
        source_health["open_meteo_ifs_ensemble"] = {
            "status": (
                "ok"
                if len(successful_outlooks) == len(global_outlook_results)
                else "partial"
                if successful_outlooks
                else "error"
            ),
            "updated_at": last_global_time.isoformat() if last_global_time else now.isoformat(),
            "retrieved_at": datetime.now(UTC).isoformat(),
            "age_seconds": global_age_seconds,
            "age_minutes": global_age_seconds / 60 if global_age_seconds is not None else None,
            "freshness_limit_minutes": 360,
            "venues_considered": len(global_outlook_results),
            "venues_with_data": len(successful_outlooks),
            "url": successful_outlooks[-1].get("url") if successful_outlooks else None,
            "message": (
                "Global ECMWF IFS ensemble weather-code conditions only; approximately 25 km "
                "grid; 3-hour steps through 144 hours, 6-hour steps afterward. This field "
                "cannot estimate thunderstorms, "
                "venue delay odds, or observed storm motion."
            ),
        }

    if mrms_snapshot is not None:
        covered_venues = sum(value == "covered" for value in mrms_venue_coverage.values())
        partial_venues = sum(value == "partial" for value in mrms_venue_coverage.values())
        no_coverage_venues = sum(value == "no_coverage" for value in mrms_venue_coverage.values())
        stale_venues = sum(value == "stale" for value in mrms_venue_coverage.values())
        venue_count = len(mrms_venue_coverage)
        if venue_count == 0 or covered_venues == venue_count:
            mrms_status = "ok"
        elif stale_venues == venue_count:
            mrms_status = "stale"
        elif covered_venues or partial_venues:
            mrms_status = "partial"
        else:
            mrms_status = "no_venue_data"
        source_health["mrms_lightning"].update(
            {
                "status": mrms_status,
                "message": (
                    f"Venue grids: {covered_venues} complete, {partial_venues} partial, "
                    f"{no_coverage_venues} without coverage, {stale_venues} stale. "
                    "MRMS is a regional public proxy, not the venue operational sensor."
                ),
                "venue_coverage": {
                    "covered": covered_venues,
                    "partial": partial_venues,
                    "no_coverage": no_coverage_venues,
                    "stale": stale_venues,
                },
            }
        )

    if refresh_href:
        href_counts = {
            status: sum(value == status for value in href_venue_coverage.values())
            for status in ("covered", "partial", "no_coverage")
        }
        considered = sum(href_counts.values())
        latest_issue = max(href_issue_times, default=None)
        issue_age_seconds = (
            max(0, int((datetime.now(UTC) - latest_issue).total_seconds()))
            if latest_issue is not None
            else None
        )
        href_source_status = (
            "not_requested"
            if considered == 0
            else "stale"
            if issue_age_seconds is not None and issue_age_seconds > 18 * 3600
            else "ok"
            if href_counts["covered"] == considered
            else "partial"
            if href_counts["covered"] or href_counts["partial"]
            else "no_venue_data"
        )
        source_health["href_calibrated_thunder"].update(
            {
                "status": href_source_status,
                "updated_at": latest_issue.isoformat() if latest_issue else None,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "age_seconds": issue_age_seconds,
                "age_minutes": issue_age_seconds / 60 if issue_age_seconds is not None else None,
                "freshness_limit_minutes": 1080,
                "venues_considered": considered,
                "venue_coverage": href_counts,
                "message": (
                    "SPC HREF CT calibrated one-hour regional thunder probability; "
                    "grid probabilities do not represent venue policy outcomes."
                ),
            }
        )
    else:
        cached_count = sum(value == "cached" for value in href_venue_coverage.values())
        source_health["href_calibrated_thunder"].update(
            {
                "status": "cached" if cached_count else "not_requested",
                "updated_at": max(href_issue_times).isoformat() if href_issue_times else None,
                "retrieved_at": now.isoformat(),
                "age_seconds": max(0, int((now - max(href_issue_times)).total_seconds()))
                if href_issue_times
                else None,
                "age_minutes": max(0, int((now - max(href_issue_times)).total_seconds())) / 60
                if href_issue_times
                else None,
                "freshness_limit_minutes": 1080,
                "venues_with_cached_data": cached_count,
                "message": "HREF downloads are skipped on the five-minute live refresh.",
            }
        )

    if hrrr_results:
        hrrr_success_count = sum(item.get("status") in ("ok", "cached") for item in hrrr_results)
        hrrr_issue_times = [
            value for item in hrrr_results if (value := item.get("issued_at")) is not None
        ]
        oldest_issue = min(hrrr_issue_times, default=None)
        hrrr_age_seconds = (
            max(0, int((datetime.now(UTC) - oldest_issue).total_seconds()))
            if oldest_issue
            else None
        )
        source_health["hrrr"] = {
            "status": (
                "ok"
                if hrrr_success_count == len(hrrr_results)
                else "partial"
                if hrrr_success_count
                else "error"
            ),
            "updated_at": oldest_issue.isoformat() if oldest_issue else None,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "age_seconds": hrrr_age_seconds,
            "age_minutes": hrrr_age_seconds / 60 if hrrr_age_seconds is not None else None,
            "freshness_limit_minutes": 720,
            "games_considered": len(hrrr_results),
            "games_with_data": hrrr_success_count,
            "message": (
                "HRRR nearest-grid reflectivity, rain rate, CAPE and wind; supporting context only."
            ),
        }
    elif not refresh_hrrr:
        source_health["hrrr"] = {
            "status": "cached",
            "updated_at": None,
            "retrieved_at": now.isoformat(),
            "message": "Hourly HRRR sampling is skipped on the five-minute live refresh.",
        }

    if glm_results:
        glm_success_count = sum(item.get("status") == "ok" for item in glm_results)
        glm_times = [value for item in glm_results if (value := item.get("valid_at")) is not None]
        latest_glm = max(glm_times, default=None)
        glm_age_seconds = (
            max(0, int((datetime.now(UTC) - latest_glm).total_seconds())) if latest_glm else None
        )
        all_unavailable = all(item.get("status") == "unavailable" for item in glm_results)
        source_health["glm"] = {
            "status": (
                "unavailable"
                if all_unavailable
                else "ok"
                if glm_success_count == len(glm_results)
                else "partial"
                if glm_success_count
                else "error"
            ),
            "updated_at": latest_glm.isoformat() if latest_glm else None,
            "retrieved_at": datetime.now(UTC).isoformat(),
            "age_seconds": glm_age_seconds,
            "freshness_limit_minutes": 30,
            "games_considered": len(glm_results),
            "games_with_data": glm_success_count,
            "message": "GOES GLM total-flash observations; satellite proxy, not a venue sensor.",
        }

    generated_at = datetime.now(UTC)
    archived_forecasts = _archive_issuances(
        root, game_index, source_health, generated_at=generated_at
    )
    _dump_json(
        root / "data" / "games" / "index.json",
        {"generated_at": generated_at.isoformat(), "games": game_index},
    )
    manifest_sources = _load_json(root / "data" / "manifest.json", {}).get("sources", {})
    manifest_sources.update(source_health)
    manifest_sources["nfl_schedule"] = manifest_sources.get("nfl_scoreboard", {"status": "unknown"})
    manifest_sources["college_schedule"] = manifest_sources.get(
        "college_scoreboard", {"status": "unknown"}
    )
    manifest = DataManifest(
        version=generated_at.strftime("%Y%m%dT%H%M%SZ"),
        generated_at=generated_at,
        sources=manifest_sources,
    )
    _dump_json(root / "data" / "manifest.json", manifest)
    return {
        "games_seen": scheduled,
        "forecasts_available": forecasted,
        "archived_forecasts": archived_forecasts,
        "errors": errors[:20],
        "source_health": source_health,
    }


def validate_configuration(*, root: Path = ROOT) -> dict[str, Any]:
    venues, policies = load_registry(root)
    warnings = [
        f"{venue.venue_id}: policy {venue.policy_id} is "
        f"{policies[venue.policy_id].verification.value}"
        for venue in venues
        if policies[venue.policy_id].verification
        in (PolicyVerification.UNKNOWN, PolicyVerification.VENUE_ASSUMPTION)
    ]
    return {"venues": len(venues), "policies": len(policies), "warnings": warnings}
