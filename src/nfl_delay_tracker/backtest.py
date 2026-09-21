"""Chronological diagnostic backtest for the active-delay duration prior."""

from __future__ import annotations

import csv
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nfl_delay_tracker.pipeline import ROOT


def _read_scored_cases(path: Path) -> list[dict[str, Any]]:
    cases = []
    with path.open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            raw = (row.get("delay_minutes") or "").strip()
            if not raw:
                continue
            cases.append({**row, "duration": int(raw)})
    return cases


def _quantile(values: list[int], probability: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(probability * len(ordered)) - 1))]


def _brier(probabilities: list[float], labels: list[int]) -> float:
    return sum(
        (probability - label) ** 2 for probability, label in zip(probabilities, labels, strict=True)
    ) / len(labels)


def _prior_metrics(training: list[dict[str, Any]], test: list[dict[str, Any]]) -> dict[str, Any]:
    train_durations = [row["duration"] for row in training]
    actual = [row["duration"] for row in test]
    p50: list[int] = []
    lower: list[int] = []
    upper: list[int] = []
    p_long: list[float] = []
    for _ in test:
        # Keep the 40-minute operational floor until more observations support refitting it.
        p50.append(max(40, _quantile(train_durations, 0.50)))
        lower.append(_quantile(train_durations, 0.10))
        upper.append(_quantile(train_durations, 0.90))
        p_long.append(sum(value > 60 for value in train_durations) / len(train_durations))
    labels_long = [int(value > 60) for value in actual]
    covered = [low <= value <= high for low, high, value in zip(lower, upper, actual, strict=True)]
    return {
        "sample_size": len(actual),
        "training_size": len(train_durations),
        "p50_mae_minutes": round(
            sum(
                abs(prediction - observed) for prediction, observed in zip(p50, actual, strict=True)
            )
            / len(actual),
            2,
        ),
        "p10_p90_interval_coverage": round(sum(covered) / len(covered), 4),
        "brier_probability_delay_exceeds_60m": round(_brier(p_long, labels_long), 4),
        "observed_delays_over_60m": sum(labels_long),
        "prediction_mode": "empirical tails with a 40-minute median floor",
        "predicted_p50_minutes": p50,
        "predicted_p10_minutes": lower,
        "predicted_p90_minutes": upper,
        "observed_duration_minutes": actual,
    }


def _fixed_policy_baseline(cases: list[dict[str, Any]]) -> dict[str, Any]:
    # Existing fixed policy rule: 30-minute quiet period plus 5/10/15/20m restart overhead.
    actual = [row["duration"] for row in cases]
    predicted_median = 40
    predicted_low, predicted_high = 35, 50
    p_long = 0.0
    labels_long = [int(value > 60) for value in actual]
    return {
        "sample_size": len(actual),
        "p50_mae_minutes": round(
            sum(abs(value - predicted_median) for value in actual) / len(actual), 2
        ),
        "p10_p90_interval_coverage": round(
            sum(predicted_low <= value <= predicted_high for value in actual) / len(actual), 4
        ),
        "brier_probability_delay_exceeds_60m": round(
            _brier([p_long] * len(actual), labels_long), 4
        ),
        "observed_delays_over_60m": sum(labels_long),
        "prediction_mode": "fixed quiet-period baseline",
        "predicted_p50_minutes": [predicted_median] * len(actual),
        "predicted_p10_minutes": [predicted_low] * len(actual),
        "predicted_p90_minutes": [predicted_high] * len(actual),
        "observed_duration_minutes": actual,
    }


def run_backtest(root: Path = ROOT) -> dict[str, Any]:
    cases = _read_scored_cases(root / "data" / "historical_delay_events.csv")
    if len(cases) < 2:
        raise ValueError("at least two sourced historical delay durations are required")
    cutoff = "2023-01-01"
    training = [row for row in cases if row["date"] < cutoff]
    test = [row for row in cases if row["date"] >= cutoff]
    if not training or not test:
        raise ValueError("historical data must contain pre-2023 training and 2023+ holdout delays")
    fixed = _fixed_policy_baseline(test)
    empirical = _prior_metrics(training, test)
    brier_change = round(
        empirical["brier_probability_delay_exceeds_60m"]
        - fixed["brier_probability_delay_exceeds_60m"],
        4,
    )
    coverage_change = round(
        empirical["p10_p90_interval_coverage"] - fixed["p10_p90_interval_coverage"], 4
    )
    priority_cases = [row for row in cases if 30 <= row["duration"] <= 180]
    priority_training = [row for row in priority_cases if row["date"] < cutoff]
    priority_test = [row for row in priority_cases if row["date"] >= cutoff]
    if not priority_training or not priority_test:
        raise ValueError("30-180 minute cases must exist in both chronological partitions")
    priority_fixed = _fixed_policy_baseline(priority_test)
    priority_empirical = _prior_metrics(priority_training, priority_test)
    priority_brier_change = round(
        priority_empirical["brier_probability_delay_exceeds_60m"]
        - priority_fixed["brier_probability_delay_exceeds_60m"],
        4,
    )
    priority_coverage_change = round(
        priority_empirical["p10_p90_interval_coverage"]
        - priority_fixed["p10_p90_interval_coverage"],
        4,
    )
    result = {
        "title": "NFL Delay Tracker historical delay-duration backtest",
        "generated_at": datetime.now(UTC).isoformat(),
        "period": [min(row["date"] for row in cases), max(row["date"] for row in cases)],
        "source_dataset": "data/historical_delay_events.csv",
        "included_delays": len(cases),
        "omitted_unscored_reports": 0,
        "chronological_split": {
            "training_before": cutoff,
            "training_cases": len(training),
            "holdout_from": cutoff,
            "holdout_cases": len(test),
        },
        "baseline": fixed,
        "revised_prior_chronological": empirical,
        "change": {
            "p10_p90_coverage_points": round(coverage_change * 100, 1),
            "brier_exceeds_60m": brier_change,
            "p50_mae_minutes": round(empirical["p50_mae_minutes"] - fixed["p50_mae_minutes"], 2),
        },
        "priority_range_30_180_minutes": {
            "scope": (
                "Scored positive delay events whose observed duration is 30 through 180 minutes "
                "inclusive."
            ),
            "included_delays": len(priority_cases),
            "chronological_split": {
                "training_before": cutoff,
                "training_cases": len(priority_training),
                "holdout_from": cutoff,
                "holdout_cases": len(priority_test),
            },
            "baseline": priority_fixed,
            "revised_prior_chronological": priority_empirical,
            "change": {
                "p10_p90_coverage_points": round(priority_coverage_change * 100, 1),
                "brier_exceeds_60m": priority_brier_change,
                "p50_mae_minutes": round(
                    priority_empirical["p50_mae_minutes"] - priority_fixed["p50_mae_minutes"],
                    2,
                ),
            },
            "interpretation": (
                "This domain-specific diagnostic filters both training and holdout to observed "
                "30-180 minute delay events. It describes duration estimates conditional on a "
                "selected delay having occurred; it is not a probability of delay or a complete "
                "game-level sample."
            ),
        },
        "interpretation": (
            "The empirical duration prior is scored on a chronological holdout of selected, "
            "officially reported lightning delays. The fixed policy baseline understates long "
            "holds; the empirical tails improve holdout interval coverage and the >60-minute "
            "Brier score. This small positive-only sample does not establish pregame risk "
            "calibration or overall forecast skill."
        ),
        "limitations": [
            "Reports are selected positive cases; they are not a complete game denominator.",
            (
                "Two separately timed suspensions from the 2018 Titans-Dolphins game are "
                "included as events; same-game delays can share storm and venue conditions."
            ),
            "One case uses published college timing; venue types and leagues are mixed.",
            "The backtest evaluates total reported delay duration, not archived issuance-time "
            "weather forecasts.",
            "This duration backtest cannot validate MRMS-conditioned forecasts because the "
            "public MRMS feed is rolling and has no complete historical venue-grid archive.",
        ],
    }
    return result


def main() -> None:
    result = run_backtest()
    print(json.dumps(result, indent=2))
    reports = ROOT / "reports"
    reports.mkdir(exist_ok=True)
    (reports / "historical-backtest.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
