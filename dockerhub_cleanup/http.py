"""Small injectable HTTP boundary built on the Python standard library."""

from __future__ import annotations

import http.client
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from dockerhub_cleanup.errors import CleanupError, HttpNotFoundError

MAX_RETRY_DELAY_SECONDS = 30.0


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
        retry_methods: frozenset[str] = frozenset({"GET"}),
    ):
        self.timeout = timeout
        self.retries = retries
        self.retry_delay = retry_delay
        self.retry_methods = frozenset(method.upper() for method in retry_methods)

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
                if (exc.code == 429 or 500 <= exc.code < 600) and self._can_retry(method, attempt):
                    self._wait_before_retry(attempt, exc.headers.get("Retry-After"))
                    attempt += 1
                    continue
                raise CleanupError(f"{method} {url} failed with HTTP {exc.code}") from exc
            except (http.client.HTTPException, OSError) as exc:
                if not self._can_retry(method, attempt):
                    if isinstance(exc, urllib.error.URLError):
                        raise CleanupError(f"{method} {url} failed: {exc.reason}") from exc
                    if isinstance(exc, TimeoutError):
                        raise CleanupError(f"{method} {url} timed out") from exc
                    raise CleanupError(f"{method} {url} failed: {type(exc).__name__}") from exc
                self._wait_before_retry(attempt)
                attempt += 1

    def _can_retry(self, method: str, attempt: int) -> bool:
        return method.upper() in self.retry_methods and attempt < self.retries

    def _wait_before_retry(self, attempt: int, retry_after: str | None = None) -> None:
        delay = self.retry_delay * 2**attempt
        if retry_after is not None and retry_after.isdigit():
            delay = float(retry_after)
        time.sleep(min(delay, MAX_RETRY_DELAY_SECONDS))
