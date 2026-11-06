"""#94: a 2xx whose body is not JSON reaches the caller as an AgentBusError.

A captive portal answers 200 with HTML. Before this, the decoder's error escaped
every `except AgentBusError` in the SDK and the CLI.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from agentbus_client.client import AgentBus, AgentBusError, AsyncAgentBus

BASE = "http://bus.invalid"
DEEP = b"[" * 200_000 + b"]" * 200_000
UNREADABLE = [b"<html>portal</html>", b"{not json", DEEP, b"\xff\xfe"]


class _Server:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.seen: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request.url.path)
        return httpx.Response(200, content=self.body)


def _sync(server: _Server, method: str) -> object:
    bus = AgentBus(api_key="ab_sk_x", base_url=BASE)
    bus._client = httpx.Client(base_url=BASE, transport=httpx.MockTransport(server))
    return bus._request(method, "/v1/whoami")


def _async(server: _Server, method: str) -> object:
    async def scenario() -> object:
        bus = AsyncAgentBus(api_key="ab_sk_x", base_url=BASE)
        await bus._client.aclose()
        bus._client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(server))
        try:
            return await bus._request(method, "/v1/whoami")
        finally:
            await bus.aclose()

    return asyncio.run(scenario())


TWINS = [_sync, _async]


@pytest.mark.parametrize("call", TWINS, ids=["sync", "async"])
@pytest.mark.parametrize("body", UNREADABLE, ids=["html", "broken", "deep", "bytes"])
@pytest.mark.parametrize("resilience", ["1", "0"])
def test_an_unreadable_2xx_is_a_typed_error_and_is_not_repeated(
    monkeypatch: pytest.MonkeyPatch, call: object, body: bytes, resilience: str
) -> None:
    monkeypatch.setenv("AGENTBUS_SDK_RESILIENCE", resilience)
    server = _Server(body)
    with pytest.raises(AgentBusError) as caught:
        call(server, "POST")  # type: ignore[operator]
    assert caught.value.status == 200
    assert caught.value.code == "unreadable_response"
    assert "200" in str(caught.value)
    assert len(server.seen) == 1


@pytest.mark.parametrize("call", TWINS, ids=["sync", "async"])
@pytest.mark.parametrize(
    ("body", "expected"),
    [(b'{"ok": 1}', {"ok": 1}), (b"[1, 2]", [1, 2]), (b"", None), (b"null", None)],
    ids=["object", "array", "empty", "null"],
)
def test_a_readable_2xx_comes_back_unchanged(call: object, body: bytes, expected: object) -> None:
    assert call(_Server(body), "GET") == expected  # type: ignore[operator]
