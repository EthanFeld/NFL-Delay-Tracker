"""Point sampling for NOAA SPC's HREF calibrated thunderstorm probabilities."""

from __future__ import annotations

import gzip
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, TypeVar

import eccodes  # type: ignore[import-untyped]

from nfl_delay_tracker.providers.http import ProviderError, get_bytes, get_text

BASE_URL = "https://nomads.ncep.noaa.gov/pub/data/nccf/com/spc_post/prod"
_FILE_RE = re.compile(
    r'href="spc_post\.t(?P<cycle>\d{2})z\.hrefct_1hr\.f(?P<lead>\d{3})\.grib2(?P<gzip>\.gz)?"'
)
_MAX_GRIB_BYTES = 5_000_000
_MAX_NEAREST_DISTANCE_KM = 35.0
_Result = TypeVar("_Result")


@dataclass(frozen=True)
class HrefCtPointForecast:
    """Calibrated probability at the nearest HREF grid point.

    SPC's HREF CT value is a percent probability for the one-hour forecast
    interval. The roughly 40-km grid represents lightning risk around the
    grid point; it is not a venue's operational lightning-detector reading.
    """

    probability: float
    issued_at: datetime
    valid_start: datetime
    valid_end: datetime
    forecast_hour: int
    latitude: float
    longitude: float
    grid_latitude: float
    grid_longitude: float
    grid_distance_km: float
    source_url: str


@dataclass(frozen=True)
class _Candidate:
    issued_at: datetime
    forecast_hour: int
    url: str


class HrefCtProvider:
    """Fetch the current public HREF CT 1-hour field and sample one point.

    Reuses directory listings and downloaded GRIB bytes across calls, so a
    caller can sample multiple venues against the same model interval without
    repeated network requests.
    """

    def __init__(
        self,
        *,
        timeout: int = 35,
        lookback_days: int = 2,
        max_grid_distance_km: float = _MAX_NEAREST_DISTANCE_KM,
        request_interval_seconds: float = 0.25,
        retries: int = 3,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if lookback_days < 0:
            raise ValueError("lookback_days cannot be negative")
        if max_grid_distance_km <= 0:
            raise ValueError("max_grid_distance_km must be positive")
        if request_interval_seconds < 0:
            raise ValueError("request_interval_seconds cannot be negative")
        if retries < 1:
            raise ValueError("retries must be positive")
        self.timeout = timeout
        self.lookback_days = lookback_days
        self.max_grid_distance_km = max_grid_distance_km
        self.request_interval_seconds = request_interval_seconds
        self.retries = retries
        self._last_request_at: float | None = None
        self._listings: dict[date, str] = {}
        self._grib: dict[str, bytes] = {}

    def sample_point(
        self,
        latitude: float,
        longitude: float,
        *,
        at: datetime | None = None,
    ) -> HrefCtPointForecast:
        """Sample the one-hour HREF CT period containing ``at`` (UTC).

        If ``at`` is omitted, the current UTC time is used. Target timestamps
        must be timezone-aware. Requests outside the model's available 48-hour
        forecast horizon, or outside the grid footprint, raise ``ProviderError``.
        """
        _validate_coordinates(latitude, longitude)
        samples, errors = self.sample_points({"point": (latitude, longitude)}, at=at)
        if "point" in errors:
            raise ProviderError(errors["point"])
        return samples["point"]

    def sample_points(
        self,
        points: dict[str, tuple[float, float]],
        *,
        at: datetime | None = None,
    ) -> tuple[dict[str, HrefCtPointForecast], dict[str, str]]:
        """Sample several points from one HREF interval with one GRIB decode.

        Per-point coverage failures are returned separately so an out-of-domain
        venue does not discard successful samples for other venues.
        """

        if not points:
            return {}, {}
        target = at or datetime.now(UTC)
        if target.tzinfo is None or target.utcoffset() is None:
            raise ValueError("at must be timezone-aware")
        target = target.astimezone(UTC)

        candidate = self._find_candidate(target)
        grib = self._download(candidate.url)
        handle = self._decode(grib, candidate.url)
        try:
            name = _string_key(handle, "shortName")
            units = _string_key(handle, "units")
            if name != "tstm" or units not in {"%", "percent"}:
                raise ProviderError(
                    f"HREF CT field has unexpected parameter metadata: {name!r} {units!r}"
                )
            if int(eccodes.codes_get(handle, "dataDate")) != int(
                candidate.issued_at.strftime("%Y%m%d")
            ):
                raise ProviderError("HREF CT filename and GRIB initialization date disagree")
            # The HREF filename gives the exact six-hour cycle. Avoid dataTime,
            # which includes sub-minute source seconds that ecCodes logs as truncation.
            issued_at = candidate.issued_at
            step_units = int(eccodes.codes_get(handle, "stepUnits"))
            if step_units != 1:
                raise ProviderError(f"HREF CT uses unsupported step units {step_units}")
            start_step = int(eccodes.codes_get(handle, "startStep"))
            end_step = int(eccodes.codes_get(handle, "endStep"))
            if start_step < 0 or end_step - start_step != 1:
                raise ProviderError("HREF CT field is not a one-hour forecast interval")
            if end_step != candidate.forecast_hour:
                raise ProviderError("HREF CT filename and GRIB forecast hour disagree")

            valid_start = issued_at + timedelta(hours=start_step)
            valid_end = issued_at + timedelta(hours=end_step)
            encoded_valid_end = _grib_datetime(handle, "validityDate", "validityTime")
            if encoded_valid_end != valid_end:
                raise ProviderError("HREF CT valid time does not match its forecast steps")
            if not valid_start <= target < valid_end:
                raise ProviderError("Selected HREF CT interval does not cover requested time")

            samples: dict[str, HrefCtPointForecast] = {}
            errors: dict[str, str] = {}
            for point_id, (latitude, longitude) in points.items():
                try:
                    _validate_coordinates(latitude, longitude)
                    nearest = eccodes.codes_grib_find_nearest(handle, latitude, longitude)[0]
                    distance_km = float(nearest["distance"])
                    if not math.isfinite(distance_km) or distance_km > self.max_grid_distance_km:
                        raise ProviderError(
                            "Point is outside HREF CT grid coverage "
                            f"({distance_km:.1f} km to nearest cell)"
                        )
                    probability = float(nearest["value"])
                    if not math.isfinite(probability) or not 0 <= probability <= 100:
                        raise ProviderError("HREF CT returned a missing or invalid probability")
                    samples[point_id] = HrefCtPointForecast(
                        probability=probability / 100.0,
                        issued_at=issued_at,
                        valid_start=valid_start,
                        valid_end=valid_end,
                        forecast_hour=candidate.forecast_hour,
                        latitude=latitude,
                        longitude=longitude,
                        grid_latitude=float(nearest["lat"]),
                        grid_longitude=_normalize_longitude(float(nearest["lon"])),
                        grid_distance_km=distance_km,
                        source_url=candidate.url,
                    )
                except (ProviderError, ValueError) as exc:
                    errors[point_id] = str(exc)
                except (eccodes.CodesInternalError, IndexError, TypeError) as exc:
                    errors[point_id] = f"HREF CT grid sampling failed: {exc}"
            return samples, errors
        finally:
            eccodes.codes_release(handle)

    def _find_candidate(self, target: datetime) -> _Candidate:
        errors: list[str] = []
        for age in range(self.lookback_days + 1):
            data_date = target.date() - timedelta(days=age)
            try:
                listing = self._listing(data_date)
            except ProviderError as exc:
                errors.append(str(exc))
                continue
            candidates: list[_Candidate] = []
            for match in _FILE_RE.finditer(listing):
                cycle_hour = int(match.group("cycle"))
                forecast_hour = int(match.group("lead"))
                if cycle_hour not in {0, 6, 12, 18} or not 1 <= forecast_hour <= 48:
                    continue
                issued_at = datetime(
                    data_date.year,
                    data_date.month,
                    data_date.day,
                    cycle_hour,
                    tzinfo=UTC,
                )
                if issued_at > target:
                    continue
                expected_hour = math.floor((target - issued_at).total_seconds() / 3600) + 1
                if forecast_hour != expected_hour:
                    continue
                suffix = ".gz" if match.group("gzip") else ""
                filename = (
                    f"spc_post.t{cycle_hour:02d}z.hrefct_1hr.f{forecast_hour:03d}.grib2{suffix}"
                )
                url = f"{BASE_URL}/spc_post.{data_date:%Y%m%d}/thunder/{filename}"
                candidates.append(_Candidate(issued_at, forecast_hour, url))
            if candidates:
                return max(candidates, key=lambda candidate: candidate.issued_at)
        detail = f"; index errors: {' | '.join(errors)}" if errors else ""
        raise ProviderError(
            f"No HREF CT 1-hour field covers {target.isoformat()} within "
            f"the last {self.lookback_days + 1} UTC dates{detail}"
        )

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

    def _listing(self, data_date: date) -> str:
        if data_date not in self._listings:
            url = f"{BASE_URL}/spc_post.{data_date:%Y%m%d}/thunder/"
            try:
                listing = self._request_with_retry(lambda: get_text(url, timeout=self.timeout))
            except ProviderError:
                raise
            if len(listing) > 2_000_000:
                raise ProviderError(f"HREF CT index is unexpectedly large: {url}")
            self._listings[data_date] = listing
        return self._listings[data_date]

    def _download(self, url: str) -> bytes:
        if url not in self._grib:
            data = self._request_with_retry(lambda: get_bytes(url, timeout=self.timeout))
            if len(data) > _MAX_GRIB_BYTES:
                raise ProviderError(f"HREF CT download is unexpectedly large: {url}")
            self._grib[url] = _unpack_grib(data, url)
        return self._grib[url]

    @staticmethod
    def _decode(data: bytes, url: str) -> Any:
        try:
            if not data.startswith(b"GRIB"):
                raise ProviderError(f"HREF CT response is not a GRIB message: {url}")
            return eccodes.codes_new_from_message(data)
        except eccodes.CodesInternalError as exc:
            raise ProviderError(f"HREF CT GRIB decode failed for {url}: {exc}") from exc


def _validate_coordinates(latitude: float, longitude: float) -> None:
    if not math.isfinite(latitude) or not -90 <= latitude <= 90:
        raise ValueError("latitude must be finite and between -90 and 90")
    if not math.isfinite(longitude) or not -180 <= longitude <= 180:
        raise ValueError("longitude must be finite and between -180 and 180")


def _normalize_longitude(longitude: float) -> float:
    return (longitude + 180) % 360 - 180


def _unpack_grib(data: bytes, url: str) -> bytes:
    if data.startswith(b"\x1f\x8b"):
        try:
            import io

            with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                data = stream.read(_MAX_GRIB_BYTES + 1)
        except (OSError, EOFError) as exc:
            raise ProviderError(f"HREF CT gzip decompression failed for {url}: {exc}") from exc
    if len(data) > _MAX_GRIB_BYTES:
        raise ProviderError(f"HREF CT GRIB is unexpectedly large: {url}")
    if not data.startswith(b"GRIB"):
        raise ProviderError(f"HREF CT response is not a GRIB message: {url}")
    return data


def _grib_datetime(handle: Any, date_key: str, time_key: str) -> datetime:
    try:
        date_value = int(eccodes.codes_get(handle, date_key))
        time_value = int(eccodes.codes_get(handle, time_key))
        return datetime.strptime(f"{date_value:08d}{time_value:04d}", "%Y%m%d%H%M").replace(
            tzinfo=UTC
        )
    except (eccodes.CodesInternalError, ValueError, TypeError) as exc:
        raise ProviderError(f"HREF CT has invalid {date_key}/{time_key} metadata") from exc


def _string_key(handle: Any, key: str) -> str:
    try:
        return str(eccodes.codes_get(handle, key))
    except (eccodes.CodesInternalError, TypeError) as exc:
        raise ProviderError(f"HREF CT is missing required {key} metadata") from exc
