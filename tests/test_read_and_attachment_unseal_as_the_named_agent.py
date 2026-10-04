from __future__ import annotations

import httpx
import pytest

from agentbus_client import sealing
from agentbus_client.client import AgentBus

BASE = "https://bus.unseal.test"
TARGET = "target-agent-1"


def _bus(handler, agent: str | None) -> AgentBus:  # type: ignore[no-untyped-def]
    bus = AgentBus(api_key="ab_sk_unseal", base_url=BASE, agent=agent)
    bus._client.close()
    bus._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)
    return bus


@pytest.fixture
def target_public() -> str:
    _private, public = sealing.ensure_keypair(TARGET)
    return public


def test_an_operator_client_reads_a_named_agents_sealed_body(target_public: str) -> None:
    sealed = sealing.seal_for("for the target only", [target_public])
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("X-AgentBus-Agent", ""))
        return httpx.Response(200, json={"delivery_id": "d-1", "text_body": sealed})

    delivery = _bus(handler, None).read("d-1", agent=TARGET)
    assert seen == [TARGET]
    assert delivery["text_body"] == "for the target only"
    assert "sealed_unreadable" not in delivery


def test_an_operator_client_opens_a_named_agents_sealed_attachment(target_public: str) -> None:
    sealed = sealing.seal_for_bytes(b"\x00binary\xffpayload", [target_public])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/deliveries/d-2/attachments/0"
        assert request.headers.get("X-AgentBus-Agent") == TARGET
        return httpx.Response(200, content=sealed)

    assert _bus(handler, None).attachment("d-2", agent=TARGET) == b"\x00binary\xffpayload"


def test_without_a_named_agent_the_clients_own_agent_still_unseals(target_public: str) -> None:
    sealed = sealing.seal_for("own agent", [target_public])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"delivery_id": "d-3", "text_body": sealed})

    assert _bus(handler, TARGET).read("d-3")["text_body"] == "own agent"


def test_a_named_agent_without_the_key_says_so(target_public: str) -> None:
    sealed = sealing.seal_for("not yours", [target_public])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"delivery_id": "d-4", "text_body": sealed})

    delivery = _bus(handler, None).read("d-4", agent="someone-else-2")
    assert delivery["sealed_unreadable"] == "no sealing key on this machine"


async def test_the_async_client_unseals_as_the_named_agent(target_public: str) -> None:
    from agentbus_client.client import AsyncAgentBus

    body = sealing.seal_for("async target", [target_public])
    blob = sealing.seal_for_bytes(b"async-bytes", [target_public])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-AgentBus-Agent") == TARGET
        if request.url.path.endswith("/attachments/0"):
            return httpx.Response(200, content=blob)
        return httpx.Response(200, json={"delivery_id": "d-5", "text_body": body})

    bus = AsyncAgentBus(api_key="ab_sk_unseal", base_url=BASE, agent=None)
    await bus._client.aclose()
    bus._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE)
    try:
        assert (await bus.read("d-5", agent=TARGET))["text_body"] == "async target"
        assert await bus.attachment("d-5", agent=TARGET) == b"async-bytes"
    finally:
        await bus._client.aclose()
