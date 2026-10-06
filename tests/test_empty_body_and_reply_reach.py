from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from agentbus_client import cli
from agentbus_client.client import AgentBus, AsyncAgentBus, EmptyBodyError

BASE = "https://bus.empty.test"


def _sync_bus(requests: list[httpx.Request]) -> AgentBus:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "m-1", "recipients": ["bob-2"]})

    bus = AgentBus(api_key="ab_sk_empty", base_url=BASE, agent="alice-1")
    bus._client.close()
    bus._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)
    return bus


@pytest.mark.parametrize("text", ["", "   ", "\n\t \n", None])
def test_send_refuses_an_empty_body_before_any_request(text: Any) -> None:
    requests: list[httpx.Request] = []
    with pytest.raises(EmptyBodyError) as info:
        _sync_bus(requests).send(["bob-2"], subject="s", text=text)
    assert requests == []
    assert info.value.code == "empty_body_refused"


@pytest.mark.parametrize("text", ["", "  \n "])
def test_reply_refuses_an_empty_body_before_any_request(text: str) -> None:
    requests: list[httpx.Request] = []
    with pytest.raises(EmptyBodyError):
        _sync_bus(requests).reply("01MSGIDAAAAAAAAAAAAAAAAAAA", text)
    assert requests == []


def test_allow_empty_sends_it(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    _sync_bus(requests).send(["bob-2"], subject="s", text="", allow_empty=True)
    assert [r.url.path for r in requests if r.method == "POST"][-1] == "/v1/messages"


def test_an_attachment_or_a_payload_or_html_is_not_an_empty_message(tmp_path: Path) -> None:
    f = tmp_path / "a.txt"
    f.write_text("data")
    requests: list[httpx.Request] = []
    bus = _sync_bus(requests)
    bus.send(["bob-2"], subject="s", text="", attachments=[str(f)])
    bus.send(["bob-2"], subject="s", text=None, payload={"k": 1})
    bus.send(["bob-2"], subject="s", text="", html="<p>hi</p>")
    assert len([r for r in requests if r.url.path == "/v1/messages"]) == 3


async def test_the_async_client_refuses_an_empty_body_too() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "m-1"})

    bus = AsyncAgentBus(api_key="ab_sk_empty", base_url=BASE, agent="alice-1")
    await bus._client.aclose()
    bus._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=BASE)
    try:
        with pytest.raises(EmptyBodyError):
            await bus.send(["bob-2"], subject="s", text=" ")
        with pytest.raises(EmptyBodyError):
            await bus.reply("01MSGIDAAAAAAAAAAAAAAAAAAA", "")
        assert requests == []
    finally:
        await bus._client.aclose()


class _ReplyBus:
    agent = "alice-1"

    def __init__(self, plain: list[str], everyone: list[str], fail: bool = False) -> None:
        self._sets = {False: plain, True: everyone}
        self._fail = fail
        self.replies: list[dict[str, Any]] = []

    def reply_recipients(self, message_id: str, *, reply_all: bool = False, agent: Any = None):
        if self._fail:
            raise RuntimeError("resolver down")
        return {"to": list(self._sets[reply_all]), "cc": []}

    def reply(self, message_id: str, text: str, **kw: Any) -> dict[str, Any]:
        if not (text or "").strip() and not kw.get("attachments") and not kw.get("allow_empty"):
            raise EmptyBodyError("refusing to send an empty message")
        self.replies.append({"text": text, **kw})
        targets = self._sets[bool(kw.get("reply_all"))]
        return {"id": "r-1", "recipients": targets}


def _cli(monkeypatch: pytest.MonkeyPatch, bus: _ReplyBus, argv: list[str]) -> int:
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    monkeypatch.setattr(cli._common, "_as_message_id", lambda _b, ident: ident)
    from agentbus_client.cli import _compose

    monkeypatch.setattr(_compose, "_as_message_id", lambda _b, ident: ident)
    return int(cli.main(argv))


def test_a_plain_reply_names_who_it_leaves_out(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bus = _ReplyBus(plain=["carol-3"], everyone=["carol-3", "dave-4", "alice-1"])
    assert _cli(monkeypatch, bus, ["reply", "01MSG", "-b", "hello"]) == 0
    err = capsys.readouterr().err
    assert "this reply does NOT go to dave-4, who were on the original" in err
    assert "alice-1" not in err.split("does NOT go to")[1].split(",")[0]
    assert bus.replies[0]["reply_all"] is False


def test_an_explicit_cc_is_not_reported_as_left_out(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bus = _ReplyBus(plain=["carol-3"], everyone=["carol-3", "dave-4"])
    assert _cli(monkeypatch, bus, ["reply", "01MSG", "-b", "hi", "--cc", "dave-4"]) == 0
    assert "does NOT go to" not in capsys.readouterr().err


def test_reply_all_that_includes_you_says_so(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bus = _ReplyBus(plain=["alice-1"], everyone=["alice-1", "dave-4"])
    assert _cli(monkeypatch, bus, ["reply", "01MSG", "--all", "-b", "hello"]) == 0
    err = capsys.readouterr().err
    assert "addresses this reply-all to you (alice-1) too" in err
    assert "does NOT go to" not in err


def test_nothing_left_out_prints_no_note(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bus = _ReplyBus(plain=["carol-3"], everyone=["carol-3"])
    assert _cli(monkeypatch, bus, ["reply", "01MSG", "-b", "hello"]) == 0
    assert "note:" not in capsys.readouterr().err


def test_an_unreachable_resolver_is_silent_and_the_reply_still_goes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bus = _ReplyBus(plain=["carol-3"], everyone=["carol-3", "dave-4"], fail=True)
    assert _cli(monkeypatch, bus, ["reply", "01MSG", "-b", "hello"]) == 0
    assert "note:" not in capsys.readouterr().err
    assert len(bus.replies) == 1


def test_an_empty_cli_reply_is_refused_with_exit_2(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bus = _ReplyBus(plain=["carol-3"], everyone=["carol-3", "dave-4"])
    assert _cli(monkeypatch, bus, ["reply", "01MSG", "--all", "-b", "  "]) == 2
    err = capsys.readouterr().err
    assert err.startswith("refused: ")
    assert "note:" not in err
    assert bus.replies == []


def test_allow_empty_on_the_cli_sends_it(monkeypatch: pytest.MonkeyPatch) -> None:
    bus = _ReplyBus(plain=["carol-3"], everyone=["carol-3"])
    assert _cli(monkeypatch, bus, ["reply", "01MSG", "-b", "", "--allow-empty"]) == 0
    assert bus.replies[0]["allow_empty"] is True
