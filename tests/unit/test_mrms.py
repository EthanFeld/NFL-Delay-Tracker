import gzip
from datetime import UTC, datetime

from nfl_delay_tracker.models import League, Venue
from nfl_delay_tracker.pipeline import load_registry
from nfl_delay_tracker.providers import mrms
from nfl_delay_tracker.providers.mrms import MrmsSnapshot, _Grid


def test_mrms_download_retries_truncated_compressed_grid(monkeypatch) -> None:
    responses = iter((b"truncated gzip", gzip.compress(b"complete grib")))
    monkeypatch.setattr(mrms, "get_bytes", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(mrms.eccodes, "codes_new_from_message", lambda message: message)
    monkeypatch.setattr(mrms, "sleep", lambda _seconds: None)

    message = mrms._download_grid_message("test", "https://example.test/grid.gz")

    assert message == b"complete grib"


def _grid(values: list[float], venue: Venue) -> _Grid:
    return _Grid(
        values=values,
        nx=3,
        ny=3,
        first_latitude=venue.latitude - 0.01,
        first_longitude=venue.longitude % 360 - 0.01,
        latitude_step=0.01,
        longitude_step=0.01,
        i_scans_negatively=False,
        j_scans_positively=True,
        j_points_are_consecutive=True,
        alternative_row_scanning=False,
        valid_at=datetime(2026, 9, 20, tzinfo=UTC),
        url="https://example.test/grid.grib2.gz",
    )


def test_mrms_missing_cells_are_not_counted_as_zero_lightning() -> None:
    venues, policies = load_registry()
    venue = next(item for item in venues if League.NFL in item.leagues)
    policy = policies[venue.policy_id]
    no_coverage = _grid([-99900.0] * 9, venue)
    snapshot = MrmsSnapshot.__new__(MrmsSnapshot)
    snapshot._grids = {
        "probability_next_30min": no_coverage,
        "probability_next_60min": no_coverage,
        "cg_density_1min": no_coverage,
    }

    features = snapshot.sample(venue, policy)

    assert not features["has_venue_data"]
    assert not any(features["coverage"].values())
    assert features["probability_next_30min"] is None


def test_mrms_zero_values_are_valid_covered_observations() -> None:
    venues, policies = load_registry()
    venue = next(item for item in venues if League.NFL in item.leagues)
    policy = policies[venue.policy_id]
    covered = _grid([0.0] * 9, venue)
    snapshot = MrmsSnapshot.__new__(MrmsSnapshot)
    snapshot._grids = {
        "probability_next_30min": covered,
        "probability_next_60min": covered,
        "cg_density_1min": covered,
    }

    features = snapshot.sample(venue, policy)

    assert features["has_venue_data"]
    assert all(features["coverage"].values())
    assert features["probability_next_30min"] == 0.0
    assert features["last_qualifying_event_at"] is None


def test_mrms_reflectivity_echoes_form_local_connected_objects() -> None:
    venues, _ = load_registry()
    venue = next(item for item in venues if League.NFL in item.leagues)
    grid = _grid([0.0, 0.0, 0.0, 0.0, 40.0, 42.0, 0.0, 0.0, 0.0], venue)

    objects = grid.storm_echoes(venue.latitude, venue.longitude, radius_miles=60.0)

    assert len(objects) == 1
    assert objects[0]["cells"] == 2
    assert objects[0]["max_reflectivity_dbz"] == 42.0
    assert objects[0]["effective_radius_miles"] > 0
