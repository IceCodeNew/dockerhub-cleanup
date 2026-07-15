"""Docker Hub API adapter."""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Iterator, Mapping
from datetime import datetime

from dockerhub_cleanup.domain import Tag, parse_api_timestamp
from dockerhub_cleanup.errors import CleanupError, HttpNotFoundError
from dockerhub_cleanup.http import HttpTransport, UrllibTransport

HUB_API = "https://hub.docker.com"


class DockerHubClient:
    """Access repositories and tags through Docker Hub's supported API."""

    def __init__(
        self,
        username: str,
        pat: str,
        *,
        transport: HttpTransport | None = None,
    ):
        self._transport = UrllibTransport() if transport is None else transport
        self._token = self._authenticate(username, pat)

    @property
    def auth_headers(self) -> Mapping[str, str]:
        """Return headers for authenticated Docker Hub API calls."""

        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/json",
        }

    def _authenticate(self, username: str, pat: str) -> str:
        payload = self._json_request(
            "POST",
            f"{HUB_API}/v2/auth/token",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"identifier": username, "secret": pat}).encode(),
        )
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise CleanupError("Docker Hub authentication returned no access token")
        return token

    def _json_request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        data: bytes | None = None,
    ) -> object:
        response = self._transport.request(method, url, headers=headers, data=data)
        try:
            return json.loads(response.body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CleanupError(f"{method} {url} returned invalid JSON") from exc

    def _paginate(self, url: str) -> Iterator[dict[str, object]]:
        seen_urls: set[str] = set()
        previous_page_was_partial = False
        while url:
            if url in seen_urls:
                raise CleanupError("Docker Hub pagination repeated a URL")
            seen_urls.add(url)
            try:
                payload = self._json_request("GET", url, headers=self.auth_headers)
            except HttpNotFoundError:
                if previous_page_was_partial:
                    return
                raise
            if not isinstance(payload, dict):
                raise CleanupError(f"unexpected paginated response from {url}")
            results = payload.get("results")
            if not isinstance(results, list):
                raise CleanupError(f"unexpected paginated response from {url}")
            for item in results:
                if not isinstance(item, dict):
                    raise CleanupError(f"unexpected paginated item from {url}")
                normalized: dict[str, object] = {}
                for key, value in item.items():
                    if not isinstance(key, str):
                        raise CleanupError(f"unexpected paginated item from {url}")
                    normalized[key] = value
                yield normalized
            next_url = payload.get("next")
            if next_url is not None and not isinstance(next_url, str):
                raise CleanupError(f"unexpected pagination URL from {url}")
            page_size = _page_size(url)
            previous_page_was_partial = page_size is not None and len(results) < page_size
            url = _trusted_hub_url(next_url) if next_url else ""

    def repositories(self, namespace: str) -> list[str]:
        """List every visible repository in a namespace."""

        encoded = urllib.parse.quote(namespace, safe="")
        url = f"{HUB_API}/v2/namespaces/{encoded}/repositories?page_size=100"
        repositories: list[str] = []
        for item in self._paginate(url):
            name = item.get("name")
            if not isinstance(name, str):
                raise CleanupError("Docker Hub returned a repository without a name")
            repositories.append(name)
        return repositories

    def tags(self, namespace: str, repository: str) -> list[Tag]:
        """List tags and cleanup metadata for a repository."""

        encoded_namespace = urllib.parse.quote(namespace, safe="")
        encoded_repository = urllib.parse.quote(repository, safe="")
        url = (
            f"{HUB_API}/v2/namespaces/{encoded_namespace}/repositories/"
            f"{encoded_repository}/tags?page_size=100"
        )
        tags: list[Tag] = []
        for item in self._paginate(url):
            name = item.get("name")
            digest = item.get("digest")
            if not isinstance(name, str) or not isinstance(digest, str):
                raise CleanupError("Docker Hub returned incomplete tag metadata")
            tags.append(
                Tag(
                    repository=repository,
                    name=name,
                    digest=digest,
                    last_pulled=_tag_timestamp(item, "tag_last_pulled"),
                    last_pushed=_tag_timestamp(item, "tag_last_pushed"),
                )
            )
        return tags

    def delete_tag(self, namespace: str, repository: str, tag: str) -> None:
        """Delete one tag without deleting a shared manifest by digest."""

        parts = [urllib.parse.quote(value, safe="") for value in (namespace, repository, tag)]
        url = f"{HUB_API}/v2/namespaces/{parts[0]}/repositories/{parts[1]}/tags/{parts[2]}"
        self._transport.request("DELETE", url, headers=self.auth_headers)


def _optional_string(item: Mapping[str, object], key: str) -> str | None:
    value = item.get(key)
    if value is None or isinstance(value, str):
        return value
    raise CleanupError(f"Docker Hub returned invalid {key} metadata")


def _tag_timestamp(item: Mapping[str, object], key: str) -> datetime | None:
    try:
        return parse_api_timestamp(_optional_string(item, key))
    except ValueError as exc:
        raise CleanupError(f"Docker Hub returned invalid {key} metadata") from exc


def _trusted_hub_url(url: str) -> str:
    resolved = urllib.parse.urljoin(f"{HUB_API}/", url)
    parsed = urllib.parse.urlsplit(resolved)
    hub = urllib.parse.urlsplit(HUB_API)
    if (parsed.scheme.lower(), parsed.netloc.lower()) != (
        hub.scheme.lower(),
        hub.netloc.lower(),
    ):
        raise CleanupError("Docker Hub pagination returned an untrusted URL")
    return resolved


def _page_size(url: str) -> int | None:
    values = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("page_size")
    if not values or not values[0].isdigit():
        return None
    return int(values[0])
