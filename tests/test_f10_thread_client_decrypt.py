"""F10 (issuedb #11): `agentbus thread --json` (and the SDK's thread())
unseal each message client-side, since the server holds ciphertext by
design on an encrypted workspace.

Reported by peer agentbus-ui-c760a1 (batch #2, finding #10). Backend
correction (thread 01M06KND89GDQ7W9MEVJ97JRNK): this is client-lane.
"""

from __future__ import annotations

from unittest.mock import patch

from agentbus_client.client import AgentBus, AsyncAgentBus


def _sync_bus() -> AgentBus:
    return AgentBus(api_key="ab_sk_test_test", agent="test-agent")


def _async_bus() -> AsyncAgentBus:
    return AsyncAgentBus(api_key="ab_sk_test_test", agent="test-agent")


def test_thread_calls_unseal_on_every_message() -> None:
    bus = _sync_bus()
    server_reply = {
        "thread_id": "01T",
        "messages": [
            {"id": "01M1", "text_body": "sealed-body-1"},
            {"id": "01M2", "text_body": "sealed-body-2"},
            {"id": "01M3", "text_body": "sealed-body-3"},
        ],
    }
    called_with: list[str] = []

    def fake_unseal(msg):
        called_with.append(msg.get("text_body"))
        msg["text_body"] = f"opened-{msg['id']}"
        msg["sealed_opened"] = True
        return msg

    with (
        patch.object(bus, "_request", return_value=server_reply),
        patch.object(bus, "unseal_message", side_effect=fake_unseal),
    ):
        result = bus.thread("01T")

    assert called_with == ["sealed-body-1", "sealed-body-2", "sealed-body-3"]
    assert [m["text_body"] for m in result["messages"]] == [
        "opened-01M1",
        "opened-01M2",
        "opened-01M3",
    ]


def test_thread_survives_a_response_with_no_messages_key() -> None:
    """Regression guard: an empty or unusual server payload must not crash."""
    bus = _sync_bus()
    with (
        patch.object(bus, "_request", return_value={"thread_id": "01T"}),
        patch.object(bus, "unseal_message") as unseal,
    ):
        result = bus.thread("01T")
    assert result == {"thread_id": "01T"}
    assert unseal.call_count == 0


def test_thread_uses_the_same_unseal_helper_as_read() -> None:
    """Both surfaces must go through unseal_message so a sealed body handled
    correctly on `show` is also handled correctly on `thread --json`."""
    bus = _sync_bus()
    # The helper marks damaged bodies with `sealed_unreadable` — a thread
    # response with a damaged message must carry the SAME field on the SAME
    # message dict, not a separate error format.

    def damaged_unseal(msg):
        msg["sealed_unreadable"] = "the sealed body is damaged: not-age"
        return msg

    with (
        patch.object(
            bus,
            "_request",
            return_value={"messages": [{"id": "01M", "text_body": "-----BEGIN AGE"}]},
        ),
        patch.object(bus, "unseal_message", side_effect=damaged_unseal),
    ):
        result = bus.thread("01T")
    assert result["messages"][0].get("sealed_unreadable") == "the sealed body is damaged: not-age"


async def test_async_thread_also_unseals_each_message() -> None:
    """The async client must have parity — a caller cannot know which path is
    behind the SDK."""
    bus = _async_bus()

    async def fake_request(*a, **kw):
        return {
            "thread_id": "01T",
            "messages": [
                {"id": "01M1", "text_body": "sealed"},
                {"id": "01M2", "text_body": "sealed"},
            ],
        }

    def fake_unseal(msg):
        msg["text_body"] = "opened"
        return msg

    with (
        patch.object(bus, "_request", side_effect=fake_request),
        patch.object(bus, "unseal_message", side_effect=fake_unseal),
    ):
        result = await bus.thread("01T")
    assert all(m["text_body"] == "opened" for m in result["messages"])
