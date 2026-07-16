"""Docker Hub Image Management discovery adapter."""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Mapping

from dockerhub_cleanup.domain import extract_digests
from dockerhub_cleanup.errors import CleanupError
from dockerhub_cleanup.http import HttpTransport, UrllibTransport

IMAGE_MANAGEMENT = "https://hub.docker.com/repository/docker"
USER_AGENT = "dockerhub-cleanup/0.1"
UNDEFINED_REFERENCE = -5


class ImageManagementClient:
    """Discover all repository digests through Docker Hub's web session API."""

    def __init__(
        self,
        cookie: str,
        *,
        transport: HttpTransport | None = None,
    ):
        if not cookie:
            raise CleanupError(
                "untagged discovery needs DH_COOKIE from an authenticated Docker Hub session"
            )
        self._cookie = cookie
        self._transport = (
            UrllibTransport(retries=2, retry_methods=frozenset({"GET", "POST"}))
            if transport is None
            else transport
        )

    @property
    def headers(self) -> Mapping[str, str]:
        """Return headers expected by the Image Management web route."""

        return {
            "Cookie": self._cookie,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }

    def all_digests(self, namespace: str, repository: str) -> set[str]:
        """Return every digest from every Image Management page."""

        encoded_namespace = urllib.parse.quote(namespace, safe="")
        encoded_repository = urllib.parse.quote(repository, safe="")
        repository_url = f"{IMAGE_MANAGEMENT}/{encoded_namespace}/{encoded_repository}"
        url = f"{repository_url}/image-management.data?sortField=last_pushed&sortOrder=asc"
        headers = {**self.headers, "Referer": f"{repository_url}/tags"}
        payload = self._request_json("GET", url, headers=headers)
        digests = extract_digests(payload)
        seen_cursors: set[str] = set()

        while (cursor := _next_cursor(payload)) is not None:
            if cursor in seen_cursors:
                raise CleanupError("Image Management pagination repeated a cursor")
            seen_cursors.add(cursor)
            data = urllib.parse.urlencode(
                {"intent": "paginate", "lastEvaluatedKey": cursor}
            ).encode()
            payload = self._request_json(
                "POST",
                url,
                headers={
                    **headers,
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data=data,
            )
            digests.update(extract_digests(payload))
        return digests

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        data: bytes | None = None,
    ) -> object:
        response = self._transport.request(method, url, headers=headers, data=data)
        try:
            return json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CleanupError(f"{method} Image Management returned invalid JSON") from exc


def _next_cursor(payload: object) -> str | None:
    if not isinstance(payload, list):
        raise CleanupError("Image Management returned an unexpected response")
    for value in payload:
        if not isinstance(value, dict):
            continue
        for key_reference, cursor_reference in value.items():
            if not (
                isinstance(key_reference, str)
                and key_reference.startswith("_")
                and key_reference[1:].isdigit()
            ):
                continue
            key_index = int(key_reference[1:])
            if key_index >= len(payload) or payload[key_index] != "lastEvaluatedKey":
                continue
            if type(cursor_reference) is int and cursor_reference == UNDEFINED_REFERENCE:
                return None
            if not (type(cursor_reference) is int and 0 <= cursor_reference < len(payload)):
                raise CleanupError("Image Management returned an invalid pagination cursor")
            cursor = payload[cursor_reference]
            if not isinstance(cursor, str):
                raise CleanupError("Image Management returned an invalid pagination cursor")
            return cursor
    return None
