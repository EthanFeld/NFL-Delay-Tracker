from urllib.error import HTTPError

import pytest

from nfl_delay_tracker.providers import http
from nfl_delay_tracker.providers.http import ProviderError, RetryableProviderError


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
def test_get_json_marks_transient_http_status_as_retryable(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    def fail_request(_request, *, timeout: int) -> None:
        assert timeout == 25
        raise HTTPError("https://example.test", status, "temporary", None, None)

    monkeypatch.setattr(http, "urlopen", fail_request)

    with pytest.raises(RetryableProviderError):
        http.get_json("https://example.test")


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_get_json_does_not_mark_permanent_http_status_as_retryable(
    monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    def fail_request(_request, *, timeout: int) -> None:
        assert timeout == 25
        raise HTTPError("https://example.test", status, "permanent", None, None)

    monkeypatch.setattr(http, "urlopen", fail_request)

    with pytest.raises(ProviderError) as error:
        http.get_json("https://example.test")

    assert not isinstance(error.value, RetryableProviderError)
