"""Global ECMWF ensemble conditions adapter for venues outside NWS coverage or horizon.

The weather-code field is retained as a categorical conditions outlook only. It is
not a thunderstorm or venue-delay probability input.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from nfl_delay_tracker.models import Venue
from nfl_delay_tracker.providers.http import ProviderError, get_json

API_ROOT = "https://ensemble-api.open-meteo.com/v1/ensemble"
ATTRIBUTION_URL = "https://open-meteo.com/en/docs/ensemble-api"
MODEL = "ecmwf_ifs025_ensemble"
_MEMBER_FIELD = re.compile(r"^weather_code_member\d+$")
_CLEAR_OR_CLOUDY_CODES = {0, 1, 2, 3}
_FOG_CODES = {45, 48}


class OpenMeteoEnsembleProvider:
    """Read global ECMWF ensemble weather codes without deriving lightning odds."""

    def __init__(self) -> None:
        self._payload_by_venue: dict[str, tuple[dict[str, Any], datetime, str]] = {}

    def _payload(self, venue: Venue) -> tuple[dict[str, Any], datetime, str]:
        cached = self._payload_by_venue.get(venue.venue_id)
        if cached is not None:
            return cached

        query = urlencode(
            {
                "latitude": f"{venue.latitude:.4f}",
                "longitude": f"{venue.longitude:.4f}",
                "models": MODEL,
                "hourly": "weather_code",
                "forecast_days": 15,
                "timezone": "GMT",
            }
        )
        request_url = f"{API_ROOT}?{query}"
        payload = get_json(request_url)
        if not isinstance(payload, dict):
            raise ProviderError("Open-Meteo ensemble response is not an object")
        fetched_at = datetime.now(UTC)
        cached = (payload, fetched_at, request_url)
        self._payload_by_venue[venue.venue_id] = cached
        return cached

    def fetch_conditions_outlook(
        self, venue: Venue, *, kickoff: datetime
    ) -> tuple[dict[str, Any], datetime, str]:
        """Return nearest native three-hour member conditions and provenance.

        Missing member values remain missing. No weather code is converted into a
        thunder probability, hazard bin, or delay estimate.
        """
        if kickoff.tzinfo is None or kickoff.utcoffset() is None:
            raise ValueError("kickoff must include a timezone")
        payload, fetched_at, request_url = self._payload(venue)
        hourly = payload.get("hourly")
        if not isinstance(hourly, dict):
            raise ProviderError("Open-Meteo ensemble response has no hourly object")
        times = hourly.get("time")
        if not isinstance(times, list) or not times:
            raise ProviderError("Open-Meteo ensemble response has no valid-time array")
        member_names = sorted(key for key in hourly if _MEMBER_FIELD.fullmatch(key))
        if not member_names:
            raise ProviderError("Open-Meteo ensemble response has no member weather-code fields")

        parsed_times: list[datetime] = []
        for value in times:
            if not isinstance(value, str):
                raise ProviderError("Open-Meteo ensemble valid times contain a non-string value")
            try:
                at = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ProviderError(f"Invalid Open-Meteo valid time: {value}") from exc
            if at.tzinfo is None or at.utcoffset() is None:
                at = at.replace(tzinfo=UTC)
            parsed_times.append(at.astimezone(UTC))

        target = kickoff.astimezone(UTC)
        native_resolution_hours = (
            6
            if (target - fetched_at) > timedelta(hours=144)
            else 3
        )
        native_indices = [
            index
            for index, at in enumerate(parsed_times)
            if at.minute == 0
            and at.second == 0
            and at.hour % native_resolution_hours == 0
        ]
        if not native_indices:
            raise ProviderError(
                "Open-Meteo ensemble response has no native-resolution values"
            )
        valid_index = min(
            native_indices,
            key=lambda index: abs((parsed_times[index] - target).total_seconds()),
        )
        valid_at = parsed_times[valid_index]
        tolerance = timedelta(
            minutes=90 if native_resolution_hours == 3 else 180
        )
        if abs(valid_at - target) > tolerance:
            raise ProviderError("No Open-Meteo ensemble hour is close to kickoff")

        counts = {
            "clear_or_cloudy": 0,
            "fog": 0,
            "precipitation_or_snow": 0,
            "other_or_unclassified": 0,
        }
        valid_members = 0
        for member in member_names:
            values = hourly.get(member)
            if not isinstance(values, list) or valid_index >= len(values):
                continue
            value = values[valid_index]
            if isinstance(value, bool) or not isinstance(value, int | float):
                continue
            if not math.isfinite(float(value)) or int(value) != value:
                continue
            code = int(value)
            valid_members += 1
            if code in _CLEAR_OR_CLOUDY_CODES:
                counts["clear_or_cloudy"] += 1
            elif code in _FOG_CODES:
                counts["fog"] += 1
            elif 50 <= code <= 86:
                counts["precipitation_or_snow"] += 1
            else:
                # Includes unsupported and convective-coded conditions. The API
                # docs say these codes cannot estimate thunderstorms.
                counts["other_or_unclassified"] += 1

        if valid_members == 0:
            raise ProviderError("Open-Meteo ensemble has no valid member codes at kickoff")

        result = {
            "model": "ECMWF IFS ensemble",
            "valid_at": valid_at.isoformat(),
            "native_resolution_hours": native_resolution_hours,
            "grid_resolution_km": 25,
            "member_count": len(member_names),
            "valid_member_count": valid_members,
            "condition_member_counts": counts,
            "attribution_url": ATTRIBUTION_URL,
            "source_url": request_url,
            "thunderstorm_probability": None,
            "storm_motion": "unavailable",
        }
        return result, fetched_at, request_url
