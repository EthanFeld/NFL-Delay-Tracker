"""Public MRMS lightning fields sampled to venue trigger radii."""

from __future__ import annotations

import gzip
import math
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from http.client import HTTPException
from time import sleep
from typing import Any

import eccodes  # type: ignore[import-untyped]

from nfl_delay_tracker.geo import distance_miles
from nfl_delay_tracker.models import Venue, WeatherPolicy
from nfl_delay_tracker.providers.http import ProviderError, get_bytes

BASE_URL = "https://mrms.ncep.noaa.gov/2D"
_PRODUCTS = {
    "probability_next_30min": "LightningProbabilityNext30min",
    "probability_next_60min": "LightningProbabilityNext60min",
    "cg_density_1min": "NLDN_CG_001min_AvgDensity",
}
_DOWNLOAD_ATTEMPTS = 3


def _download_grid_message(product: str, url: str) -> Any:
    last_error: Exception | None = None
    for attempt in range(_DOWNLOAD_ATTEMPTS):
        try:
            compressed = get_bytes(url, timeout=35)
            message = gzip.decompress(compressed)
            return eccodes.codes_new_from_message(message)
        except (
            ProviderError,
            OSError,
            EOFError,
            zlib.error,
            HTTPException,
            eccodes.CodesInternalError,
        ) as exc:
            last_error = exc
            if attempt + 1 < _DOWNLOAD_ATTEMPTS:
                sleep(0.5 * (2**attempt))
    raise ProviderError(
        f"MRMS {product} download/decode failed after {_DOWNLOAD_ATTEMPTS} attempts: {last_error}"
    ) from last_error


@dataclass
class _Grid:
    values: Any
    nx: int
    ny: int
    first_latitude: float
    first_longitude: float
    latitude_step: float
    longitude_step: float
    i_scans_negatively: bool
    j_scans_positively: bool
    j_points_are_consecutive: bool
    alternative_row_scanning: bool
    valid_at: datetime
    url: str

    def _linear_index(self, i: int, j: int) -> int:
        if self.alternative_row_scanning:
            if self.j_points_are_consecutive and i % 2:
                j = self.ny - 1 - j
            elif not self.j_points_are_consecutive and j % 2:
                i = self.nx - 1 - i
        return i * self.ny + j if self.j_points_are_consecutive else j * self.nx + i

    @staticmethod
    def _index_range(center: int, radius: int, limit: int) -> range:
        lower = max(0, center - radius)
        upper = min(limit - 1, center + radius)
        return range(lower, upper + 1)

    def _cells(
        self, latitude: float, longitude: float, radius_miles: float
    ) -> Iterator[tuple[float, float]]:
        latitude_step = abs(self.latitude_step)
        longitude_step = abs(self.longitude_step)
        signed_latitude_step = latitude_step if self.j_scans_positively else -latitude_step
        signed_longitude_step = -longitude_step if self.i_scans_negatively else longitude_step
        longitude = longitude % 360.0
        row_center = round((latitude - self.first_latitude) / signed_latitude_step)
        col_center = round((longitude - self.first_longitude) / signed_longitude_step)
        lat_margin = radius_miles / 68.9 + latitude_step
        cos_latitude = max(0.15, math.cos(math.radians(latitude)))
        lon_margin = radius_miles / (69.17 * cos_latitude) + longitude_step
        row_radius = math.ceil(lat_margin / latitude_step)
        col_radius = math.ceil(lon_margin / longitude_step)
        rows = self._index_range(row_center, row_radius, self.ny)
        cols = self._index_range(col_center, col_radius, self.nx)
        for j in rows:
            cell_latitude = self.first_latitude + j * signed_latitude_step
            for i in cols:
                cell_longitude = self.first_longitude + i * signed_longitude_step
                distance = distance_miles(
                    latitude, longitude, cell_latitude, cell_longitude % 360.0
                )
                if distance <= radius_miles:
                    yield float(self.values[self._linear_index(i, j)]), distance

    def summarize(
        self, latitude: float, longitude: float, inner: float, outer: float
    ) -> dict[str, float | int | None]:
        values = [
            value
            for value, distance in self._cells(latitude, longitude, outer)
            if inner <= distance <= outer and math.isfinite(value) and value >= 0
        ]
        if not values:
            return {"max": None, "mean": None, "p90": None, "fraction_positive": None, "cells": 0}
        ordered = sorted(values)
        p90 = ordered[min(len(ordered) - 1, math.ceil(0.9 * len(ordered)) - 1)]
        return {
            "max": max(values),
            "mean": sum(values) / len(values),
            "p90": p90,
            "fraction_positive": sum(value > 0 for value in values) / len(values),
            "cells": len(values),
        }

    def storm_echoes(
        self,
        latitude: float,
        longitude: float,
        *,
        radius_miles: float = 60.0,
        threshold_dbz: float = 35.0,
    ) -> list[dict[str, float | int]]:
        """Extract local connected reflectivity objects for successive-scan tracking."""
        latitude_step = abs(self.latitude_step)
        longitude_step = abs(self.longitude_step)
        signed_latitude_step = latitude_step if self.j_scans_positively else -latitude_step
        signed_longitude_step = -longitude_step if self.i_scans_negatively else longitude_step
        longitude = longitude % 360.0
        row_center = round((latitude - self.first_latitude) / signed_latitude_step)
        col_center = round((longitude - self.first_longitude) / signed_longitude_step)
        lat_margin = radius_miles / 68.9 + latitude_step
        cos_latitude = max(0.15, math.cos(math.radians(latitude)))
        lon_margin = radius_miles / (69.17 * cos_latitude) + longitude_step
        row_radius = math.ceil(lat_margin / latitude_step)
        col_radius = math.ceil(lon_margin / longitude_step)
        rows = self._index_range(row_center, row_radius, self.ny)
        cols = self._index_range(col_center, col_radius, self.nx)

        echoes: dict[tuple[int, int], tuple[float, float, float, float]] = {}
        for j in rows:
            cell_latitude = self.first_latitude + j * signed_latitude_step
            for i in cols:
                value = float(self.values[self._linear_index(i, j)])
                if not math.isfinite(value) or not threshold_dbz <= value <= 80.0:
                    continue
                cell_longitude = (self.first_longitude + i * signed_longitude_step) % 360.0
                distance = distance_miles(latitude, longitude, cell_latitude, cell_longitude)
                if distance <= radius_miles:
                    echoes[(i, j)] = (cell_latitude, cell_longitude, value, distance)

        objects: list[dict[str, float | int]] = []
        unvisited = set(echoes)
        while unvisited:
            start = unvisited.pop()
            component = [start]
            pending = [start]
            while pending:
                i, j = pending.pop()
                for di in (-1, 0, 1):
                    for dj in (-1, 0, 1):
                        neighbor = (i + di, j + dj)
                        if (di or dj) and neighbor in unvisited:
                            unvisited.remove(neighbor)
                            component.append(neighbor)
                            pending.append(neighbor)
            component_values = [echoes[key] for key in component]
            strongest = max(value[2] for value in component_values)
            if len(component) < 2 and strongest < 45.0:
                continue
            centroid_longitude = sum(value[1] for value in component_values) / len(component)
            objects.append(
                {
                    "latitude": sum(value[0] for value in component_values) / len(component),
                    "longitude": (centroid_longitude + 180.0) % 360.0 - 180.0,
                    "distance_miles": min(value[3] for value in component_values),
                    "max_reflectivity_dbz": strongest,
                    "cells": len(component),
                    "effective_radius_miles": math.sqrt(len(component)) * 0.35,
                }
            )
        objects.sort(key=lambda item: (item["distance_miles"], -item["max_reflectivity_dbz"]))
        return objects[:12]


def _valid_time(handle: Any) -> datetime:
    date_value = int(eccodes.codes_get(handle, "validityDate"))
    time_value = int(eccodes.codes_get(handle, "validityTime"))
    return datetime.strptime(f"{date_value:08d}{time_value:04d}", "%Y%m%d%H%M").replace(tzinfo=UTC)


def _read_grid(product: str, directory: str) -> _Grid:
    filename = f"MRMS_{product}.latest.grib2.gz"
    url = f"{BASE_URL}/{directory}/{filename}"
    handle = _download_grid_message(product, url)
    try:
        grid_type = eccodes.codes_get(handle, "gridType")
        if grid_type != "regular_ll":
            raise ProviderError(f"MRMS {product} uses unsupported grid {grid_type}")
        nx = int(eccodes.codes_get(handle, "Nx"))
        ny = int(eccodes.codes_get(handle, "Ny"))
        first_latitude = float(eccodes.codes_get(handle, "latitudeOfFirstGridPointInDegrees"))
        first_longitude = float(eccodes.codes_get(handle, "longitudeOfFirstGridPointInDegrees"))
        last_latitude = float(eccodes.codes_get(handle, "latitudeOfLastGridPointInDegrees"))
        last_longitude = float(eccodes.codes_get(handle, "longitudeOfLastGridPointInDegrees"))
        latitude_step = abs((last_latitude - first_latitude) / (ny - 1))
        longitude_step = abs((last_longitude - first_longitude) / (nx - 1))
        values = eccodes.codes_get_values(handle)
        if len(values) != nx * ny:
            raise ProviderError(f"MRMS {product} has invalid grid dimensions")
        return _Grid(
            values=values,
            nx=nx,
            ny=ny,
            first_latitude=first_latitude,
            first_longitude=first_longitude,
            latitude_step=latitude_step,
            longitude_step=longitude_step,
            i_scans_negatively=bool(eccodes.codes_get(handle, "iScansNegatively")),
            j_scans_positively=bool(eccodes.codes_get(handle, "jScansPositively")),
            j_points_are_consecutive=bool(eccodes.codes_get(handle, "jPointsAreConsecutive")),
            alternative_row_scanning=bool(eccodes.codes_get(handle, "alternativeRowScanning")),
            valid_at=_valid_time(handle),
            url=url,
        )
    finally:
        eccodes.codes_release(handle)


class MrmsSnapshot:
    """One download per national product, then local venue-radius sampling."""

    def __init__(self, *, include_storm_motion: bool = False) -> None:
        self._grids = {
            key: _read_grid(directory, directory) for key, directory in _PRODUCTS.items()
        }
        self._reflectivity_grid: _Grid | None = None
        if include_storm_motion:
            try:
                self._reflectivity_grid = _read_grid(
                    "MergedReflectivityQComposite", "MergedReflectivityQComposite"
                )
            except ProviderError:
                # Reflectivity adds optional motion context; preserve lightning estimates
                # when this larger public radar product is unavailable.
                self._reflectivity_grid = None

    @property
    def latest_valid_at(self) -> datetime:
        return max(grid.valid_at for grid in self._grids.values())

    @property
    def sources(self) -> dict[str, str]:
        sources = {name: grid.url for name, grid in self._grids.items()}
        reflectivity_grid = getattr(self, "_reflectivity_grid", None)
        if reflectivity_grid is not None:
            sources["storm_motion_reflectivity"] = reflectivity_grid.url
        return sources

    def sample(
        self, venue: Venue, policy: WeatherPolicy, *, include_storm_motion: bool = False
    ) -> dict[str, Any]:
        radius = policy.trigger_radius_miles
        probabilities: dict[str, float | None] = {}
        coverage: dict[str, bool] = {}
        valid_at: dict[str, datetime] = {}
        for key in ("probability_next_30min", "probability_next_60min"):
            grid = self._grids[key]
            summary = grid.summarize(venue.latitude, venue.longitude, 0, radius)
            peak = summary["max"]
            probabilities[key] = min(1.0, float(peak) / 100.0) if peak is not None else None
            coverage[key] = bool(summary["cells"])
            valid_at[key] = grid.valid_at

        density_grid = self._grids["cg_density_1min"]
        density = density_grid.summarize(venue.latitude, venue.longitude, 0, radius)
        coverage["cg_density_1min"] = bool(density["cells"])
        valid_at["cg_density_1min"] = density_grid.valid_at
        latest_event = density_grid.valid_at if (density["max"] or 0) > 0 else None
        rings: dict[str, Any] = {}
        bands = [(0.0, radius, "trigger")]
        for lower, upper in ((radius, 10.0), (10.0, 15.0), (15.0, 20.0), (20.0, 30.0)):
            if upper > lower:
                bands.append((lower, upper, f"{lower:g}-{upper:g}_mi"))
        for lower, upper, name in bands:
            rings[name] = {
                "probability_next_30min": self._grids["probability_next_30min"].summarize(
                    venue.latitude, venue.longitude, lower, upper
                ),
                "cg_density_1min": density_grid.summarize(
                    venue.latitude, venue.longitude, lower, upper
                ),
            }
        motion_grid = getattr(self, "_reflectivity_grid", None)
        storm_echoes = (
            {
                "valid_at": motion_grid.valid_at,
                "source_url": motion_grid.url,
                "threshold_dbz": 35.0,
                "coverage_cells": motion_grid.summarize(venue.latitude, venue.longitude, 0, 60.0)[
                    "cells"
                ],
                "objects": motion_grid.storm_echoes(
                    venue.latitude, venue.longitude, radius_miles=60.0, threshold_dbz=35.0
                ),
            }
            if include_storm_motion and motion_grid is not None
            else None
        )
        return {
            **probabilities,
            "cg_density_per_km2_min": density,
            "last_qualifying_event_at": latest_event,
            "coverage": coverage,
            "has_venue_data": any(coverage.values()),
            "product_valid_at": valid_at,
            "trigger_radius_miles": radius,
            "probability_valid_at": self._grids["probability_next_30min"].valid_at,
            "density_valid_at": density_grid.valid_at,
            "rings": rings,
            "storm_echoes": storm_echoes,
            "sources": self.sources,
        }
