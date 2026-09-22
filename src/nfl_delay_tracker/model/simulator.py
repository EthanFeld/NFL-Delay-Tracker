"""Venue-policy Monte Carlo simulator for pregames and active delays."""

from __future__ import annotations

import bisect
import csv
import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from statistics import NormalDist
from typing import Any

from nfl_delay_tracker.model.trajectories import correlated_events
from nfl_delay_tracker.models import HazardPoint, WeatherPolicy

_NORMAL = NormalDist()
STEP_MINUTES = 5
DEFAULT_GAME_DURATION_MINUTES = 210


@dataclass(frozen=True)
class SimulatedHold:
    started_at: datetime
    weather_clear_at: datetime
    football_resume_at: datetime
    phase: str
    reset_count: int


@dataclass(frozen=True)
class TrajectoryResult:
    holds: tuple[SimulatedHold, ...]
    game_end_at: datetime
    kickoff_delayed: bool
    total_delay_minutes: float


def _sample_weighted(values: list[int], weights: list[float], rng: random.Random) -> int:
    threshold = rng.random() * sum(weights)
    total = 0.0
    for value, weight in zip(values, weights, strict=True):
        total += weight
        if threshold <= total:
            return value
    return values[-1]


def _interpolate_hazard(points: list[HazardPoint], minute: int) -> float:
    if not points:
        return 0.0
    offsets = [point.offset_minutes for point in points]
    position = bisect.bisect_left(offsets, minute)
    if position == 0:
        return points[0].probability if minute == points[0].offset_minutes else 0.0
    if position == len(points):
        return 0.0
    before = points[position - 1]
    after = points[position]
    if after.offset_minutes == before.offset_minutes:
        return after.probability
    fraction = (minute - before.offset_minutes) / (after.offset_minutes - before.offset_minutes)
    return before.probability + fraction * (after.probability - before.probability)


def _step_probabilities(hazards: list[HazardPoint], offsets: list[int]) -> list[float]:
    return [_interpolate_hazard(hazards, offset) for offset in offsets]


def _has_positive_hazard_in_window(
    hazards: list[HazardPoint], *, start_minute: int, duration_minutes: int
) -> bool:
    """Whether any sampled five-minute bin in the exposure window has storm risk."""
    if duration_minutes <= 0:
        return False
    sorted_hazards = sorted(hazards, key=lambda item: item.offset_minutes)
    return any(
        _interpolate_hazard(sorted_hazards, minute) > 0
        for minute in range(start_minute, start_minute + duration_minutes, STEP_MINUTES)
    )


def _zero_delay_result(simulation_count: int) -> dict[str, Any]:
    return {
        "delay_probability": 0.0,
        "kickoff_delay_probability": 0.0,
        "in_game_delay_probability": 0.0,
        "multiple_delay_probability": 0.0,
        "expected_delay_minutes": 0.0,
        "simulation_count": simulation_count,
    }


def simulate_trajectory(
    *,
    kickoff: datetime,
    policy: WeatherPolicy,
    hazards: list[HazardPoint],
    game_duration_minutes: int = DEFAULT_GAME_DURATION_MINUTES,
    rho: float = 0.0,
    seed: int | None = None,
    start_offset_minutes: int | None = None,
    start_in_game: bool = False,
) -> TrajectoryResult:
    """Run one correlated event path through warmups, game and shifted finish.

    ``start_offset_minutes`` and ``start_in_game`` anchor a live-game path at
    its current clock instead of simulating warmups and completed play again.
    """
    if kickoff.tzinfo is None or kickoff.utcoffset() is None:
        raise ValueError("kickoff must be timezone-aware")
    if start_in_game and start_offset_minutes is None:
        raise ValueError("a live-game path needs its current offset from kickoff")
    if start_offset_minutes is not None and not start_in_game:
        raise ValueError("a current kickoff offset can only be used for a live-game path")
    if start_in_game and start_offset_minutes is not None and start_offset_minutes < 0:
        raise ValueError("a live-game path cannot start before kickoff")
    rng = random.Random(seed)
    sorted_hazards = sorted(hazards, key=lambda item: item.offset_minutes)
    warmup = policy.warmup_exposure_minutes
    # Six additional hours permit repeated storm arrivals and a shifted finish.
    warmup_before_game = warmup if start_offset_minutes is None else 0
    max_steps = (warmup_before_game + game_duration_minutes + 360) // STEP_MINUTES
    first_minute = -warmup if start_offset_minutes is None else start_offset_minutes
    offsets = [first_minute + index * STEP_MINUTES for index in range(max_steps)]
    probabilities = _step_probabilities(sorted_hazards, offsets)
    events = correlated_events(probabilities, rho=rho, rng=rng)

    holds: list[SimulatedHold] = []
    playing_minutes = 0
    minute = first_minute
    in_hold = False
    hold_started = 0
    last_strike = 0
    clear_at: int | None = None
    resume_at: int | None = None
    reset_count = 0
    phase = "in_game" if start_in_game else "pregame"
    kickoff_delayed = False

    for event in events:
        if not in_hold:
            if event:
                in_hold = True
                hold_started = minute
                last_strike = minute
                reset_count = 0
                phase = (
                    "pregame"
                    if not start_in_game and playing_minutes == 0 and minute <= 0
                    else "in_game"
                )
                clear_at = minute + policy.quiet_period_minutes
                restart = _sample_weighted(
                    policy.restart_overhead.minutes, policy.restart_overhead.weights, rng
                )
                resume_at = clear_at + restart
            else:
                if minute >= 0:
                    phase = "in_game"
                    playing_minutes += STEP_MINUTES
        else:
            if event:
                last_strike = minute
                reset_count += 1
                clear_at = minute + policy.quiet_period_minutes
                restart = _sample_weighted(
                    policy.restart_overhead.minutes, policy.restart_overhead.weights, rng
                )
                resume_at = clear_at + restart
            elif resume_at is not None and minute + STEP_MINUTES >= resume_at:
                clear_minute = clear_at if clear_at is not None else last_strike
                resume_minute = resume_at
                holds.append(
                    SimulatedHold(
                        started_at=kickoff + timedelta(minutes=hold_started),
                        weather_clear_at=kickoff + timedelta(minutes=clear_minute),
                        football_resume_at=kickoff + timedelta(minutes=resume_minute),
                        phase=phase,
                        reset_count=reset_count,
                    )
                )
                kickoff_delayed = kickoff_delayed or phase == "pregame"
                in_hold = False
                if minute >= 0:
                    phase = "in_game"
                # Keep the fixed wall-clock bin cursor aligned with its sampled
                # weather event. A delayed game extends into later bins naturally.
        minute += STEP_MINUTES
        if playing_minutes >= game_duration_minutes:
            break

    if in_hold:
        final_minute = minute
        clear_minute = clear_at if clear_at is not None else last_strike
        resume_minute = resume_at if resume_at is not None else clear_minute
        holds.append(
            SimulatedHold(
                started_at=kickoff + timedelta(minutes=hold_started),
                weather_clear_at=kickoff + timedelta(minutes=clear_minute),
                football_resume_at=kickoff + timedelta(minutes=max(resume_minute, final_minute)),
                phase=phase,
                reset_count=reset_count,
            )
        )
        kickoff_delayed = kickoff_delayed or phase == "pregame"

    end_at = kickoff + timedelta(minutes=minute)
    total_delay = sum(
        (hold.football_resume_at - hold.started_at).total_seconds() / 60 for hold in holds
    )
    return TrajectoryResult(tuple(holds), end_at, kickoff_delayed, total_delay)


def simulate_pregame(
    *,
    kickoff: datetime,
    policy: WeatherPolicy,
    hazards: list[HazardPoint],
    simulation_count: int = 20_000,
    game_duration_minutes: int = DEFAULT_GAME_DURATION_MINUTES,
    rho: float = 0.0,
    seed: int = 17,
) -> dict[str, Any]:
    """Estimate delay probabilities and duration from correlated trajectories."""
    if simulation_count <= 0:
        raise ValueError("simulation_count must be positive")
    if not _has_positive_hazard_in_window(
        hazards,
        start_minute=-policy.warmup_exposure_minutes,
        duration_minutes=policy.warmup_exposure_minutes + game_duration_minutes,
    ):
        return _zero_delay_result(simulation_count)
    any_delay = kickoff_delay = in_game_delay = multiple = 0
    total_minutes = 0.0
    for index in range(simulation_count):
        result = simulate_trajectory(
            kickoff=kickoff,
            policy=policy,
            hazards=hazards,
            game_duration_minutes=game_duration_minutes,
            rho=rho,
            seed=seed + index,
        )
        count = len(result.holds)
        any_delay += count > 0
        kickoff_delay += result.kickoff_delayed
        in_game_delay += any(hold.phase == "in_game" for hold in result.holds)
        multiple += count > 1
        total_minutes += result.total_delay_minutes
    return {
        "delay_probability": any_delay / simulation_count,
        "kickoff_delay_probability": kickoff_delay / simulation_count,
        "in_game_delay_probability": in_game_delay / simulation_count,
        "multiple_delay_probability": multiple / simulation_count,
        "expected_delay_minutes": total_minutes / simulation_count,
        "simulation_count": simulation_count,
    }


def simulate_remaining_game(
    *,
    now: datetime,
    kickoff: datetime,
    policy: WeatherPolicy,
    hazards: list[HazardPoint],
    remaining_game_minutes: int,
    simulation_count: int = 20_000,
    rho: float = 0.0,
    seed: int = 2026,
) -> dict[str, Any]:
    """Estimate new delay risk from now through the remaining game horizon."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if kickoff.tzinfo is None or kickoff.utcoffset() is None:
        raise ValueError("kickoff must be timezone-aware")
    if now < kickoff:
        raise ValueError("a remaining-game forecast cannot start before kickoff")
    if remaining_game_minutes < 0:
        raise ValueError("remaining_game_minutes cannot be negative")
    if simulation_count <= 0:
        raise ValueError("simulation_count must be positive")
    if remaining_game_minutes == 0:
        return _zero_delay_result(simulation_count)

    elapsed_minutes = int((now - kickoff).total_seconds() // 60)
    # Start at the next five-minute bin. The current bin is already partly
    # elapsed at refresh time and is excluded by the pipeline risk window.
    start_offset = ((elapsed_minutes // STEP_MINUTES) + 1) * STEP_MINUTES
    if not _has_positive_hazard_in_window(
        hazards,
        start_minute=start_offset,
        duration_minutes=remaining_game_minutes,
    ):
        return _zero_delay_result(simulation_count)
    any_delay = multiple = 0
    total_minutes = 0.0
    for index in range(simulation_count):
        result = simulate_trajectory(
            kickoff=kickoff,
            policy=policy,
            hazards=hazards,
            game_duration_minutes=remaining_game_minutes,
            rho=rho,
            seed=seed + index,
            start_offset_minutes=start_offset,
            start_in_game=True,
        )
        count = len(result.holds)
        any_delay += count > 0
        multiple += count > 1
        total_minutes += result.total_delay_minutes
    return {
        "delay_probability": any_delay / simulation_count,
        "kickoff_delay_probability": 0.0,
        "in_game_delay_probability": any_delay / simulation_count,
        "multiple_delay_probability": multiple / simulation_count,
        "expected_delay_minutes": total_minutes / simulation_count,
        "simulation_count": simulation_count,
    }


def simulate_active_delay(
    *,
    now: datetime,
    last_qualifying_event_at: datetime,
    policy: WeatherPolicy,
    future_hazards: list[HazardPoint],
    simulation_count: int = 20_000,
    rho: float = 0.0,
    seed: int = 23,
    horizon_minutes: int = 180,
) -> dict[str, Any]:
    """Return resume CDF conditional on a currently active weather hold."""
    if now.tzinfo is None or last_qualifying_event_at.tzinfo is None:
        raise ValueError("active-delay times must be timezone-aware")
    if simulation_count <= 0:
        raise ValueError("simulation_count must be positive")
    earliest_clear = max(
        now,
        last_qualifying_event_at + timedelta(minutes=policy.quiet_period_minutes),
    )
    offsets = list(range(0, horizon_minutes + STEP_MINUTES, STEP_MINUTES))
    probabilities = _step_probabilities(future_hazards, offsets)
    resume_minutes: list[int] = []
    clear_minutes: list[int] = []
    for sim_index in range(simulation_count):
        events = correlated_events(probabilities, rho=rho, rng=random.Random(seed + sim_index))
        latest_strike = int((last_qualifying_event_at - now).total_seconds() // 60)
        clear_minute = max(0, latest_strike + policy.quiet_period_minutes)
        overhead: int | None = None
        resume_minute = clear_minute
        resumed = False
        rng = random.Random(seed + 1_000_000 + sim_index)
        for offset, event in zip(offsets, events, strict=True):
            current_minute = max(offset, latest_strike)
            if event:
                latest_strike = current_minute
                clear_minute = latest_strike + policy.quiet_period_minutes
                overhead = _sample_weighted(
                    policy.restart_overhead.minutes, policy.restart_overhead.weights, rng
                )
                resume_minute = clear_minute + overhead
            else:
                if current_minute >= clear_minute:
                    if overhead is None:
                        overhead = _sample_weighted(
                            policy.restart_overhead.minutes, policy.restart_overhead.weights, rng
                        )
                        resume_minute = clear_minute + overhead
                    if current_minute >= resume_minute:
                        resumed = True
                        break
        resume_minutes.append(resume_minute if resumed else horizon_minutes + STEP_MINUTES)
        clear_minutes.append(
            clear_minute if clear_minute <= horizon_minutes else horizon_minutes + STEP_MINUTES
        )

    resume_minutes.sort()
    clear_minutes.sort()
    clear_distribution = [
        {
            "minutes_from_now": point,
            "probability": sum(value <= point for value in clear_minutes) / simulation_count,
        }
        for point in range(0, horizon_minutes + STEP_MINUTES, STEP_MINUTES)
    ]
    distribution = [
        {
            "minutes_from_now": point,
            "probability": sum(value <= point for value in resume_minutes) / simulation_count,
        }
        for point in range(0, horizon_minutes + STEP_MINUTES, STEP_MINUTES)
    ]

    def quantile(probability: float) -> int | None:
        index = min(simulation_count - 1, max(0, int(probability * (simulation_count - 1))))
        value = resume_minutes[index]
        return value if value <= horizon_minutes else None

    return {
        "earliest_weather_clear_at": earliest_clear,
        "weather_clear_cdf": clear_distribution,
        "resume_cdf": distribution,
        "resume_p50_minutes": quantile(0.50),
        "resume_p75_minutes": quantile(0.75),
        "resume_p90_minutes": quantile(0.90),
        "probability_additional_minutes": {
            delay: sum(value > delay for value in resume_minutes) / simulation_count
            for delay in (15, 30, 45, 60, 90, 120, 150, 180)
        },
        "simulation_count": simulation_count,
    }


def historical_delay_duration_prior(
    *,
    now: datetime,
    durations_minutes: list[int],
    delay_started_at: datetime | None = None,
) -> dict[str, Any]:
    """Build broad fallback CDF from documented past game delays.

    Used only when no recent qualifying-flash observation exists. It is a
    selected-event prior, not a venue-specific live weather forecast.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    if not durations_minutes or any(duration <= 0 for duration in durations_minutes):
        raise ValueError("historical durations must contain positive values")
    elapsed = 0
    candidates = sorted(durations_minutes)
    if delay_started_at is not None:
        if delay_started_at.tzinfo is None or delay_started_at.utcoffset() is None:
            raise ValueError("delay_started_at must be timezone-aware")
        elapsed = max(0, int((now - delay_started_at).total_seconds() // 60))
        remaining = [duration - elapsed for duration in candidates if duration > elapsed]
        # If current delay already exceeds every example, retain a broad tail
        # rather than fabricate certainty that play is about to resume.
        candidates = remaining or [30, 60, 90, 180]
    else:
        candidates = list(candidates)

    count = len(candidates)

    def quantile(probability: float) -> int:
        index = min(count - 1, max(0, math.ceil(probability * count) - 1))
        return candidates[index]

    cdf = [
        {
            "at": (now + timedelta(minutes=minute)).isoformat(),
            "probability": sum(value <= minute for value in candidates) / count,
        }
        for minute in range(0, max(180, ((candidates[-1] + 14) // 15) * 15) + 1, 15)
    ]
    return {
        "resume_cdf": cdf,
        "resume_p50": now + timedelta(minutes=quantile(0.50)),
        "resume_p75": now + timedelta(minutes=quantile(0.75)),
        "resume_p90": now + timedelta(minutes=quantile(0.90)),
        "probability_additional_minutes": {
            delay: sum(value > delay for value in candidates) / count
            for delay in (15, 30, 45, 60, 90, 120, 150, 180)
        },
        "source": "Selected historical delay-duration prior",
        "notes": [
            f"Empirical fallback from {count} reported delay durations; not venue-specific "
            "or weather-conditioned.",
            (
                "Delay start time unavailable; chart projects total duration from this refresh "
                "and may overstate remaining time."
                if delay_started_at is None
                else "Elapsed delay conditioned on reported start; last qualifying-lightning "
                "timestamp remains unknown."
            ),
            "Official venue/team update takes precedence. This is not a safety decision.",
        ],
    }


def load_historical_delay_durations(csv_path: Path) -> list[int]:
    """Read scored positive durations from the curated provenance CSV."""
    durations: list[int] = []
    with csv_path.open(encoding="utf-8", newline="") as source:
        for row in csv.DictReader(source):
            raw_duration = (row.get("delay_minutes") or "").strip()
            if raw_duration:
                durations.append(int(raw_duration))
    return durations
