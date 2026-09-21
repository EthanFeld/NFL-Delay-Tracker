"""Optional point sampling for GOES-R Geostationary Lightning Mapper flashes.

This module samples the latest complete GLM-L2-LCFA granule from NOAA's public
GOES-18 or GOES-19 S3 bucket. It intentionally does not convert missing data
into a zero-flash reading: absent NetCDF support, stale data, invalid metadata,
or out-of-view venues raise :class:`ProviderError`.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from nfl_delay_tracker.geo import distance_miles
from nfl_delay_tracker.providers.http import ProviderError, get_bytes, get_text

_SATELLITES = {"18": -137.0, "19": -75.2}
_PRODUCT = "GLM-L2-LCFA"
_LIST_BASE = "https://noaa-goes{satellite}.s3.amazonaws.com/"
_OBJECT_BASE = "https://noaa-goes{satellite}.s3.amazonaws.com/"
_FILENAME_RE = re.compile(
    r"(?:^|/)OR_GLM-L2-LCFA_G(?P<satellite>18|19)_"
    r"s(?P<start>\d{14})_e(?P<end>\d{14})_c(?P<created>\d{14})\.nc$"
)
_MAX_NETCDF_BYTES = 20_000_000
_EARTH_RADIUS_KM = 6371.0
_SATELLITE_RADIUS_KM = 42164.0


@dataclass(frozen=True)
class GlmFlashSample:
    """Observed flash count and density in a circular venue neighborhood.

    ``flash_density_per_km2_min`` is the number of GLM-detected flash
    centroids per square kilometer per minute during this granule. It is a
    satellite observation proxy, not a ground-network strike density.
    """

    satellite: str
    latitude: float
    longitude: float
    radius_miles: float
    flash_count: int
    duration_seconds: float
    flash_rate_per_minute: float
    flash_density_per_km2_min: float
    valid_start: datetime
    valid_end: datetime
    source_url: str


@dataclass(frozen=True)
class _Granule:
    satellite: str
    start: datetime
    end: datetime
    key: str


def _open_netcdf(data: bytes) -> Any:
    """Open an in-memory NetCDF-4 file; import the optional package lazily."""
    try:
        import netCDF4
    except ImportError as exc:
        raise ProviderError(
            "GLM NetCDF support is optional; install NFL Delay Tracker with "
            "the 'glm' extra (python -m pip install 'nfl-delay-tracker[glm]')"
        ) from exc
    try:
        return netCDF4.Dataset("glm-granule-in-memory.nc", mode="r", memory=data)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ProviderError(f"GLM NetCDF-4 decode failed: {exc}") from exc


def _utc_from_doy(value: str) -> datetime:
    parsed = datetime.strptime(value[:13], "%Y%j%H%M%S").replace(tzinfo=UTC)
    expected_year = int(value[:4])
    expected_day = int(value[4:7])
    if parsed.year != expected_year or parsed.timetuple().tm_yday != expected_day:
        raise ValueError(f"invalid GLM day-of-year timestamp: {value}")
    return parsed + timedelta(milliseconds=int(value[13]) * 100)


def _parse_granule(key: str) -> _Granule | None:
    match = _FILENAME_RE.search(key)
    if match is None:
        return None
    try:
        start = _utc_from_doy(match.group("start"))
        end = _utc_from_doy(match.group("end"))
        created = _utc_from_doy(match.group("created"))
    except ValueError:
        return None
    if end <= start or created < end or end - start > timedelta(minutes=2):
        return None
    return _Granule(match.group("satellite"), start, end, key)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", maxsplit=1)[-1]


def _keys_from_listing(xml_text: str) -> tuple[list[str], str | None]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ProviderError(f"NOAA GLM S3 listing is malformed: {exc}") from exc
    if _local_name(root.tag) == "Error":
        raise ProviderError("NOAA GLM S3 returned an error listing")
    keys = [
        element.text
        for element in root.iter()
        if _local_name(element.tag) == "Key" and element.text is not None
    ]
    continuation = None
    for element in root.iter():
        if _local_name(element.tag) == "IsTruncated" and element.text == "true":
            for candidate in root.iter():
                if _local_name(candidate.tag) == "NextContinuationToken":
                    continuation = candidate.text
                    break
            if not continuation:
                raise ProviderError("NOAA GLM S3 listing was truncated without a token")
            break
    return keys, continuation


def _iter_coordinate_values(values: Any) -> Iterator[float | None]:
    """Keep coordinate alignment while replacing masked/fill values with None."""
    try:
        iterator = iter(values)
    except TypeError as exc:
        raise ProviderError("GLM flash coordinates are not an array") from exc
    for raw_value in iterator:
        if getattr(raw_value, "mask", False):
            yield None
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError, OverflowError):
            yield None
            continue
        if math.isfinite(value):
            yield value
        else:
            yield None


def _view_zenith_degrees(latitude: float, longitude: float, satellite_longitude: float) -> float:
    """Approximate satellite view zenith using a spherical Earth model."""
    lat = math.radians(latitude)
    delta_lon = math.radians(longitude - satellite_longitude)
    cos_central_angle = math.cos(lat) * math.cos(delta_lon)
    sin_central_angle = math.sqrt(max(0.0, 1.0 - cos_central_angle**2))
    satellite_radius = _SATELLITE_RADIUS_KM
    earth_radius = _EARTH_RADIUS_KM
    toward_satellite = satellite_radius * cos_central_angle - earth_radius
    tangent = satellite_radius * sin_central_angle
    return math.degrees(math.atan2(tangent, toward_satellite))


class GoesGlmProvider:
    """Fetch one recent LCFA granule and count flash centroids near a point.

    The provider uses whichever operational satellite (GOES-18/West or
    GOES-19/East) is nearer in longitude. It rejects points with a modeled
    view zenith above ``max_view_zenith_degrees`` because edge-of-disk quality
    makes a zero count especially unsafe to interpret.
    """

    def __init__(
        self,
        *,
        timeout: int = 25,
        lookback_hours: int = 2,
        max_view_zenith_degrees: float = 80.0,
        max_download_bytes: int = _MAX_NETCDF_BYTES,
        dataset_factory: Callable[[bytes], Any] | None = None,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if lookback_hours < 1 or lookback_hours > 24:
            raise ValueError("lookback_hours must be between 1 and 24")
        if not 0 < max_view_zenith_degrees < 90:
            raise ValueError("max_view_zenith_degrees must be between 0 and 90")
        if max_download_bytes <= 0:
            raise ValueError("max_download_bytes must be positive")
        self.timeout = timeout
        self.lookback_hours = lookback_hours
        self.max_view_zenith_degrees = max_view_zenith_degrees
        self.max_download_bytes = max_download_bytes
        self._dataset_factory = dataset_factory or _open_netcdf
        self._list_cache: dict[tuple[str, str], list[str]] = {}
        self._coordinate_cache: dict[str, tuple[list[float | None], list[float | None]]] = {}

    def sample_point(
        self,
        latitude: float,
        longitude: float,
        *,
        radius_miles: float = 10.0,
        at: datetime | None = None,
    ) -> GlmFlashSample:
        """Count flash centroids inside a radius for the latest complete granule.

        ``at`` defaults to current UTC. It and all returned granule timestamps
        are timezone-aware. Missing coverage or a granule older than the
        configured lookback fails with ``ProviderError``.
        """
        _validate_point(latitude, longitude)
        if not math.isfinite(radius_miles) or radius_miles <= 0:
            raise ValueError("radius_miles must be finite and positive")
        target = at or datetime.now(UTC)
        if target.tzinfo is None or target.utcoffset() is None:
            raise ValueError("at must be timezone-aware")
        target = target.astimezone(UTC)

        satellite = _nearest_satellite(longitude)
        satellite_longitude = _SATELLITES[satellite]
        view_zenith = _view_zenith_degrees(latitude, longitude, satellite_longitude)
        if not math.isfinite(view_zenith) or view_zenith > self.max_view_zenith_degrees:
            raise ProviderError(
                f"Venue is outside reliable GOES-{satellite} GLM view "
                f"({view_zenith:.1f} degree view zenith)"
            )

        granule = self._latest_granule(satellite, target)
        url = _OBJECT_BASE.format(satellite=satellite) + granule.key
        coordinates = self._coordinate_cache.get(url)
        if coordinates is None:
            try:
                data = get_bytes(url, timeout=self.timeout)
            except ProviderError:
                raise
            if not data or len(data) > self.max_download_bytes:
                raise ProviderError(f"NOAA GLM granule has invalid size: {len(data)} bytes")
            coordinates = self._read_flash_coordinates(data)
            self._coordinate_cache[url] = coordinates
            if len(self._coordinate_cache) > 4:
                self._coordinate_cache.pop(next(iter(self._coordinate_cache)))

        count = self._count_near_point(coordinates, latitude, longitude, radius_miles)
        duration = (granule.end - granule.start).total_seconds()
        if duration <= 0:
            raise ProviderError("NOAA GLM granule has a non-positive interval")
        rate = count / (duration / 60.0)
        radius_km = radius_miles * 1.609344
        area_km2 = math.pi * radius_km**2
        return GlmFlashSample(
            satellite=f"GOES-{satellite}",
            latitude=latitude,
            longitude=longitude,
            radius_miles=radius_miles,
            flash_count=count,
            duration_seconds=duration,
            flash_rate_per_minute=rate,
            flash_density_per_km2_min=rate / area_km2,
            valid_start=granule.start,
            valid_end=granule.end,
            source_url=url,
        )

    def _latest_granule(self, satellite: str, target: datetime) -> _Granule:
        newest_allowed = target
        oldest_allowed = target - timedelta(hours=self.lookback_hours)
        first_hour = target.replace(minute=0, second=0, microsecond=0)
        hour_count = self.lookback_hours + 1
        candidates: list[_Granule] = []
        for offset in range(hour_count):
            hour = first_hour - timedelta(hours=offset)
            prefix = f"{_PRODUCT}/{hour:%Y}/{hour:%j}/{hour:%H}/"
            for key in self._list_keys(satellite, prefix):
                granule = _parse_granule(key)
                if (
                    granule is not None
                    and granule.satellite == satellite
                    and granule.end <= newest_allowed
                    and granule.end >= oldest_allowed
                ):
                    candidates.append(granule)
        if not candidates:
            raise ProviderError(
                f"No complete GOES-{satellite} GLM granule within the last "
                f"{self.lookback_hours} hours of {target.isoformat()}"
            )
        return max(candidates, key=lambda item: item.end)

    def _list_keys(self, satellite: str, prefix: str) -> list[str]:
        cache_key = (satellite, prefix)
        if cache_key in self._list_cache:
            return self._list_cache[cache_key]
        keys: list[str] = []
        token: str | None = None
        for _ in range(5):
            query = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if token:
                query["continuation-token"] = token
            url = _LIST_BASE.format(satellite=satellite) + "?" + urlencode(query)
            listing = get_text(url, timeout=self.timeout)
            if len(listing) > 5_000_000:
                raise ProviderError("NOAA GLM S3 listing is unexpectedly large")
            page_keys, token = _keys_from_listing(listing)
            keys.extend(page_keys)
            if token is None:
                self._list_cache[cache_key] = keys
                return keys
        raise ProviderError("NOAA GLM S3 listing exceeded the page limit")

    def _read_flash_coordinates(self, data: bytes) -> tuple[list[float | None], list[float | None]]:
        try:
            dataset = self._dataset_factory(data)
            with dataset as opened:
                variables = opened.variables
                if "flash_lat" not in variables or "flash_lon" not in variables:
                    raise ProviderError("GLM NetCDF is missing flash_lat/flash_lon")
                lat_variable = variables["flash_lat"]
                lon_variable = variables["flash_lon"]
                if len(lat_variable.shape) != 1 or len(lon_variable.shape) != 1:
                    raise ProviderError("GLM flash coordinate arrays are not one-dimensional")
                if lat_variable.shape != lon_variable.shape:
                    raise ProviderError("GLM flash_lat and flash_lon dimensions disagree")
                latitudes = list(_iter_coordinate_values(lat_variable[:]))
                longitudes = list(_iter_coordinate_values(lon_variable[:]))
                if len(latitudes) != len(longitudes):
                    raise ProviderError("GLM flash_lat and flash_lon values disagree")
                return latitudes, longitudes
        except ProviderError:
            raise
        except (
            OSError,
            RuntimeError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            IndexError,
            OverflowError,
        ) as exc:
            raise ProviderError(f"GLM flash data could not be sampled: {exc}") from exc

    @staticmethod
    def _count_near_point(
        coordinates: tuple[list[float | None], list[float | None]],
        latitude: float,
        longitude: float,
        radius_miles: float,
    ) -> int:
        latitudes, longitudes = coordinates
        return sum(
            1
            for flash_lat, flash_lon in zip(latitudes, longitudes, strict=True)
            if flash_lat is not None
            and flash_lon is not None
            and -90 <= flash_lat <= 90
            and -180 <= flash_lon <= 180
            and distance_miles(latitude, longitude, flash_lat, flash_lon) <= radius_miles
        )


def _validate_point(latitude: float, longitude: float) -> None:
    if not math.isfinite(latitude) or not -90 <= latitude <= 90:
        raise ValueError("latitude must be finite and between -90 and 90")
    if not math.isfinite(longitude) or not -180 <= longitude <= 180:
        raise ValueError("longitude must be finite and between -180 and 180")


def _nearest_satellite(longitude: float) -> str:
    return min(
        _SATELLITES,
        key=lambda satellite: abs((longitude - _SATELLITES[satellite] + 180) % 360 - 180),
    )
