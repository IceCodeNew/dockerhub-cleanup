import json
import urllib.parse
from collections.abc import Mapping
from unittest.mock import patch

import pytest

from dockerhub_cleanup.errors import CleanupError
from dockerhub_cleanup.http import HttpResponse
from dockerhub_cleanup.image_management import USER_AGENT, ImageManagementClient


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

    def __bool__(self) -> bool:
        return False


def response(payload: object) -> HttpResponse:
    return HttpResponse(200, {}, json.dumps(payload).encode())


def cursor_payload(cursor: object) -> list[object]:
    return [{"_1": -5 if cursor is None else 2}, "lastEvaluatedKey", cursor]


def test_cookie_is_required() -> None:
    with pytest.raises(CleanupError, match="DH_COOKIE"):
        ImageManagementClient("")


def test_client_creates_default_transport() -> None:
    transport = FakeTransport([response([])])
    with patch(
        "dockerhub_cleanup.image_management.UrllibTransport", return_value=transport
    ) as factory:
        client = ImageManagementClient("session=secret")
        assert client.all_digests("user", "app") == set()
    factory.assert_called_once_with(
        retries=2,
        retry_methods=frozenset({"GET", "POST"}),
    )


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
        "User-Agent": USER_AGENT,
        "Referer": "https://hub.docker.com/repository/docker/user%20name/app%2Fname/tags",
    }
    assert data is None


def test_discovery_follows_post_cursor_pages() -> None:
    digest_a = "sha256:" + "a" * 64
    digest_b = "sha256:" + "b" * 64
    transport = FakeTransport(
        [
            response([*cursor_payload("cursor one"), digest_a]),
            response([digest_b]),
        ]
    )
    client = ImageManagementClient("session=secret", transport=transport)

    assert client.all_digests("user", "app") == {digest_a, digest_b}

    method, _, headers, data = transport.requests[1]
    assert method == "POST"
    assert headers is not None
    assert headers["Content-Type"] == "application/x-www-form-urlencoded"
    assert headers["Referer"] == "https://hub.docker.com/repository/docker/user/app/tags"
    assert urllib.parse.parse_qs((data or b"").decode()) == {
        "intent": ["paginate"],
        "lastEvaluatedKey": ["cursor one"],
    }


def test_repeated_cursor_is_rejected() -> None:
    transport = FakeTransport(
        [
            response(cursor_payload("same")),
            response(cursor_payload("same")),
        ]
    )
    client = ImageManagementClient("session=secret", transport=transport)

    with pytest.raises(CleanupError, match="repeated a cursor"):
        client.all_digests("user", "app")


def test_terminal_cursor_field_without_value_stops_pagination() -> None:
    digest = "sha256:" + "a" * 64
    client = ImageManagementClient(
        "session=secret",
        transport=FakeTransport([response([*cursor_payload(None), digest])]),
    )

    assert client.all_digests("user", "app") == {digest}


def test_unrelated_encoded_fields_do_not_start_pagination() -> None:
    payload = [{"_1": 2}, "anotherField", "value"]
    client = ImageManagementClient("session=secret", transport=FakeTransport([response(payload)]))

    assert client.all_digests("user", "app") == set()


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [{"_1": 2}, "lastEvaluatedKey"],
        [{"_1": 2}, "lastEvaluatedKey", 3],
        [{"_1": True}, "lastEvaluatedKey"],
        [{"_1": -5.0}, "lastEvaluatedKey"],
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
