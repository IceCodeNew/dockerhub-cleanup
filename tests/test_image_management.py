import json
import urllib.parse
from collections.abc import Mapping

import pytest

from dockerhub_cleanup.errors import CleanupError
from dockerhub_cleanup.http import HttpResponse
from dockerhub_cleanup.image_management import ImageManagementClient


class FakeTransport:
    def __init__(self, responses: list[HttpResponse]):
        self.responses = responses
        self.requests: list[tuple[str, str, Mapping[str, str] | None, bytes | None]] = []

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        data: bytes | None = None,
    ) -> HttpResponse:
        self.requests.append((method, url, headers, data))
        return self.responses.pop(0)


def response(payload: object) -> HttpResponse:
    return HttpResponse(200, {}, json.dumps(payload).encode())


def test_cookie_is_required() -> None:
    with pytest.raises(CleanupError, match="DH_COOKIE"):
        ImageManagementClient("")


def test_single_page_discovery_encodes_path_and_sends_session_headers() -> None:
    digest = "sha256:" + "a" * 64
    transport = FakeTransport([response(["nested", {"digest": digest}])])
    client = ImageManagementClient("session=secret", transport=transport)

    assert client.all_digests("user name", "app/name") == {digest}

    method, url, headers, data = transport.requests[0]
    assert method == "GET"
    assert "/user%20name/app%2Fname/image-management.data?" in url
    assert headers == {
        "Cookie": "session=secret",
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json",
    }
    assert data is None


def test_discovery_follows_post_cursor_pages() -> None:
    digest_a = "sha256:" + "a" * 64
    digest_b = "sha256:" + "b" * 64
    transport = FakeTransport(
        [
            response([digest_a, "lastEvaluatedKey", "cursor one"]),
            response([digest_b]),
        ]
    )
    client = ImageManagementClient("session=secret", transport=transport)

    assert client.all_digests("user", "app") == {digest_a, digest_b}

    method, _, headers, data = transport.requests[1]
    assert method == "POST"
    assert headers is not None
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert urllib.parse.parse_qs((data or b"").decode()) == {
        "intent": ["paginate"],
        "lastEvaluatedKey": ["cursor one"],
    }


def test_repeated_cursor_is_rejected() -> None:
    transport = FakeTransport(
        [
            response(["lastEvaluatedKey", "same"]),
            response(["lastEvaluatedKey", "same"]),
        ]
    )
    client = ImageManagementClient("session=secret", transport=transport)

    with pytest.raises(CleanupError, match="repeated a cursor"):
        client.all_digests("user", "app")


@pytest.mark.parametrize(
    "payload",
    [
        {},
        ["lastEvaluatedKey"],
        ["lastEvaluatedKey", 3],
    ],
)
def test_invalid_response_or_cursor_is_rejected(payload: object) -> None:
    client = ImageManagementClient("session=secret", transport=FakeTransport([response(payload)]))
    with pytest.raises(CleanupError, match="unexpected response|pagination cursor"):
        client.all_digests("user", "app")


def test_invalid_json_is_reported_without_body() -> None:
    transport = FakeTransport([HttpResponse(200, {}, b"secret-not-json")])
    client = ImageManagementClient("session=secret", transport=transport)

    with pytest.raises(CleanupError, match="invalid JSON") as raised:
        client.all_digests("user", "app")
    assert "secret-not-json" not in str(raised.value)
