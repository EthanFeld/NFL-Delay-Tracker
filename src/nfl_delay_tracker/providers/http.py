"""Small standard-library JSON HTTP client for scheduled Actions."""

from __future__ import annotations

import json
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class ProviderError(RuntimeError):
    """A remote provider failed or returned invalid content."""


class RetryableProviderError(ProviderError):
    """A remote request failed for a reason that may succeed on retry."""


def get_json(url: str, *, headers: dict[str, str] | None = None, timeout: int = 25) -> Any:
    request_headers = {"Accept": "application/json", "User-Agent": "NFL Delay Tracker/0.1"}
    if headers:
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except HTTPError as exc:
        error_type = (
            RetryableProviderError
            if exc.code in {408, 429} or 500 <= exc.code < 600
            else ProviderError
        )
        raise error_type(f"GET {url} failed: {exc}") from exc
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RetryableProviderError(f"GET {url} failed: {exc}") from exc


def get_text(url: str, *, headers: dict[str, str] | None = None, timeout: int = 25) -> str:
    request_headers = {"Accept": "text/csv,text/plain,*/*", "User-Agent": "NFL Delay Tracker/0.1"}
    if headers:
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            return cast(str, response.read().decode("utf-8-sig"))
    except (HTTPError, URLError, TimeoutError, UnicodeDecodeError) as exc:
        raise ProviderError(f"GET {url} failed: {exc}") from exc


def get_bytes(url: str, *, headers: dict[str, str] | None = None, timeout: int = 25) -> bytes:
    request_headers = {
        "Accept": "application/octet-stream,*/*",
        "User-Agent": "NFL Delay Tracker/0.1",
    }
    if headers:
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            return cast(bytes, response.read())
    except (HTTPError, URLError, TimeoutError) as exc:
        raise ProviderError(f"GET {url} failed: {exc}") from exc
