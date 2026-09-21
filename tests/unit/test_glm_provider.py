from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from nfl_delay_tracker.providers import glm
from nfl_delay_tracker.providers.glm import GoesGlmProvider, _parse_granule
from nfl_delay_tracker.providers.http import ProviderError


class _Variable:
    def __init__(self, values: list[float | object]) -> None:
        self.values = values
        self.shape = (len(values),)

    def __getitem__(self, key: slice) -> list[float | object]:
        assert key == slice(None)
        return self.values


class _Dataset:
    def __init__(self, latitude: list[float | object], longitude: list[float | object]) -> None:
        self.variables = {
            "flash_lat": _Variable(latitude),
            "flash_lon": _Variable(longitude),
        }

    def __enter__(self) -> _Dataset:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _listing(*keys: str) -> str:
    contents = "".join(f"<Contents><Key>{key}</Key></Contents>" for key in keys)
    return f"<ListBucketResult>{contents}<IsTruncated>false</IsTruncated></ListBucketResult>"


def test_parse_granule_requires_well_formed_ordered_times() -> None:
    key = (
        "GLM-L2-LCFA/2026/263/20/OR_GLM-L2-LCFA_G19_"
        "s20262632005000_e20262632005200_c20262632005400.nc"
    )
    granule = _parse_granule(key)
    assert granule is not None
    assert granule.satellite == "19"
    assert granule.start == datetime(2026, 9, 20, 20, 5, tzinfo=UTC)
    assert granule.end == datetime(2026, 9, 20, 20, 5, 20, tzinfo=UTC)
    assert _parse_granule(key.replace("s2026263", "s2026367")) is None
    assert _parse_granule(key.replace("e20262632005200", "e20262632005000")) is None


def test_samples_flashes_and_returns_density_and_source(monkeypatch: pytest.MonkeyPatch) -> None:
    key = (
        "GLM-L2-LCFA/2026/263/20/OR_GLM-L2-LCFA_G19_"
        "s20262632005000_e20262632005200_c20262632005400.nc"
    )
    at = datetime(2026, 9, 20, 20, 5, 30, tzinfo=UTC)
    request_urls: list[str] = []

    def listing(url: str, *, timeout: int) -> str:
        del timeout
        request_urls.append(url)
        query = parse_qs(urlsplit(url).query)
        return _listing(key) if query.get("prefix") == ["GLM-L2-LCFA/2026/263/20/"] else _listing()

    monkeypatch.setattr(glm, "get_text", listing)
    monkeypatch.setattr(glm, "get_bytes", lambda url, *, timeout: b"fake-netcdf")
    provider = GoesGlmProvider(
        dataset_factory=lambda _data: _Dataset(
            [40.713, 40.700, 41.2, float("nan")],
            [-74.000, -74.100, -74.006, -74.006],
        )
    )

    sample = provider.sample_point(40.7128, -74.0060, radius_miles=10, at=at)

    assert sample.satellite == "GOES-19"
    assert sample.flash_count == 2
    assert sample.duration_seconds == 20
    assert sample.flash_rate_per_minute == 6
    assert sample.flash_density_per_km2_min > 0
    assert sample.valid_start == datetime(2026, 9, 20, 20, 5, tzinfo=UTC)
    assert sample.valid_end == datetime(2026, 9, 20, 20, 5, 20, tzinfo=UTC)
    assert sample.source_url == f"https://noaa-goes19.s3.amazonaws.com/{key}"
    assert request_urls


def test_empty_flash_set_reports_zero_only_for_valid_granule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = (
        "GLM-L2-LCFA/2026/263/20/OR_GLM-L2-LCFA_G19_"
        "s20262632005000_e20262632005200_c20262632005400.nc"
    )
    monkeypatch.setattr(glm, "get_text", lambda _url, *, timeout: _listing(key))
    monkeypatch.setattr(glm, "get_bytes", lambda _url, *, timeout: b"fake-netcdf")
    provider = GoesGlmProvider(dataset_factory=lambda _data: _Dataset([], []))

    sample = provider.sample_point(
        40.7128,
        -74.0060,
        at=datetime(2026, 9, 20, 20, 5, 30, tzinfo=UTC),
    )

    assert sample.flash_count == 0
    assert sample.flash_density_per_km2_min == 0


def test_no_recent_coverage_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(glm, "get_text", lambda _url, *, timeout: _listing())
    provider = GoesGlmProvider(dataset_factory=lambda _data: _Dataset([], []))

    with pytest.raises(ProviderError, match="No complete GOES-19 GLM granule"):
        provider.sample_point(
            40.7128,
            -74.0060,
            at=datetime(2026, 9, 20, 20, 5, 30, tzinfo=UTC),
        )


def test_missing_coordinate_variable_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    key = (
        "GLM-L2-LCFA/2026/263/20/OR_GLM-L2-LCFA_G19_"
        "s20262632005000_e20262632005200_c20262632005400.nc"
    )

    class MissingCoordinates(_Dataset):
        def __init__(self) -> None:
            self.variables = {}

    monkeypatch.setattr(glm, "get_text", lambda _url, *, timeout: _listing(key))
    monkeypatch.setattr(glm, "get_bytes", lambda _url, *, timeout: b"fake-netcdf")
    provider = GoesGlmProvider(dataset_factory=lambda _data: MissingCoordinates())

    with pytest.raises(ProviderError, match="missing flash_lat/flash_lon"):
        provider.sample_point(
            40.7128,
            -74.0060,
            at=datetime(2026, 9, 20, 20, 5, 30, tzinfo=UTC),
        )


def test_reuses_one_granule_decode_for_multiple_venues(monkeypatch: pytest.MonkeyPatch) -> None:
    key = (
        "GLM-L2-LCFA/2026/263/20/OR_GLM-L2-LCFA_G19_"
        "s20262632005000_e20262632005200_c20262632005400.nc"
    )
    monkeypatch.setattr(glm, "get_text", lambda _url, *, timeout: _listing(key))
    downloads: list[str] = []
    decodes: list[bytes] = []
    monkeypatch.setattr(
        glm,
        "get_bytes",
        lambda url, *, timeout: downloads.append(url) or b"fake-netcdf",
    )
    provider = GoesGlmProvider(
        dataset_factory=lambda data: decodes.append(data) or _Dataset([40.71], [-74.00])
    )
    at = datetime(2026, 9, 20, 20, 5, 30, tzinfo=UTC)

    provider.sample_point(40.7128, -74.0060, at=at)
    provider.sample_point(40.7200, -74.0000, at=at)

    assert len(downloads) == 1
    assert len(decodes) == 1


@pytest.mark.parametrize(
    ("longitude", "expected"),
    [(-74.0, "19"), (-122.0, "18")],
)
def test_nearest_operational_satellite(longitude: float, expected: str) -> None:
    assert glm._nearest_satellite(longitude) == expected
