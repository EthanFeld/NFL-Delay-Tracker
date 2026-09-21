from jsonschema import validate

from nfl_delay_tracker.backtest import run_backtest
from nfl_delay_tracker.models import HazardPoint


def test_resume_backtest_improves_tail_coverage_and_score() -> None:
    result = run_backtest()
    assert result["included_delays"] == 29
    assert result["chronological_split"]["holdout_cases"] == 7
    assert result["baseline"]["p10_p90_interval_coverage"] == 0
    assert result["revised_prior_chronological"]["p10_p90_interval_coverage"] == 0.7143
    assert (
        result["revised_prior_chronological"]["p50_mae_minutes"]
        < result["baseline"]["p50_mae_minutes"]
    )
    assert (
        result["revised_prior_chronological"]["brier_probability_delay_exceeds_60m"]
        < result["baseline"]["brier_probability_delay_exceeds_60m"]
    )


def test_priority_delay_range_has_separate_chronological_metrics() -> None:
    result = run_backtest()["priority_range_30_180_minutes"]
    assert result["included_delays"] == 25
    assert result["chronological_split"]["training_cases"] == 19
    assert result["chronological_split"]["holdout_cases"] == 6
    assert result["revised_prior_chronological"]["p50_mae_minutes"] == 26.5
    assert result["revised_prior_chronological"]["p10_p90_interval_coverage"] == 0.8333
    assert result["revised_prior_chronological"]["brier_probability_delay_exceeds_60m"] == 0.3089


def test_generated_weather_schema_accepts_canonical_probability() -> None:
    schema = HazardPoint.model_json_schema()
    validate(
        {"offset_minutes": 0, "probability": 0.5, "source": "fixture", "valid_at": None}, schema
    )
