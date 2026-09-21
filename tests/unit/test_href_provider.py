from __future__ import annotations

import gzip
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from nfl_delay_tracker.providers import href
from nfl_delay_tracker.providers.http import ProviderError


def test_sample_point_selects_latest_matching_cycle_and_returns_metadata(monkeypatch) -> None:
    requests: list[str] = []
    listing = """
    <a href="spc_post.t00z.hrefct_1hr.f024.grib2">f024</a>
    <a href="spc_post.t12z.hrefct_1hr.f012.grib2.gz">f012</a>
    """
    monkeypatch.setattr(href, "get_text", lambda url, timeout: listing)

    expected_url = (
        "https://nomads.ncep.noaa.gov/pub/data/nccf/com/spc_post/prod/"
        "spc_post.20260920/thunder/spc_post.t12z.hrefct_1hr.f012.grib2.gz"
    )

    def download(url: str, *, timeout: int) -> bytes:
        requests.append(url)
        return gzip.compress(b"GRIB fixture bytes")

    monkeypatch.setattr(href, "get_bytes", download)
    fake_handle = object()
    monkeypatch.setattr(href.eccodes, "codes_new_from_message", lambda data: fake_handle)
    monkeypatch.setattr(href.eccodes, "codes_release", lambda handle: None)
    values = {
        "shortName": "tstm",
        "units": "%",
        "dataDate": 20260920,
        "dataTime": 1200,
        "stepUnits": 1,
        "startStep": 11,
        "endStep": 12,
        "validityDate": 20260921,
        "validityTime": 0,
    }
    monkeypatch.setattr(href.eccodes, "codes_get", lambda handle, key: values[key])
    monkeypatch.setattr(
        href.eccodes,
        "codes_grib_find_nearest",
        lambda handle, latitude, longitude: (
            {
                "lat": 39.93,
                "lon": 284.84,
                "value": 37.5,
                "distance": 4.1,
                "index": 12172,
            },
        ),
    )
    monkeypatch.setattr(
        href.eccodes,
        "codes_new_from_message",
        lambda data: fake_handle if data == b"GRIB fixture bytes" else None,
    )

    provider = href.HrefCtProvider()
    forecast = provider.sample_point(
        39.9,
        -75.16,
        at=datetime(2026, 9, 20, 23, 30, tzinfo=UTC),
    )

    assert forecast.probability == pytest.approx(0.375)
    assert forecast.issued_at == datetime(2026, 9, 20, 12, tzinfo=UTC)
    assert forecast.valid_start == datetime(2026, 9, 20, 23, tzinfo=UTC)
    assert forecast.valid_end == datetime(2026, 9, 21, 0, tzinfo=UTC)
    assert forecast.forecast_hour == 12
    assert forecast.grid_latitude == 39.93
    assert forecast.grid_longitude == pytest.approx(-75.16)
    assert forecast.grid_distance_km == pytest.approx(4.1)
    assert forecast.source_url == expected_url
    assert requests == [expected_url]


def test_sample_point_caches_download_for_second_coordinate(monkeypatch) -> None:
    url = "https://example.test/field.grib2"
    provider = href.HrefCtProvider()
    provider._grib[url] = b"GRIB cached fixture"
    assert provider._download(url) == b"GRIB cached fixture"


def test_sample_points_decodes_one_hour_field_once(monkeypatch) -> None:
    listing = '<a href="spc_post.t12z.hrefct_1hr.f012.grib2">f012</a>'
    monkeypatch.setattr(href, "get_text", lambda *_args, **_kwargs: listing)
    monkeypatch.setattr(href, "get_bytes", lambda *_args, **_kwargs: b"GRIB fixture")
    decode_count = 0
    fake_handle = object()

    def decode(_data: bytes) -> object:
        nonlocal decode_count
        decode_count += 1
        return fake_handle

    monkeypatch.setattr(href.eccodes, "codes_new_from_message", decode)
    monkeypatch.setattr(href.eccodes, "codes_release", lambda _handle: None)
    values = {
        "shortName": "tstm",
        "units": "%",
        "dataDate": 20260920,
        "dataTime": 1200,
        "stepUnits": 1,
        "startStep": 11,
        "endStep": 12,
        "validityDate": 20260921,
        "validityTime": 0,
    }
    monkeypatch.setattr(href.eccodes, "codes_get", lambda _handle, key: values[key])

    def nearest(_handle, latitude: float, longitude: float) -> tuple[dict[str, float], ...]:
        return (
            {
                "lat": latitude,
                "lon": longitude,
                "value": 20,
                "distance": 50 if latitude == 0 else 4,
            },
        )

    monkeypatch.setattr(href.eccodes, "codes_grib_find_nearest", nearest)
    provider = href.HrefCtProvider()

    samples, errors = provider.sample_points(
        {"venue-a": (39.9, -75.1), "venue-b": (40.0, -75.0), "outside": (0.0, 0.0)},
        at=datetime(2026, 9, 20, 23, 30, tzinfo=UTC),
    )

    assert set(samples) == {"venue-a", "venue-b"}
    assert "outside" in errors
    assert decode_count == 1


def test_compressed_grib_is_bounded_and_validated() -> None:
    packed = gzip.compress(b"GRIB valid fixture")
    assert href._unpack_grib(packed, "fixture") == b"GRIB valid fixture"
    with pytest.raises(ProviderError, match="not a GRIB"):
        href._unpack_grib(gzip.compress(b"html"), "fixture")


def test_candidate_selection_requires_available_covering_hour() -> None:
    provider = href.HrefCtProvider(lookback_days=0)
    provider._listings[datetime(2026, 9, 20, tzinfo=UTC).date()] = (
        '<a href="spc_post.t12z.hrefct_1hr.f011.grib2">f011</a>'
    )
    target = datetime(2026, 9, 20, 23, 30, tzinfo=UTC)
    with pytest.raises(ProviderError, match="No HREF CT 1-hour field"):
        provider._find_candidate(target)


def test_missing_probability_and_out_of_domain_fail_closed(monkeypatch) -> None:
    listing = '<a href="spc_post.t12z.hrefct_1hr.f012.grib2">f012</a>'
    monkeypatch.setattr(href, "get_text", lambda url, timeout: listing)
    monkeypatch.setattr(href, "get_bytes", lambda url, timeout: b"GRIB fixture")
    fake_handle = SimpleNamespace()
    monkeypatch.setattr(href.eccodes, "codes_new_from_message", lambda data: fake_handle)
    monkeypatch.setattr(href.eccodes, "codes_release", lambda handle: None)
    values = {
        "shortName": "tstm",
        "units": "%",
        "dataDate": 20260920,
        "dataTime": 1200,
        "stepUnits": 1,
        "startStep": 11,
        "endStep": 12,
        "validityDate": 20260921,
        "validityTime": 0,
    }
    monkeypatch.setattr(href.eccodes, "codes_get", lambda handle, key: values[key])
    monkeypatch.setattr(
        href.eccodes,
        "codes_grib_find_nearest",
        lambda handle, latitude, longitude: (
            {"lat": 0, "lon": 0, "value": 20, "distance": 50, "index": 0},
        ),
    )
    provider = href.HrefCtProvider()
    with pytest.raises(ProviderError, match="outside HREF CT grid coverage"):
        provider.sample_point(
            39.9,
            -75.16,
            at=datetime(2026, 9, 20, 23, 30, tzinfo=UTC),
        )
