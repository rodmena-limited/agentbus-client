"""#93 item 22: `memory` prints the oldest seq the server sent.

`_explain_limit` read `exc.payload`, which does not exist, so the hint never
printed. The bodies below were captured from the server's own serialiser,
`agentbus.errors.MemoryFull(...).to_response().body` at agentbus 4e422e3. The
server merges its extras flat into the problem+json body and drops any that are
None, so `oldest_seq` is a top-level key and is absent for an empty notebook.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from agentbus_client import cli
from agentbus_client.cli import _memory
from agentbus_client.client import AgentBus, AgentBusError
from agentbus_client.client.errors import _raise_for

WIRE_FULL = (
    '{"type":"https://agentbus.rodmena.co.uk/problems/memory-full","title":"Memory is full",'
    '"status":422,"code":"memory_full","detail":"memory full, cleanup: 131072 of 131072 '
    "stored bytes and 200 of 200 entries are in use, and this entry needs 402 more. Remove "
    "the oldest with `agentbus memory truncate --first N`, or one entry with `agentbus "
    'memory rm <seq>` (the oldest seq is 3).","entry_bytes":402,"oldest_seq":3,'
    '"bytes_used":131072,"bytes_limit":131072,"entries":200,"entries_limit":256}'
)
WIRE_NO_ENTRIES = (
    '{"type":"https://agentbus.rodmena.co.uk/problems/memory-full","title":"Memory is full",'
    '"status":422,"code":"memory_full","detail":"memory full, cleanup","entry_bytes":402,'
    '"bytes_used":131072,"bytes_limit":131072,"entries":0,"entries_limit":256}'
)


def _refusal(wire: str) -> AgentBusError:
    response = httpx.Response(422, content=wire, headers={"content-type": "application/json"})
    with pytest.raises(AgentBusError) as caught:
        _raise_for(response)
    return caught.value


def test_the_hint_names_the_seq_the_server_sent(capsys: pytest.CaptureFixture[str]) -> None:
    assert _memory._explain_limit(_refusal(WIRE_FULL)) == 4
    assert capsys.readouterr().err.splitlines() == [
        f"memory full: {json.loads(WIRE_FULL)['detail']}",
        "  free space with:  agentbus memory truncate --first 10   (oldest is seq 3)",
    ]


def test_the_hint_omits_the_seq_when_the_server_sent_none(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _memory._explain_limit(_refusal(WIRE_NO_ENTRIES)) == 4
    assert capsys.readouterr().err.splitlines() == [
        "memory full: memory full, cleanup",
        "  free space with:  agentbus memory truncate --first 10",
    ]


def test_a_real_memory_add_prints_the_hint_end_to_end(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Through main(), the SDK and `_raise_for`, with only HTTP stubbed."""
    posted: list[dict[str, Any]] = []

    def _server(request: httpx.Request) -> httpx.Response:
        posted.append(json.loads(request.content))
        return httpx.Response(422, content=WIRE_FULL)

    bus = AgentBus(api_key="ab_sk_stub", base_url="https://stub", agent="t")
    bus._client = httpx.Client(transport=httpx.MockTransport(_server), base_url="https://stub")
    monkeypatch.setattr(_memory, "_bus", lambda _args: bus)
    monkeypatch.setattr(bus, "_seal_to_self", lambda payload, _agent: payload)

    assert cli.main(["memory", "one more note"]) == 4
    assert posted == [{"text": "one more note"}]
    assert "(oldest is seq 3)" in capsys.readouterr().err
