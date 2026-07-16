import http.client
from collections.abc import Callable
from unittest.mock import MagicMock, patch

import pytest

from dockerhub_cleanup.errors import CleanupError
from dockerhub_cleanup.http import HttpResponse, UrllibTransport

ReadErrorFactory = Callable[[], Exception]


def _read_error_response(error: Exception) -> MagicMock:
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = 200
    response.headers.items.return_value = []
    response.read.side_effect = error
    return response


def _successful_response() -> MagicMock:
    response = MagicMock()
    response.__enter__.return_value = response
    response.status = 200
    response.headers.items.return_value = [("Content-Type", "application/json")]
    response.read.return_value = b"{}"
    return response


@pytest.mark.parametrize(
    "error_factory",
    [
        lambda: http.client.RemoteDisconnected("closed"),
        lambda: http.client.IncompleteRead(b"partial", 10),
        lambda: ConnectionResetError("reset"),
    ],
)
def test_read_errors_are_retried(error_factory: ReadErrorFactory) -> None:
    responses = [
        _read_error_response(error_factory()),
        _read_error_response(error_factory()),
        _successful_response(),
    ]
    transport = UrllibTransport(retries=2, retry_delay=0)

    with (
        patch("urllib.request.urlopen", side_effect=responses) as urlopen,
        patch("dockerhub_cleanup.http.time.sleep") as sleep,
    ):
        result = transport.request("GET", "https://example.test/data")

    assert result == HttpResponse(200, {"Content-Type": "application/json"}, b"{}")
    assert urlopen.call_count == 3
    assert sleep.call_count == 2


@pytest.mark.parametrize(
    ("error_factory", "error_name"),
    [
        (lambda: http.client.RemoteDisconnected("closed"), "RemoteDisconnected"),
        (lambda: http.client.IncompleteRead(b"partial", 10), "IncompleteRead"),
        (lambda: ConnectionResetError("reset"), "ConnectionResetError"),
    ],
)
def test_exhausted_read_errors_fail_safely(
    error_factory: ReadErrorFactory,
    error_name: str,
) -> None:
    responses = [_read_error_response(error_factory()) for _ in range(3)]
    transport = UrllibTransport(retries=2, retry_delay=0)

    with (
        patch("urllib.request.urlopen", side_effect=responses) as urlopen,
        patch("dockerhub_cleanup.http.time.sleep") as sleep,
        pytest.raises(CleanupError, match=error_name) as raised,
    ):
        transport.request("GET", "https://example.test/data")

    assert "partial" not in str(raised.value)
    assert "reset" not in str(raised.value)
    assert "closed" not in str(raised.value)
    assert urlopen.call_count == 3
    assert sleep.call_count == 2
