"""Small injectable HTTP boundary built on the Python standard library."""

from __future__ import annotations

import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from dockerhub_cleanup.errors import CleanupError


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

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout

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
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return HttpResponse(
                    status=response.status,
                    headers=dict(response.headers.items()),
                    body=response.read(),
                )
        except urllib.error.HTTPError as exc:
            raise CleanupError(f"{method} {url} failed with HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise CleanupError(f"{method} {url} failed: {exc.reason}") from exc
        except TimeoutError as exc:
            raise CleanupError(f"{method} {url} timed out") from exc
