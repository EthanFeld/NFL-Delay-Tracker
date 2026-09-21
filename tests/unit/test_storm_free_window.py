from __future__ import annotations

from nfl_delay_tracker.models import HazardPoint
from nfl_delay_tracker.pipeline import (
    _forecast_confirms_clear_window,
    _hazard_window_state,
)


def _hazards(probabilities: list[float]) -> list[HazardPoint]:
    return [
        HazardPoint(
            offset_minutes=index * 5,
            probability=probability,
            source="test",
        )
        for index, probability in enumerate(probabilities)
    ]


def _confirmed_clear(**overrides: object) -> bool:
    arguments: dict[str, object] = {
        "start_offset": 0,
        "end_offset": 20,
        "forecast_hazards": _hazards([0, 0, 0, 0]),
        "nws_hazards": _hazards([0, 0, 0, 0]),
        "nws_fresh": True,
        "href_hazards": [],
        "href_fresh": False,
        "window_is_current": False,
        "mrms_features": None,
        "alert_features": {},
        "storm_motion": {},
        "delay_active": False,
    }
    arguments.update(overrides)
    return _forecast_confirms_clear_window(**arguments)  # type: ignore[arg-type]


def test_complete_zero_forecast_confirms_zero_risk_window() -> None:
    assert _hazard_window_state(_hazards([0, 0, 0, 0]), start_offset=0, end_offset=20) == "clear"
    assert _confirmed_clear()


def test_missing_time_bins_do_not_count_as_clear() -> None:
    partial = [_hazards([0])[0], HazardPoint(offset_minutes=10, probability=0, source="test")]
    assert _hazard_window_state(partial, start_offset=0, end_offset=20) == "incomplete"
    assert not _confirmed_clear(forecast_hazards=partial, nws_hazards=partial)


def test_any_positive_forecast_bin_prevents_hard_zero() -> None:
    positive = _hazards([0, 0.01, 0, 0])
    assert _hazard_window_state(positive, start_offset=0, end_offset=20) == "storm"
    assert not _confirmed_clear(forecast_hazards=positive, nws_hazards=positive)
    assert not _confirmed_clear(nws_hazards=positive)
    assert not _confirmed_clear(nws_fresh=False, href_fresh=False)
    assert not _confirmed_clear(
        storm_motion={"tracks": [{"status": "approaching"}]}
    )
    assert not _confirmed_clear(delay_active=True)


def test_current_clear_requires_fresh_zero_local_observations() -> None:
    mrms = {
        "coverage": {
            "probability_next_30min": True,
            "probability_next_60min": True,
            "cg_density_1min": True,
        },
        "probability_next_30min": 0.0,
        "probability_next_60min": 0.0,
        "cg_density_per_km2_min": {"fraction_positive": 0.0, "max": 0.0},
    }
    alerts = {"status": "ok", "has_active_warning": False}
    assert _confirmed_clear(
        window_is_current=True,
        mrms_features=mrms,
        alert_features=alerts,
    )
    assert not _confirmed_clear(window_is_current=True, alert_features=alerts)
    assert not _confirmed_clear(
        window_is_current=True,
        mrms_features=mrms,
        alert_features={"status": "ok", "has_active_warning": True},
    )
