"""A server address that redirects (http -> https, meeting.lan -> a public name) is followed by the client."""

from __future__ import annotations

import httpx
import pytest

from meeting_notes.client import api
from meeting_notes.client.streamer import _to_ws_url

OLD = "http://meeting.lan"
NEW = "https://meeting.example.com"


@pytest.fixture(autouse=True)
def _fresh_moves():
    api._MOVED.clear()
    yield
    api._MOVED.clear()


def _client(handler, base=OLD) -> api.ServerClient:
    client = api.ServerClient(base, "tok")
    client._client.close()
    client._client = httpx.Client(base_url=client.base_url, transport=httpx.MockTransport(handler))
    return client


def _moving_server(seen):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), request.content if request.method == "POST" else b""))
        if str(request.url).startswith(OLD):
            return httpx.Response(308, headers={"location": NEW + request.url.raw_path.decode()})
        return httpx.Response(200, json={"ok": True, "status": "ok"})

    return handler


def test_a_redirected_call_is_repeated_at_the_new_address_and_remembered():
    seen = []
    client = _client(_moving_server(seen))

    assert client.health()["ok"] is True
    assert [url for _, url, _ in seen] == [OLD + "/health", NEW + "/health"]
    assert client.base_url == NEW
    assert api.server_base(OLD) == api.server_base(OLD + "/") == NEW
    assert api.ServerClient(OLD).base_url == NEW  # later clients start at the new address


def test_a_streamed_upload_is_not_lost_it_is_retried_at_the_new_address():
    seen = []
    client = _client(_moving_server(seen))

    with pytest.raises(api.ServerUnavailable, match="moved to https://meeting.example.com"):
        client._request("POST", "/v1/sessions/s1/tracks/mic", content=iter([b"abc"]))
    assert api.server_base(OLD) == NEW
    assert len(seen) == 1  # not replayed from a spent iterator; the queue's next try goes to NEW directly


def test_a_small_post_body_is_replayed():
    seen = []
    client = _client(_moving_server(seen))

    client._request("POST", "/v1/sessions/s1/rename", content=b'{"name":"x"}')
    assert [(m, url, body) for m, url, body in seen][-1] == ("POST", NEW + "/v1/sessions/s1/rename", b'{"name":"x"}')


@pytest.mark.parametrize(
    "location",
    ["https://meeting.example.com/login", "/login?next=/health"],
)
def test_a_redirect_that_is_not_the_same_call_elsewhere_is_not_followed(location):
    client = _client(lambda request: httpx.Response(302, headers={"location": location}))

    with pytest.raises(httpx.HTTPStatusError):
        client.health()
    assert api._MOVED == {}


def test_https_is_never_moved_to_plain_http():
    client = _client(
        lambda request: httpx.Response(301, headers={"location": "http://meeting.example.com/health"}), base=NEW
    )

    with pytest.raises(httpx.HTTPStatusError):
        client.health()
    assert api.server_base(NEW) == NEW


def test_an_unreachable_new_address_gives_the_configured_one_a_fresh_try():
    api._MOVED[OLD] = NEW

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    client = _client(down)
    assert client.base_url == NEW
    with pytest.raises(api.ServerUnavailable):
        client.health()
    assert api.server_base(OLD) == OLD


def test_websockets_connect_to_the_moved_address():
    assert _to_ws_url(OLD) == "ws://meeting.lan"
    api._MOVED[OLD] = NEW
    assert _to_ws_url(OLD) == "wss://meeting.example.com"
