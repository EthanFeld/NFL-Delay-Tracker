from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime, timedelta

from nfl_delay_tracker.pipeline import (
    _archive_issuances,
    _card_forecast_projection,
    _compact_archived_features,
)


def test_compact_weather_features_retain_provider_provenance() -> None:
    compact = _compact_archived_features(
        {
            "hrrr_point": {
                "source_url": "https://example.test/hrrr.grib2",
                "field_source_urls": {"cape_j_kg": "https://example.test/hrrr.grib2"},
                "cape_j_kg": 1200,
            },
            "glm_observation": {
                "source_url": "https://example.test/glm.nc",
                "flash_count": 3,
            },
        }
    )

    assert compact["hrrr_point"]["source_url"].endswith("hrrr.grib2")
    assert compact["hrrr_point"]["field_source_urls"]["cape_j_kg"].endswith("hrrr.grib2")
    assert compact["glm_observation"]["source_url"].endswith("glm.nc")


def test_card_projection_keeps_venue_risk_and_active_resume_fields() -> None:
    projection = _card_forecast_projection(
        {
            "generated_at": "2026-09-20T12:00:00Z",
            "model_version": "test",
            "venue": {"name": "Open Field", "timezone": "America/New_York", "roof_type": "outdoor"},
            "policy": {"trigger_radius_miles": 8},
            "pregame": {
                "delay_probability": 0.3,
                "kickoff_delay_probability": 0.2,
                "in_game_delay_probability": 0.1,
                "expected_delay_minutes": 25,
                "hourly_delay_hazard": [{"probability": 0.5}],
            },
            "delay": {
                "active": True,
                "officially_confirmed": True,
                "resume_p50": "2026-09-20T12:30:00Z",
                "resume_p75": "2026-09-20T12:45:00Z",
                "resume_p90": "2026-09-20T13:00:00Z",
                "probability_additional_minutes": {"30": 0.6},
                "resume_cdf": [{"probability": 0.6}],
            },
            "quality": {"status": "experimental"},
            "weather": {
                "venue_features": {
                    "nws_alerts": {
                        "status": "ok",
                        "has_active_warning": True,
                        "alerts": [{"headline": "Severe thunderstorm warning"}],
                    },
                    "hrrr_point": {"cape_j_kg": 1400},
                    "global_weather_outlook": {
                        "model": "ECMWF IFS ensemble",
                        "valid_at": "2026-09-20T12:00:00Z",
                        "condition_member_counts": {"clear_or_cloudy": 12},
                    },
                }
            },
        }
    )

    assert projection["venue"]["roof_type"] == "outdoor"
    assert projection["pregame"]["kickoff_delay_probability"] == 0.2
    assert projection["pregame"]["in_game_delay_probability"] == 0.1
    assert projection["delay"]["resume_p90"] == "2026-09-20T13:00:00Z"
    assert projection["delay"]["probability_additional_minutes"]["30"] == 0.6
    assert projection["weather"]["venue_features"]["nws_alerts"]["has_active_warning"]
    assert (
        projection["weather"]["venue_features"]["global_weather_outlook"]
        ["condition_member_counts"]["clear_or_cloudy"]
        == 12
    )
    assert "hourly_delay_hazard" not in projection["pregame"]
    assert "hrrr_point" not in projection["weather"]["venue_features"]


def test_archive_is_compact_rate_limited_and_rotated(tmp_path) -> None:
    game_id = "nfl_test_1"
    issued_at = datetime(2026, 9, 20, 12, tzinfo=UTC)
    forecast_path = tmp_path / "data" / "games" / f"{game_id}.json"
    forecast_path.parent.mkdir(parents=True)

    def save_forecast(at: datetime, *, active: bool = False) -> None:
        forecast_path.write_text(
            json.dumps(
                {
                    "generated_at": at.isoformat(),
                    "model_version": "test",
                    "game": {"game_id": game_id, "kickoff_utc": at.isoformat()},
                    "policy": {"policy_id": "test", "trigger_radius_miles": 8},
                    "weather": {
                        "source": "fixture",
                        "fetched_at": at.isoformat(),
                        "venue_features": {"href_hourly": [{"probability": 0.25}]},
                        "hazards": [
                            {
                                "offset_minutes": offset,
                                "probability": 0.1,
                                "source": "repeated source text",
                                "valid_at": at.isoformat(),
                            }
                            for offset in range(0, 3000, 5)
                        ],
                    },
                    "pregame": {
                        "delay_probability": 0.25,
                        "hourly_delay_hazard": [{"probability": 0.1}] * 600,
                    },
                    "delay": {"active": active},
                    "quality": {"status": "test"},
                }
            ),
            encoding="utf-8",
        )

    game_index = [{"game_id": game_id}]
    save_forecast(issued_at)
    assert _archive_issuances(tmp_path, game_index, {}, generated_at=issued_at) == 1
    archive_path = tmp_path / "data" / "archive" / "issued-forecasts-2026-09-20.jsonl"
    row = json.loads(archive_path.read_text(encoding="utf-8").splitlines()[0])
    assert len(row["hazard_curve_5m"]) == 600
    assert len(json.dumps(row)) < 25_000
    assert "hourly_delay_hazard" not in row["pregame"]

    save_forecast(issued_at + timedelta(minutes=5))
    assert (
        _archive_issuances(
            tmp_path,
            game_index,
            {},
            generated_at=issued_at + timedelta(minutes=5),
        )
        == 0
    )
    save_forecast(issued_at + timedelta(minutes=10), active=True)
    assert (
        _archive_issuances(
            tmp_path,
            game_index,
            {},
            generated_at=issued_at + timedelta(minutes=10),
        )
        == 1
    )

    next_day = issued_at + timedelta(days=1)
    save_forecast(next_day)
    assert _archive_issuances(tmp_path, game_index, {}, generated_at=next_day) == 1
    compressed = archive_path.with_suffix(".jsonl.gz")
    with gzip.open(compressed, "rt", encoding="utf-8") as archive:
        assert len(archive.read().splitlines()) == 2

    later = issued_at + timedelta(days=60)
    save_forecast(later)
    assert _archive_issuances(tmp_path, game_index, {}, generated_at=later) == 1
    assert compressed.exists()
