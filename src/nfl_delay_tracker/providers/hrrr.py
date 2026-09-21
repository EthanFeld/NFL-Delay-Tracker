"""Optional point sampler for NOAA's public HRRR surface GRIB2 files.

The adapter reads the public NOAA HRRR S3 archive's GRIB index and requests
byte ranges for only five messages at one hourly valid time. It is a numerical
weather-model input, not a calibrated probability of a game delay. HRRR has a
roughly 3-km grid, so nearest-cell point values do not represent stadium sensors
or venue-wide conditions. Products may be delayed or absent; all required
fields, valid times, ranges, and point coverage are checked before a result is
returned. No missing field is silently treated as zero.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import eccodes  # type: ignore[import-untyped]

from nfl_delay_tracker.providers.http import ProviderError, get_text

BASE_URL = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"
_MAX_INDEX_BYTES = 2_000_000
_MAX_MESSAGE_BYTES = 12_000_000
_MAX_GRID_DISTANCE_KM = 12.0
_INDEX_LINE_RE = re.compile(r"^(\d+):(\d+):(.*)$")
_Result = TypeVar("_Result")


@dataclass(frozen=True)
class HrrrPointForecast:
    """Nearest-cell HRRR fields and their model valid/issue times (UTC)."""

    latitude: float
    longitude: float
    grid_latitude: float
    grid_longitude: float
    grid_distance_km: float
    issued_at: datetime
    valid_at: datetime
    forecast_hour: int
    reflectivity_dbz: float
    precipitation_rate_kg_m2_s: float
    cape_j_kg: float
    u_wind_10m_ms: float
    v_wind_10m_ms: float
    wind_speed_10m_ms: float
    source_url: str
    field_source_urls: dict[str, str]


@dataclass(frozen=True)
class _IndexRecord:
    offset: int
    description: str


@dataclass(frozen=True)
class _Candidate:
    issued_at: datetime
    valid_at: datetime
    forecast_hour: int
    source_url: str
    index_url: str
    ranges: dict[str, tuple[int, int]]


_FIELD_QUERIES: dict[str, str] = {
    "reflectivity": ":REFC:entire atmosphere:",
    "precipitation": ":PRATE:surface:",
    "cape": ":CAPE:surface:",
    "u_wind": ":UGRD:10 m above ground:",
    "v_wind": ":VGRD:10 m above ground:",
}


class HrrrPointProvider:
    """Fetch and cache point/hour HRRR reflectivity, rain rate, CAPE, and wind.

    ``sample_point`` returns the latest complete HRRR hourly field valid at or
    before ``at``. Only issue cycles available by ``now`` are considered. It
    searches recent cycles to tolerate late model publication, then downloads
    five index-selected GRIB messages with HTTP Range requests. Cache is scoped
    to this provider instance and repeated points at the same cycle/hour reuse
    the same small message payloads.

    Limitations: this public HRRR file family is hourly and CONUS-only; it does
    not cover Alaska, Hawaii, or territories. Values come from a single nearest
    grid cell. Precipitation is instantaneous rate, not a stadium rain gauge or
    accumulated event total. HRRR fields have no delay-specific calibration.
    """

    def __init__(
        self,
        *,
        timeout: int = 35,
        max_grid_distance_km: float = _MAX_GRID_DISTANCE_KM,
        max_cycle_age_hours: int = 12,
        request_interval_seconds: float = 0.2,
        retries: int = 3,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if max_grid_distance_km <= 0:
            raise ValueError("max_grid_distance_km must be positive")
        if max_cycle_age_hours < 0:
            raise ValueError("max_cycle_age_hours cannot be negative")
        if request_interval_seconds < 0:
            raise ValueError("request_interval_seconds cannot be negative")
        if retries < 1:
            raise ValueError("retries must be positive")
        self.timeout = timeout
        self.max_grid_distance_km = max_grid_distance_km
        self.max_cycle_age_hours = max_cycle_age_hours
        self.request_interval_seconds = request_interval_seconds
        self.retries = retries
        self._last_request_at: float | None = None
        self._indexes: dict[str, str] = {}
        self._messages: dict[tuple[str, int, int], bytes] = {}

    def sample_point(
        self,
        latitude: float,
        longitude: float,
        *,
        at: datetime | None = None,
        now: datetime | None = None,
    ) -> HrrrPointForecast:
        """Sample a point at the most recent complete hourly valid time.

        ``at`` and ``now`` must be timezone-aware when supplied. ``at`` is
        floored to the previous whole UTC hour. ``now`` exists for deterministic
        tests and backfills; a cycle issued after it is never selected.
        """
        _validate_coordinates(latitude, longitude)
        available_as_of = now or datetime.now(UTC)
        target = at or available_as_of
        _require_aware(available_as_of, "now")
        _require_aware(target, "at")
        available_as_of = available_as_of.astimezone(UTC)
        target = target.astimezone(UTC).replace(minute=0, second=0, microsecond=0)

        candidate = self._find_candidate(target, available_as_of)
        values: dict[str, float] = {}
        grid_point: tuple[float, float, float] | None = None
        field_urls: dict[str, str] = {}

        for field_name, byte_range in candidate.ranges.items():
            start, end = byte_range
            message = self._message(candidate.source_url, start, end)
            url = f"{candidate.source_url}#bytes={start}-{end}"
            field_urls[field_name] = url
            handle = self._decode(message, url)
            try:
                missing_value = _validate_field_metadata(
                    handle,
                    field_name,
                    candidate.issued_at,
                    candidate.valid_at,
                )
                nearest = eccodes.codes_grib_find_nearest(handle, latitude, longitude)[0]
                distance = float(nearest["distance"])
                value = float(nearest["value"])
                if not math.isfinite(distance) or distance > self.max_grid_distance_km:
                    raise ProviderError(
                        f"HRRR point is outside the grid footprint ({distance:.1f} km)"
                    )
                if not math.isfinite(value) or value < -9000 or value == missing_value:
                    raise ProviderError(f"HRRR {field_name} has a missing or invalid value")
                values[field_name] = value
                if grid_point is None:
                    grid_point = (
                        float(nearest["lat"]),
                        _normalize_longitude(float(nearest["lon"])),
                        distance,
                    )
                elif distance > self.max_grid_distance_km:
                    raise ProviderError(f"HRRR {field_name} point is outside grid coverage")
            except ProviderError:
                raise
            except (eccodes.CodesInternalError, IndexError, KeyError, TypeError, ValueError) as exc:
                raise ProviderError(f"HRRR {field_name} point sampling failed: {exc}") from exc
            finally:
                eccodes.codes_release(handle)

        if grid_point is None or set(values) != set(_FIELD_QUERIES):
            raise ProviderError("HRRR did not return all required point fields")
        u_wind = values["u_wind"]
        v_wind = values["v_wind"]
        return HrrrPointForecast(
            latitude=latitude,
            longitude=longitude,
            grid_latitude=grid_point[0],
            grid_longitude=grid_point[1],
            grid_distance_km=grid_point[2],
            issued_at=candidate.issued_at,
            valid_at=candidate.valid_at,
            forecast_hour=candidate.forecast_hour,
            reflectivity_dbz=values["reflectivity"],
            precipitation_rate_kg_m2_s=values["precipitation"],
            cape_j_kg=values["cape"],
            u_wind_10m_ms=u_wind,
            v_wind_10m_ms=v_wind,
            wind_speed_10m_ms=math.hypot(u_wind, v_wind),
            source_url=candidate.source_url,
            field_source_urls=field_urls,
        )

    def _find_candidate(self, target: datetime, available_as_of: datetime) -> _Candidate:
        latest_cycle = min(target, available_as_of).replace(minute=0, second=0, microsecond=0)
        errors: list[str] = []
        for age in range(self.max_cycle_age_hours + 1):
            issued_at = latest_cycle - timedelta(hours=age)
            lead = int((target - issued_at).total_seconds() // 3600)
            if lead < 0 or lead > 48:
                continue
            source_url = _grib_url(issued_at, lead)
            index_url = f"{source_url}.idx"
            try:
                index = self._index(index_url)
                ranges = _find_field_ranges(index, source_url)
                return _Candidate(issued_at, target, lead, source_url, index_url, ranges)
            except ProviderError as exc:
                errors.append(f"{issued_at:%Y-%m-%d %HZ}: {exc}")
        detail = "; ".join(errors[-3:])
        suffix = f"; recent cycle errors: {detail}" if detail else ""
        raise ProviderError(
            f"No complete HRRR hourly fields cover {target.isoformat()} "
            f"from a cycle available by {available_as_of.isoformat()}{suffix}"
        )

    def _index(self, url: str) -> str:
        if url not in self._indexes:
            index = self._request_with_retry(lambda: get_text(url, timeout=self.timeout))
            if len(index.encode("utf-8")) > _MAX_INDEX_BYTES:
                raise ProviderError(f"HRRR index is unexpectedly large: {url}")
            self._indexes[url] = index
            if len(self._indexes) > 64:
                self._indexes.pop(next(iter(self._indexes)))
        return self._indexes[url]

    def _message(self, url: str, start: int, end: int) -> bytes:
        cache_key = (url, start, end)
        if cache_key not in self._messages:
            expected = end - start + 1
            if expected <= 0 or expected > _MAX_MESSAGE_BYTES:
                raise ProviderError(f"HRRR selected message has invalid size: {expected} bytes")
            data = self._request_with_retry(
                lambda: _get_range(url, start, end, timeout=self.timeout)
            )
            if len(data) != expected or not data.startswith(b"GRIB"):
                raise ProviderError(
                    f"HRRR byte-range response is not one complete GRIB message: {url}"
                )
            self._messages[cache_key] = data
            if len(self._messages) > 128:
                self._messages.pop(next(iter(self._messages)))
        return self._messages[cache_key]

    def _request_with_retry(self, operation: Callable[[], _Result]) -> _Result:
        last_error: ProviderError | None = None
        for attempt in range(self.retries):
            if self._last_request_at is not None:
                elapsed = time.monotonic() - self._last_request_at
                wait_seconds = self.request_interval_seconds - elapsed
                if wait_seconds > 0:
                    time.sleep(wait_seconds)
            self._last_request_at = time.monotonic()
            try:
                return operation()
            except ProviderError as exc:
                last_error = exc
                if attempt + 1 < self.retries:
                    time.sleep(min(4.0, 0.5 * (2**attempt)))
        assert last_error is not None
        raise last_error

    @staticmethod
    def _decode(message: bytes, url: str) -> Any:
        try:
            handle = eccodes.codes_new_from_message(message)
        except eccodes.CodesInternalError as exc:
            raise ProviderError(f"HRRR GRIB decode failed for {url}: {exc}") from exc
        if handle is None:
            raise ProviderError(f"HRRR GRIB decoder returned no message for {url}")
        return handle


def _validate_field_metadata(
    handle: Any,
    field_name: str,
    issued_at: datetime,
    valid_at: datetime,
) -> float:
    accepted_short_names = {
        "reflectivity": {"refc"},
        "precipitation": {"prate"},
        "cape": {"cape"},
        "u_wind": {"10u", "ugrd"},
        "v_wind": {"10v", "vgrd"},
    }[field_name]
    try:
        short_name = str(eccodes.codes_get(handle, "shortName")).lower()
        data_date = int(eccodes.codes_get(handle, "dataDate"))
        data_time = int(eccodes.codes_get(handle, "dataTime"))
        validity_date = int(eccodes.codes_get(handle, "validityDate"))
        validity_time = int(eccodes.codes_get(handle, "validityTime"))
        encoded_issue = _grib_datetime(data_date, data_time)
        encoded_valid = _grib_datetime(validity_date, validity_time)
        missing_value = float(eccodes.codes_get(handle, "missingValue"))
    except (eccodes.CodesInternalError, TypeError, ValueError) as exc:
        raise ProviderError(f"HRRR {field_name} message lacks valid metadata") from exc
    if short_name not in accepted_short_names:
        raise ProviderError(f"HRRR message is not the requested {field_name} field")
    if encoded_issue != issued_at or encoded_valid != valid_at:
        raise ProviderError(f"HRRR {field_name} message has inconsistent issue/valid timestamps")
    if not math.isfinite(missing_value):
        raise ProviderError(f"HRRR {field_name} has invalid missing-value metadata")
    return missing_value


def _find_field_ranges(index: str, source_url: str) -> dict[str, tuple[int, int]]:
    records: list[_IndexRecord] = []
    for line in index.splitlines():
        match = _INDEX_LINE_RE.match(line.strip())
        if match:
            records.append(_IndexRecord(int(match.group(2)), match.group(3)))
    if not records:
        raise ProviderError(f"HRRR index contains no usable records: {source_url}.idx")

    selected: dict[str, _IndexRecord] = {}
    for field_name, query in _FIELD_QUERIES.items():
        matches = [record for record in records if query in f":{record.description}"]
        if not matches:
            raise ProviderError(f"HRRR index is missing required {field_name} message")
        selected[field_name] = matches[0]

    ranges: dict[str, tuple[int, int]] = {}
    for field_name, record in selected.items():
        following_offsets = [item.offset for item in records if item.offset > record.offset]
        if not following_offsets:
            raise ProviderError(f"HRRR {field_name} message has no end offset in index")
        end = min(following_offsets) - 1
        length = end - record.offset + 1
        if length <= 0 or length > _MAX_MESSAGE_BYTES:
            raise ProviderError(f"HRRR {field_name} index range has invalid size: {length}")
        ranges[field_name] = (record.offset, end)
    return ranges


def _grib_url(issued_at: datetime, forecast_hour: int) -> str:
    return (
        f"{BASE_URL}/hrrr.{issued_at:%Y%m%d}/conus/"
        f"hrrr.t{issued_at:%H}z.wrfsfcf{forecast_hour:02d}.grib2"
    )


def _get_range(url: str, start: int, end: int, *, timeout: int) -> bytes:
    request = Request(
        url,
        headers={
            "Accept": "application/octet-stream,*/*",
            "Accept-Encoding": "identity",
            "Range": f"bytes={start}-{end}",
            "User-Agent": "NFL Delay Tracker/0.1",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            if response.status != 206:
                raise ProviderError(f"HRRR server ignored byte range for {url}")
            return cast(bytes, response.read(_MAX_MESSAGE_BYTES + 1))
    except ProviderError:
        raise
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise ProviderError(f"HRRR range GET {url} failed: {exc}") from exc


def _grib_datetime(date_value: int, time_value: int) -> datetime:
    try:
        return datetime.strptime(f"{date_value:08d}{time_value:04d}", "%Y%m%d%H%M").replace(
            tzinfo=UTC
        )
    except ValueError as exc:
        raise ProviderError("HRRR message has invalid date/time metadata") from exc


def _normalize_longitude(longitude: float) -> float:
    return (longitude + 180) % 360 - 180


def _validate_coordinates(latitude: float, longitude: float) -> None:
    if not math.isfinite(latitude) or not -90 <= latitude <= 90:
        raise ValueError("latitude must be finite and between -90 and 90")
    if not math.isfinite(longitude) or not -180 <= longitude <= 180:
        raise ValueError("longitude must be finite and between -180 and 180")


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
