"""NWS forecast-grid thunder probability adapter."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from nfl_delay_tracker.models import HazardPoint, Venue
from nfl_delay_tracker.providers.http import get_json

_DURATION = re.compile(r"^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def parse_valid_time(value: str) -> tuple[datetime, datetime]:
    start_text, duration_text = value.split("/", maxsplit=1)
    start = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
    match = _DURATION.match(duration_text)
    if not match:
        raise ValueError(f"unsupported NWS validTime duration: {duration_text}")
    days, hours, minutes, seconds = (int(part or 0) for part in match.groups())
    end = start + timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    return start.astimezone(UTC), end.astimezone(UTC)


class NwsGridProvider:
    """Fetch NWS forecast-grid probabilities; no live lightning observations."""

    def __init__(self) -> None:
        self._grid_url_by_venue: dict[str, str] = {}
        self.contact_email = os.getenv("NWS_CONTACT_EMAIL")

    @property
    def _headers(self) -> dict[str, str]:
        user_agent = "NFL Delay Tracker/0.1"
        if self.contact_email:
            user_agent += f" ({self.contact_email})"
        return {"User-Agent": user_agent}

    def fetch_hazards(
        self, venue: Venue, *, kickoff: datetime
    ) -> tuple[list[HazardPoint], datetime, str]:
        grid_url = self._grid_url_by_venue.get(venue.venue_id)
        if grid_url is None:
            point_url = f"https://api.weather.gov/points/{venue.latitude:.4f},{venue.longitude:.4f}"
            point_result = get_json(
                point_url,
                headers=self._headers,
            )
            grid_url = point_result["properties"]["forecastGridData"]
            self._grid_url_by_venue[venue.venue_id] = grid_url
        payload: dict[str, Any] = get_json(
            grid_url,
            headers=self._headers,
        )
        properties = payload.get("properties", {})
        thunder = properties.get("probabilityOfThunder", {}).get("values", [])
        updated = properties.get("updateTime")
        fetched_at = datetime.now(UTC)
        if isinstance(updated, str):
            try:
                fetched_at = datetime.fromisoformat(updated.replace("Z", "+00:00")).astimezone(UTC)
            except ValueError:
                pass
        elif isinstance(properties.get("validTimes"), str):
            try:
                fetched_at = parse_valid_time(properties["validTimes"])[0]
            except (ValueError, TypeError):
                pass
        hazards: list[HazardPoint] = []
        for entry in thunder:
            raw_value = entry.get("value")
            raw_interval = entry.get("validTime")
            if raw_value is None or not raw_interval:
                continue
            start, end = parse_valid_time(raw_interval)
            original_duration = max(1, int((end - start).total_seconds() / 60))
            if end <= kickoff - timedelta(minutes=90) or start >= kickoff + timedelta(hours=9):
                continue
            start = max(start, kickoff - timedelta(minutes=90))
            end = min(end, kickoff + timedelta(hours=9))
            probability = max(0.0, min(1.0, float(raw_value) / 100.0))
            per_five = 1 - (1 - probability) ** (5 / original_duration) if probability < 1 else 1.0
            start_minute = int((start - kickoff).total_seconds() // 60)
            end_minute = int((end - kickoff).total_seconds() // 60)
            first = (start_minute // 5) * 5
            for offset in range(first, end_minute, 5):
                hazards.append(
                    HazardPoint(
                        offset_minutes=offset,
                        probability=per_five,
                        source="NWS probabilityOfThunder; forecast proxy, not MRMS lightning",
                        valid_at=kickoff.astimezone(UTC) + timedelta(minutes=offset),
                    )
                )
        hazards.sort(key=lambda point: point.offset_minutes)
        deduped: dict[int, HazardPoint] = {}
        for point in hazards:
            current = deduped.get(point.offset_minutes)
            if current is None or point.probability > current.probability:
                deduped[point.offset_minutes] = point
        return [deduped[key] for key in sorted(deduped)], fetched_at, grid_url
