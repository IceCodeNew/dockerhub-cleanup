import json
import urllib.error
from collections.abc import Mapping
from email.message import Message
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from dockerhub_cleanup.dockerhub import HUB_API, DockerHubClient
from dockerhub_cleanup.errors import CleanupError
from dockerhub_cleanup.http import HttpResponse, UrllibTransport


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


def response(payload: object, *, status: int = 200) -> HttpResponse:
    return HttpResponse(status=status, headers={}, body=json.dumps(payload).encode())


def client_with_responses(*responses: HttpResponse) -> tuple[DockerHubClient, FakeTransport]:
    transport = FakeTransport([response({"access_token": "jwt"}), *responses])
    return DockerHubClient("user", "secret", transport=transport), transport


def test_client_creates_default_transport() -> None:
    transport = FakeTransport([response({"access_token": "jwt"})])
    with patch("dockerhub_cleanup.dockerhub.UrllibTransport", return_value=transport) as factory:
        DockerHubClient("user", "secret")
    factory.assert_called_once_with()


def test_authentication_sends_credentials_only_in_request_body() -> None:
    client, transport = client_with_responses()

    method, url, headers, data = transport.requests[0]
    assert method == "POST"
    assert url == f"{HUB_API}/v2/auth/token"
    assert headers == {"Content-Type": "application/json"}
    assert json.loads(data or b"") == {"identifier": "user", "secret": "secret"}
    assert client.auth_headers == {
        "Authorization": "Bearer jwt",
        "Accept": "application/json",
    }


@pytest.mark.parametrize("payload", [{}, [], {"access_token": 3}, {"access_token": ""}])
def test_authentication_rejects_missing_access_token(payload: object) -> None:
    with pytest.raises(CleanupError, match="no access token"):
        DockerHubClient("user", "secret", transport=FakeTransport([response(payload)]))


def test_repositories_follow_pagination_and_encode_namespace() -> None:
    second_url = f"{HUB_API}/page/2"
    client, transport = client_with_responses(
        response({"results": [{"name": "one"}], "next": second_url}),
        response({"results": [{"name": "two"}], "next": None}),
    )

    assert client.repositories("user name") == ["one", "two"]
    assert transport.requests[1][1].endswith("/user%20name/repositories?page_size=100")
    assert transport.requests[2][1] == second_url


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ([], "paginated response"),
        ({}, "paginated response"),
        ({"results": ["bad"], "next": None}, "paginated item"),
        ({"results": [], "next": 3}, "pagination URL"),
        ({"results": [{"other": "value"}], "next": None}, "without a name"),
    ],
)
def test_repositories_validate_pagination(payload: object, message: str) -> None:
    client, _ = client_with_responses(response(payload))
    with pytest.raises(CleanupError, match=message):
        client.repositories("user")


def test_repositories_reject_non_string_object_keys() -> None:
    client, _ = client_with_responses()
    payload = {"results": [{3: "bad"}], "next": None}
    with (
        patch.object(client, "_json_request", return_value=payload),
        pytest.raises(CleanupError, match="paginated item"),
    ):
        client.repositories("user")


def test_repositories_reject_repeated_page_url() -> None:
    repeated = f"{HUB_API}/v2/namespaces/user/repositories?page_size=100"
    client, _ = client_with_responses(response({"results": [], "next": repeated}))
    with pytest.raises(CleanupError, match="repeated a URL"):
        client.repositories("user")


def test_repositories_reject_cross_origin_pagination() -> None:
    client, transport = client_with_responses(
        response({"results": [], "next": "https://example.test/steal-token"})
    )

    with pytest.raises(CleanupError, match="untrusted URL"):
        client.repositories("user")
    assert len(transport.requests) == 2


def test_repositories_accept_relative_hub_pagination() -> None:
    client, transport = client_with_responses(
        response({"results": [{"name": "one"}], "next": "/v2/page/2"}),
        response({"results": [{"name": "two"}], "next": None}),
    )

    assert client.repositories("user") == ["one", "two"]
    assert transport.requests[2][1] == f"{HUB_API}/v2/page/2"


def test_tags_parse_metadata_and_encode_path_parts() -> None:
    client, transport = client_with_responses(
        response(
            {
                "results": [
                    {
                        "name": "release/test",
                        "digest": "sha256:abc",
                        "tag_last_pulled": "2026-01-01T00:00:00Z",
                        "tag_last_pushed": None,
                    }
                ],
                "next": None,
            }
        )
    )

    tags = client.tags("user name", "app/name")
    assert tags[0].name == "release/test"
    assert tags[0].last_pulled is not None
    assert tags[0].last_pushed is None
    assert "/user%20name/repositories/app%2Fname/tags" in transport.requests[1][1]


@pytest.mark.parametrize(
    ("item", "message"),
    [
        ({"name": "tag"}, "incomplete tag"),
        ({"name": "tag", "digest": "digest", "tag_last_pulled": 3}, "last_pulled"),
        ({"name": "tag", "digest": "digest", "tag_last_pushed": 3}, "last_pushed"),
        (
            {"name": "tag", "digest": "digest", "tag_last_pulled": "not-a-time"},
            "last_pulled",
        ),
    ],
)
def test_tags_validate_metadata(item: object, message: str) -> None:
    client, _ = client_with_responses(response({"results": [item], "next": None}))
    with pytest.raises(CleanupError, match=message):
        client.tags("user", "app")


def test_delete_tag_uses_encoded_hub_endpoint() -> None:
    client, transport = client_with_responses(HttpResponse(204, {}, b""))

    client.delete_tag("user name", "app/name", "release/test")

    method, url, headers, data = transport.requests[1]
    assert method == "DELETE"
    assert url.endswith("/user%20name/repositories/app%2Fname/tags/release%2Ftest")
    assert headers == client.auth_headers
    assert data is None


def test_invalid_json_is_reported_without_response_body() -> None:
    transport = FakeTransport([HttpResponse(200, {}, b"not-json-secret")])
    with pytest.raises(CleanupError, match="invalid JSON") as raised:
        DockerHubClient("user", "secret", transport=transport)
    assert "not-json-secret" not in str(raised.value)


def test_urllib_transport_returns_response() -> None:
    raw_response = MagicMock()
    raw_response.__enter__.return_value = raw_response
    raw_response.status = 200
    raw_response.headers.items.return_value = [("X-Test", "yes")]
    raw_response.read.return_value = b"{}"

    with patch("urllib.request.urlopen", return_value=raw_response) as urlopen:
        result = UrllibTransport(timeout=5).request(
            "POST", "https://example.test/path", headers={"X": "y"}, data=b"body"
        )

    assert result == HttpResponse(200, {"X-Test": "yes"}, b"{}")
    request = urlopen.call_args.args[0]
    assert request.method == "POST"
    assert request.data == b"body"
    assert urlopen.call_args.kwargs == {"timeout": 5}


def test_urllib_transport_sanitizes_http_errors() -> None:
    headers = Message()
    error = urllib.error.HTTPError(
        "https://example.test", 401, "unauthorized", headers, BytesIO(b"secret response")
    )
    with (
        patch("urllib.request.urlopen", side_effect=error),
        pytest.raises(CleanupError, match="HTTP 401") as raised,
    ):
        UrllibTransport().request("GET", "https://example.test")
    assert "secret response" not in str(raised.value)


def test_urllib_transport_reports_network_errors() -> None:
    with (
        patch("urllib.request.urlopen", side_effect=urllib.error.URLError("offline")),
        pytest.raises(CleanupError, match="offline"),
    ):
        UrllibTransport().request("GET", "https://example.test")


def test_urllib_transport_reports_timeouts() -> None:
    with (
        patch("urllib.request.urlopen", side_effect=TimeoutError),
        pytest.raises(CleanupError, match="timed out"),
    ):
        UrllibTransport().request("GET", "https://example.test")
