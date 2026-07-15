"""Small injectable HTTP boundary built on the Python standard library."""

from __future__ import annotations

import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from dockerhub_cleanup.errors import CleanupError, HttpNotFoundError


@dataclass(frozen=True)
class HttpResponse:
    """HTTP response data consumed by registry adapters."""

    status: int
    headers: Mapping[str, str]
    body: bytes


class HttpTransport(Protocol):
    """Transport contract used by external service adapters."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        data: bytes | None = None,
    ) -> HttpResponse: ...


class UrllibTransport:
    """Perform HTTP requests without exposing credentials in errors."""

    def __init__(
        self,
        timeout: float = 30.0,
        *,
        retries: int = 0,
        retry_delay: float = 0.5,
    ):
        self.timeout = timeout
        self.retries = retries
        self.retry_delay = retry_delay

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        data: bytes | None = None,
    ) -> HttpResponse:
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers=dict(headers or {}),
        )
        attempt = 0
        while True:
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return HttpResponse(
                        status=response.status,
                        headers=dict(response.headers.items()),
                        body=response.read(),
                    )
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    raise HttpNotFoundError(f"{method} {url} failed with HTTP 404") from exc
                raise CleanupError(f"{method} {url} failed with HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt >= self.retries:
                    if isinstance(exc, urllib.error.URLError):
                        raise CleanupError(f"{method} {url} failed: {exc.reason}") from exc
                    raise CleanupError(f"{method} {url} timed out") from exc
                time.sleep(self.retry_delay * 2**attempt)
                attempt += 1
