from __future__ import annotations

from datetime import UTC, datetime

import pytest

from nfl_delay_tracker.providers import hrrr
from nfl_delay_tracker.providers.http import ProviderError


def _index() -> str:
    stamp = "d=2026092023"
    return "\n".join(
        (
            f"1:0:{stamp}:REFC:entire atmosphere:1 hour fcst:",
            f"2:100:{stamp}:PRATE:surface:1 hour fcst:",
            f"3:200:{stamp}:CAPE:surface:1 hour fcst:",
            f"4:300:{stamp}:UGRD:10 m above ground:1 hour fcst:",
            f"5:400:{stamp}:VGRD:10 m above ground:1 hour fcst:",
            f"6:500:{stamp}:TMP:2 m above ground:1 hour fcst:",
        )
    )


def test_find_field_ranges_selects_five_exact_hourly_messages() -> None:
    source_url = "https://example.test/hr.20260920.f01.grib2"
    ranges = hrrr._find_field_ranges(_index(), source_url)

    assert ranges == {
        "reflectivity": (0, 99),
        "precipitation": (100, 199),
        "cape": (200, 299),
        "u_wind": (300, 399),
        "v_wind": (400, 499),
    }


def test_find_field_ranges_fails_closed_when_a_required_field_is_missing() -> None:
    with pytest.raises(ProviderError, match="missing required precipitation"):
        hrrr._find_field_ranges(
            _index().replace("PRATE:surface", "APCP:surface"),
            "https://example.test/file.grib2",
        )


def test_sample_point_fetches_only_indexed_ranges_and_checks_time(monkeypatch) -> None:
    provider = hrrr.HrrrPointProvider(request_interval_seconds=0)
    index_urls: list[str] = []
    range_requests: list[tuple[str, int, int]] = []

    def get_index(url: str, *, timeout: int) -> str:
        index_urls.append(url)
        return _index()

    def get_range(url: str, start: int, end: int, *, timeout: int) -> bytes:
        range_requests.append((url, start, end))
        field = {0: b"refc", 100: b"prate", 200: b"cape", 300: b"10u", 400: b"10v"}[start]
        return (b"GRIB" + field).ljust(end - start + 1, b"\0")

    monkeypatch.setattr(hrrr, "get_text", get_index)
    monkeypatch.setattr(hrrr, "_get_range", get_range)
    monkeypatch.setattr(hrrr.eccodes, "codes_new_from_message", lambda message: message)
    monkeypatch.setattr(hrrr.eccodes, "codes_release", lambda handle: None)
    short_names = {
        b"refc": "refc",
        b"prate": "prate",
        b"cape": "cape",
        b"10u": "10u",
        b"10v": "10v",
    }
    values = {b"refc": 40.0, b"prate": 0.001, b"cape": 500.0, b"10u": 3.0, b"10v": 4.0}

    def codes_get(handle: bytes, key: str) -> object:
        if key == "shortName":
            return short_names[handle[4:].split(b"\0", 1)[0]]
        return {
            "dataDate": 20260920,
            "dataTime": 2300,
            "validityDate": 20260921,
            "validityTime": 0,
            "missingValue": 9999.0,
        }[key]

    def find_nearest(handle: bytes, latitude: float, longitude: float):
        return (
            {
                "lat": 39.9,
                "lon": 284.8,
                "distance": 1.5,
                "value": values[handle[4:].split(b"\0", 1)[0]],
            },
        )

    monkeypatch.setattr(hrrr.eccodes, "codes_get", codes_get)
    monkeypatch.setattr(hrrr.eccodes, "codes_grib_find_nearest", find_nearest)

    forecast = provider.sample_point(
        39.9,
        -75.2,
        at=datetime(2026, 9, 21, 0, 40, tzinfo=UTC),
        now=datetime(2026, 9, 20, 23, 55, tzinfo=UTC),
    )

    assert len(index_urls) == 1
    assert len(range_requests) == 5
    assert all(end - start + 1 == 100 for _, start, end in range_requests)
    assert forecast.issued_at == datetime(2026, 9, 20, 23, tzinfo=UTC)
    assert forecast.valid_at == datetime(2026, 9, 21, 0, tzinfo=UTC)
    assert forecast.forecast_hour == 1
    assert forecast.reflectivity_dbz == 40
    assert forecast.precipitation_rate_kg_m2_s == pytest.approx(0.001)
    assert forecast.cape_j_kg == 500
    assert forecast.wind_speed_10m_ms == pytest.approx(5)
    assert forecast.source_url.endswith("hrrr.t23z.wrfsfcf01.grib2")
    assert forecast.grid_longitude == pytest.approx(-75.2)


def test_candidate_never_uses_cycle_newer_than_now(monkeypatch) -> None:
    provider = hrrr.HrrrPointProvider(request_interval_seconds=0, retries=1)
    requested: list[str] = []

    def get_index(url: str, *, timeout: int) -> str:
        requested.append(url)
        if "t09z" in url:
            return _index()
        raise ProviderError("not available")

    monkeypatch.setattr(hrrr, "get_text", get_index)
    candidate = provider._find_candidate(
        datetime(2026, 9, 21, 10, tzinfo=UTC),
        datetime(2026, 9, 21, 9, 45, tzinfo=UTC),
    )

    assert candidate.issued_at == datetime(2026, 9, 21, 9, tzinfo=UTC)
    assert candidate.forecast_hour == 1
    assert all("t10z" not in url for url in requested)


def test_range_reader_rejects_server_that_ignores_range(monkeypatch) -> None:
    class _Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(hrrr, "urlopen", lambda *_args, **_kwargs: _Response())
    with pytest.raises(ProviderError, match="ignored byte range"):
        hrrr._get_range("https://example.test/file.grib2", 0, 99, timeout=1)


def test_sample_point_rejects_stale_or_missing_values(monkeypatch) -> None:
    provider = hrrr.HrrrPointProvider(request_interval_seconds=0)
    monkeypatch.setattr(hrrr, "get_text", lambda *_args, **_kwargs: _index())
    monkeypatch.setattr(
        hrrr,
        "_get_range",
        lambda _url, start, end, **_kwargs: (b"GRIBrefc").ljust(end - start + 1, b"\0"),
    )
    monkeypatch.setattr(hrrr.eccodes, "codes_new_from_message", lambda message: message)
    monkeypatch.setattr(hrrr.eccodes, "codes_release", lambda _handle: None)
    monkeypatch.setattr(
        hrrr.eccodes,
        "codes_get",
        lambda handle, key: {
            "shortName": "refc",
            "dataDate": 20260920,
            "dataTime": 23,
            "validityDate": 20260921,
            "validityTime": 0,
            "missingValue": 9999.0,
        }[key],
    )
    monkeypatch.setattr(
        hrrr.eccodes,
        "codes_grib_find_nearest",
        lambda *_args: ({"lat": 39.9, "lon": 285, "distance": 1, "value": 40},),
    )

    with pytest.raises(ProviderError, match="inconsistent issue/valid timestamps"):
        provider.sample_point(
            39.9,
            -75,
            at=datetime(2026, 9, 21, 0, tzinfo=UTC),
            now=datetime(2026, 9, 20, 23, 30, tzinfo=UTC),
        )
