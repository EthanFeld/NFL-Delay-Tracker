import json
import random
from datetime import UTC, datetime, timedelta

import pytest

from nfl_delay_tracker.geo import distance_miles, in_policy_circle, point_on_ring
from nfl_delay_tracker.model.calibration import brier_score, fit_logistic, reliability_bins
from nfl_delay_tracker.model.hazard import probability_per_bin
from nfl_delay_tracker.model.simulator import (
    historical_delay_duration_prior,
    simulate_active_delay,
    simulate_pregame,
    simulate_trajectory,
)
from nfl_delay_tracker.model.trajectories import correlated_events, estimate_latent_correlation
from nfl_delay_tracker.models import Game, HazardPoint, League, RestartOverhead, WeatherPolicy
from nfl_delay_tracker.pipeline import (
    _carry_forward_future_forecast,
    _write_game_status,
    load_registry,
)


def _outdoor_policy() -> WeatherPolicy:
    _, policies = load_registry()
    return policies["venue_assumption_8mi_30min"]


def _point(offset: int, probability: float) -> HazardPoint:
    return HazardPoint(offset_minutes=offset, probability=probability, source="test")


def test_short_refresh_carries_fresh_forecast_without_old_alerts() -> None:
    now = datetime(2026, 9, 20, 12, tzinfo=UTC)
    game = Game(
        game_id="week-ahead",
        league=League.NFL,
        season=2026,
        home_team="Home",
        away_team="Away",
        kickoff_utc=now + timedelta(hours=120),
    )
    previous = {
        "generated_at": (now - timedelta(hours=1)).isoformat(),
        "game": {"status": "scheduled"},
        "pregame": {"delay_probability": 0.04},
        "quality": {"forecast_scope": "regional_outlook"},
        "weather": {
            "venue_features": {
                "nws_alerts": {"status": "ok", "alerts": [{"headline": "old alert"}]}
            }
        },
    }

    carried = _carry_forward_future_forecast(previous, game, now=now)

    assert carried is not None
    assert carried["pregame"] == {"delay_probability": 0.04}
    assert carried["game"]["game_id"] == game.game_id
    assert carried["weather"]["venue_features"]["nws_alerts"] == {
        "status": "stale",
        "alerts": [],
    }
    assert previous["weather"]["venue_features"]["nws_alerts"]["alerts"]


@pytest.mark.parametrize("lead_hours", [120, 240])
def test_short_refresh_carries_global_conditions_outlook_without_fake_pregame_risk(
    lead_hours: int,
) -> None:
    now = datetime(2026, 9, 20, 12, tzinfo=UTC)
    game = Game(
        game_id="global-outlook",
        league=League.NFL,
        season=2026,
        home_team="Home",
        away_team="Away",
        kickoff_utc=now + timedelta(hours=lead_hours),
    )
    global_outlook = {
        "model": "ECMWF IFS ensemble",
        "valid_at": "2026-09-25T12:00:00+00:00",
        "condition_member_counts": {"clear_or_cloudy": 18},
    }
    previous = {
        "generated_at": (now - timedelta(hours=1)).isoformat(),
        "game": {"status": "scheduled"},
        "pregame": None,
        "quality": {"forecast_scope": "global_weather_outlook"},
        "weather": {"venue_features": {"global_weather_outlook": global_outlook}},
    }

    carried = _carry_forward_future_forecast(previous, game, now=now)

    assert carried is not None
    assert carried["pregame"] is None
    assert carried["weather"]["venue_features"]["global_weather_outlook"] == global_outlook
    assert carried["quality"]["forecast_scope"] == "global_weather_outlook"


def test_live_status_write_preserves_week_ahead_forecast(tmp_path) -> None:
    now = datetime(2026, 9, 20, 12, tzinfo=UTC)
    game = Game(
        game_id="week-ahead-preserved",
        league=League.NFL,
        season=2026,
        home_team="Home",
        away_team="Away",
        kickoff_utc=now + timedelta(hours=120),
    )
    record_path = tmp_path / "data" / "games" / f"{game.game_id}.json"
    record_path.parent.mkdir(parents=True)
    previous = {
        "generated_at": (now - timedelta(hours=1)).isoformat(),
        "game": {"game_id": game.game_id, "status": "scheduled"},
        "pregame": {"delay_probability": 0.04},
        "quality": {"forecast_scope": "regional_outlook"},
        "weather": {"hazards": [{"probability": 0.01}]},
    }
    record_path.write_text(json.dumps(previous), encoding="utf-8")

    _write_game_status(tmp_path, game.game_id, game)

    saved = json.loads(record_path.read_text(encoding="utf-8"))
    assert saved["game"]["status"] == game.status.value
    assert saved["pregame"] == previous["pregame"]
    assert saved["quality"] == previous["quality"]
    assert saved["weather"] == previous["weather"]
    assert _carry_forward_future_forecast(saved, game, now=now) is not None


def test_zero_hazard_produces_zero_delay_probability() -> None:
    result = simulate_pregame(
        kickoff=datetime(2026, 9, 20, tzinfo=UTC),
        policy=_outdoor_policy(),
        hazards=[_point(-60, 0), _point(600, 0)],
        simulation_count=100,
        seed=4,
    )
    assert result["delay_probability"] == 0
    assert result["kickoff_delay_probability"] == 0
    assert result["in_game_delay_probability"] == 0


def test_event_at_kickoff_creates_kickoff_hold() -> None:
    hazards = [_point(-5, 0), _point(0, 1), _point(5, 0), _point(600, 0)]
    result = simulate_trajectory(
        kickoff=datetime(2026, 9, 20, tzinfo=UTC),
        policy=_outdoor_policy(),
        hazards=hazards,
        seed=22,
    )
    assert result.kickoff_delayed
    assert result.holds[0].phase == "pregame"
    assert result.holds[0].weather_clear_at >= result.holds[0].started_at
    assert result.holds[0].football_resume_at >= result.holds[0].weather_clear_at


def test_new_event_resets_quiet_period() -> None:
    hazards = [_point(offset, float(offset in (0, 30))) for offset in range(-60, 600, 5)]
    result = simulate_trajectory(
        kickoff=datetime(2026, 9, 20, tzinfo=UTC),
        policy=_outdoor_policy(),
        hazards=hazards,
        seed=1,
    )
    assert result.holds
    assert result.holds[0].reset_count == 1
    clear_minutes = (
        result.holds[0].weather_clear_at - result.holds[0].started_at
    ).total_seconds() / 60
    assert clear_minutes == 60


def test_post_hold_weather_bins_keep_their_wall_clock_times() -> None:
    policy = _outdoor_policy().model_copy(
        update={"restart_overhead": RestartOverhead(minutes=[5], weights=[1])}
    )
    hazards = [_point(offset, float(offset in (0, 35))) for offset in range(-60, 600, 5)]
    result = simulate_trajectory(
        kickoff=datetime(2026, 9, 20, tzinfo=UTC),
        policy=policy,
        hazards=hazards,
        seed=1,
    )
    assert len(result.holds) == 2
    assert result.holds[1].started_at == datetime(2026, 9, 20, 0, 35, tzinfo=UTC)


def test_active_delay_has_deterministic_minimum_clearance() -> None:
    now = datetime(2026, 9, 20, 20, tzinfo=UTC)
    forecast = simulate_active_delay(
        now=now,
        last_qualifying_event_at=now,
        policy=_outdoor_policy(),
        future_hazards=[_point(0, 0), _point(180, 0)],
        simulation_count=400,
        seed=9,
    )
    assert forecast["earliest_weather_clear_at"] == now.replace(minute=30)
    assert forecast["weather_clear_cdf"][5]["probability"] == 0
    assert forecast["weather_clear_cdf"][6]["probability"] == 1
    assert forecast["resume_p50_minutes"] >= 35


def test_active_delay_never_predicts_resume_before_refresh_time() -> None:
    now = datetime(2026, 9, 20, 20, tzinfo=UTC)
    policy = _outdoor_policy().model_copy(
        update={"restart_overhead": RestartOverhead(minutes=[5], weights=[1])}
    )
    forecast = simulate_active_delay(
        now=now,
        last_qualifying_event_at=now - timedelta(minutes=45),
        policy=policy,
        future_hazards=[_point(0, 0), _point(180, 0)],
        simulation_count=100,
        seed=11,
    )

    assert forecast["earliest_weather_clear_at"] == now
    assert forecast["resume_cdf"][0]["probability"] == 0
    assert forecast["weather_clear_cdf"][0]["probability"] == 1
    assert forecast["resume_p50_minutes"] == 5


def test_resume_fallback_reports_wider_historical_tail() -> None:
    now = datetime(2026, 9, 20, tzinfo=UTC)
    result = historical_delay_duration_prior(
        now=now,
        durations_minutes=[24, 34, 35, 36, 40, 65, 78, 86, 348],
    )
    assert result["resume_p50"] == now.replace(minute=40)
    assert result["resume_p90"] == now + timedelta(minutes=348)
    assert result["probability_additional_minutes"][60] > 0
    assert "not venue-specific" in result["notes"][0]


def test_correlated_trajectory_is_deterministic_and_estimator_needs_history() -> None:
    probabilities = [0.2] * 200
    left = correlated_events(probabilities, rho=0.6, rng=random.Random(11))
    right = correlated_events(probabilities, rho=0.6, rng=random.Random(11))
    assert left == right
    assert estimate_latent_correlation([left]) is not None
    assert estimate_latent_correlation([[True, False]]) is None


def test_policy_circle_uses_great_circle_distance() -> None:
    center = (35.0, -80.0)
    edge = point_on_ring(*center, 8.0, 90)
    assert distance_miles(*center, *edge) == pytest.approx(8.0, abs=1e-8)
    assert in_policy_circle(*center, *edge, 8.0)
    assert not in_policy_circle(*center, *edge, 7.99)


def test_calibration_and_logistic_helpers() -> None:
    assert probability_per_bin(0.5, 60, 5) == pytest.approx(0.0561256873)
    assert brier_score([0.1, 0.9], [0, 1]) == pytest.approx(0.01)
    bins = reliability_bins([0.1, 0.9], [0, 1], bin_count=2)
    assert len(bins) == 2
    model = fit_logistic([[0], [1], [2], [3]], [0, 0, 1, 1], ["signal"], epochs=300)
    assert model.predict_probability([3]) > model.predict_probability([0])
