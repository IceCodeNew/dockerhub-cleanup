"""Docker Hub Image Management discovery adapter."""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Mapping

from dockerhub_cleanup.domain import extract_digests
from dockerhub_cleanup.errors import CleanupError
from dockerhub_cleanup.http import HttpTransport, UrllibTransport

IMAGE_MANAGEMENT = "https://hub.docker.com/repository/docker"


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
        self._transport = transport or UrllibTransport()

    @property
    def headers(self) -> Mapping[str, str]:
        """Return headers expected by the Image Management web route."""

        return {
            "Cookie": self._cookie,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
        }

    def all_digests(self, namespace: str, repository: str) -> set[str]:
        """Return every digest from every Image Management page."""

        encoded_namespace = urllib.parse.quote(namespace, safe="")
        encoded_repository = urllib.parse.quote(repository, safe="")
        url = (
            f"{IMAGE_MANAGEMENT}/{encoded_namespace}/{encoded_repository}/image-management.data"
            "?sortField=last_pushed&sortOrder=asc"
        )
        payload = self._request_json("GET", url, headers=self.headers)
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
                    **self.headers,
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
    for index, value in enumerate(payload):
        if value != "lastEvaluatedKey":
            continue
        if index + 1 >= len(payload):
            raise CleanupError("Image Management returned an invalid pagination cursor")
        cursor = payload[index + 1]
        if not isinstance(cursor, str):
            raise CleanupError("Image Management returned an invalid pagination cursor")
        return cursor
    return None
