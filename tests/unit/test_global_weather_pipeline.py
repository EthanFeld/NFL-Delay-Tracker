from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import yaml

from nfl_delay_tracker.pipeline import refresh_forecasts
from nfl_delay_tracker.providers import open_meteo
from nfl_delay_tracker.providers.alerts import NwsSevereThunderstormWarningProvider
from nfl_delay_tracker.providers.http import ProviderError
from nfl_delay_tracker.providers.nws import NwsGridProvider


def test_out_of_coverage_game_publishes_conditions_without_delay_odds(
    tmp_path, monkeypatch
) -> None:
    config_dir = tmp_path / "config"
    games_dir = tmp_path / "data" / "games"
    config_dir.mkdir()
    games_dir.mkdir(parents=True)
    (config_dir / "venues.yaml").write_text(
        yaml.safe_dump(
            {
                "venues": {
                    "rio": {
                        "venue_id": "rio",
                        "name": "Maracanã Stadium",
                        "home_teams": ["NFL International Game"],
                        "league": "NFL",
                        "latitude": -22.9121619,
                        "longitude": -43.2311861,
                        "timezone": "America/Sao_Paulo",
                        "roof_type": "outdoor",
                        "roof_weather_protection": False,
                        "policy_id": "assumed",
                    }
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    (config_dir / "policies.yaml").write_text(
        yaml.safe_dump(
            {
                "policies": {
                    "assumed": {
                        "policy_id": "assumed",
                        "trigger_radius_miles": 8,
                        "monitor_radii_miles": [8, 20],
                        "quiet_period_minutes": 30,
                        "warmup_exposure_minutes": 30,
                        "clearance_mode": "circle",
                        "restart_overhead": {"minutes": [0], "weights": [1]},
                        "verification": "venue_assumption",
                        "notes": "Test assumption.",
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (config_dir / "model.yaml").write_text(
        "initial_latent_correlation: 0.45\ntemporal_blending: {}\n", encoding="utf-8"
    )
    kickoff = datetime.now(UTC) + timedelta(hours=120)
    game_id = "nfl_2026_401872960"
    (games_dir / "index.json").write_text(
        json.dumps(
            {
                "games": [
                    {
                        "game_id": game_id,
                        "league": "NFL",
                        "season": 2026,
                        "home_team": "Dallas Cowboys",
                        "away_team": "Baltimore Ravens",
                        "venue_id": "rio",
                        "venue_name": "Maracanã Stadium",
                        "venue_city": "Rio de Janeiro",
                        "neutral_site": True,
                        "kickoff_utc": kickoff.isoformat(),
                        "status": "scheduled",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "data" / "manifest.json").write_text("{}", encoding="utf-8")

    def no_nws(_self, _venue, *, kickoff):
        raise ProviderError("NWS is not available at this venue")

    outlook = {
        "model": "ECMWF IFS ensemble",
        "valid_at": kickoff.replace(minute=0, second=0, microsecond=0).isoformat(),
        "native_resolution_hours": 3,
        "grid_resolution_km": 25,
        "member_count": 50,
        "valid_member_count": 50,
        "condition_member_counts": {
            "clear_or_cloudy": 30,
            "fog": 0,
            "precipitation_or_snow": 18,
            "other_or_unclassified": 2,
        },
        "attribution_url": "https://open-meteo.com/en/docs/ensemble-api",
        "source_url": "https://ensemble-api.open-meteo.com/v1/ensemble?model=test",
        "thunderstorm_probability": None,
        "storm_motion": "unavailable",
    }
    fetched_at = datetime.now(UTC)
    monkeypatch.setattr(NwsGridProvider, "fetch_hazards", no_nws)
    monkeypatch.setattr(
        open_meteo.OpenMeteoEnsembleProvider,
        "fetch_conditions_outlook",
        lambda _self, _venue, *, kickoff: (
            outlook,
            fetched_at,
            outlook["source_url"],
        ),
    )
    monkeypatch.setattr(
        NwsSevereThunderstormWarningProvider,
        "fetch_for_venues",
        lambda _self, _venues, *, now: {},
    )

    result = refresh_forecasts(
        root=tmp_path,
        simulation_count=100,
        refresh_href=False,
        refresh_hrrr=False,
    )

    forecast = json.loads((games_dir / f"{game_id}.json").read_text(encoding="utf-8"))
    index = json.loads((games_dir / "index.json").read_text(encoding="utf-8"))
    card = index["games"][0]
    assert result["forecasts_available"] == 1
    assert forecast["quality"]["forecast_scope"] == "global_weather_outlook"
    assert forecast["pregame"] is None
    assert forecast["weather"]["hazards"] == []
    assert forecast["weather"]["venue_features"]["global_weather_outlook"] == outlook
    assert card["quality"]["forecast_scope"] == "global_weather_outlook"
    assert (
        card["weather"]["venue_features"]["global_weather_outlook"]["condition_member_counts"]
        == outlook["condition_member_counts"]
    )
    assert result["source_health"]["open_meteo_ifs_ensemble"]["status"] == "ok"
