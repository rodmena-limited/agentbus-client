"""#53 (SPECS/0053): a reply whose ONLY resolved recipient is you is refused.

KNOWN-POSITIVE from the reporting platform (thread 01M1EFPRKJ9V8MF6JKSDJJF7AT,
12:40Z): they replied to their own outbound message id — the one `agentbus
send` printed — and the reply landed in their own inbox, From=To=themselves,
while the counterparty waited. Reproduced read-only on 2026-09-01: the
server's resolve-reply on an own outbound id answers to=[you], cc=[].
"""

from __future__ import annotations

import argparse
import asyncio
import io
from contextlib import redirect_stderr, redirect_stdout

import pytest

from agentbus_client import cli
from agentbus_client.client import AgentBus, AsyncAgentBus, SelfReplyError
from agentbus_client.client._reply_guard import _refuse_self_reply
from agentbus_client.client.errors import AgentBusError

# ------------------------------------------------------------- the rule itself


def test_own_outbound_id_is_refused():
    with pytest.raises(SelfReplyError) as info:
        _refuse_self_reply({"to": ["me"], "cc": []}, "me", "msg_1", allow_self=False)
    assert info.value.message_id == "msg_1"
    assert info.value.acting == "me"
    assert info.value.code == "self_reply_refused"


def test_allow_self_sends():
    _refuse_self_reply({"to": ["me"], "cc": []}, "me", "msg_1", allow_self=True)


@pytest.mark.parametrize(
    "resolved",
    [
        {"to": ["peer"], "cc": []},  # the normal case
        {"to": ["me", "peer"], "cc": []},  # reply-all that includes others
        {"to": ["me"], "cc": ["peer"]},  # a cc reaches someone
        {"to": [], "cc": []},  # nobody: the server's problem, not this guard's
    ],
)
def test_anyone_but_you_passes(resolved):
    _refuse_self_reply(resolved, "me", "msg_1", allow_self=False)


def test_silent_when_the_recipient_set_is_unknown():
    """resolved None = the resolve route was refused/absent; nothing to judge."""
    _refuse_self_reply(None, "me", "msg_1", allow_self=False)


def test_silent_when_the_acting_agent_is_unknown():
    _refuse_self_reply({"to": ["me"], "cc": []}, None, "msg_1", allow_self=False)


# ------------------------------------------------------ both SDK reply paths


def _resolved_to_self(*_a, **_k):
    return {"text": "x"}, {"to": ["me"], "cc": [], "subject": "Re: x"}


def test_sync_reply_refuses_before_any_request(monkeypatch):
    bus = AgentBus(api_key="ab_sk_test_x", agent="me", base_url="http://127.0.0.1:9")
    posted: list[str] = []
    monkeypatch.setattr(bus, "_as_message_id", lambda ident, agent=None: ident)
    monkeypatch.setattr(bus, "_seal_if_needed", _resolved_to_self)
    monkeypatch.setattr(bus, "_sign_if_possible", lambda payload, agent, resolved: payload)
    monkeypatch.setattr(bus, "_request", lambda *a, **k: posted.append(a[1]) or {})
    with pytest.raises(SelfReplyError):
        bus.reply("msg_1", "hello")
    assert posted == [], "the reply must be refused BEFORE the POST"
    bus.reply("msg_1", "hello", allow_self=True)
    assert posted == ["/v1/messages/msg_1/reply"]


def test_async_reply_refuses_before_any_request(monkeypatch):
    bus = AsyncAgentBus(api_key="ab_sk_test_x", agent="me", base_url="http://127.0.0.1:9")
    posted: list[str] = []

    async def seal(*_a, **_k):
        return _resolved_to_self()

    async def request(*a, **k):
        posted.append(a[1])
        return {}

    monkeypatch.setattr(bus, "_seal_if_needed", seal)
    monkeypatch.setattr(bus, "_sign_if_possible", lambda payload, agent, resolved: payload)
    monkeypatch.setattr(bus, "_request", request)
    with pytest.raises(SelfReplyError):
        asyncio.run(bus.reply("msg_1", "hello"))
    assert posted == []
    asyncio.run(bus.reply("msg_1", "hello", allow_self=True))
    assert posted == ["/v1/messages/msg_1/reply"]


# ------------------------------------------------------------------ the CLI


class FakeBus:
    agent = "me"

    def __init__(self, thread_messages: list[dict], *, thread_lookup_fails: bool = False):
        self._messages = thread_messages
        self._thread_fails = thread_lookup_fails
        self.reply_kwargs: dict | None = None

    def read(self, ident, **_):
        if ident == "own_1":
            return {
                "message_id": "own_1",
                "thread_id": "th_1",
                "sender_address": "agentbus+me.ws@mail.test",
            }
        raise AgentBusError("no", code="not_found", status=404)

    def thread(self, thread_id):
        if self._thread_fails:
            raise AgentBusError("no", code="not_found", status=404)
        return {"thread": {"id": thread_id}, "messages": self._messages}

    def reply(self, message_id, text, **kwargs):
        self.reply_kwargs = kwargs
        if not kwargs.get("allow_self"):
            raise SelfReplyError("would reach only you", message_id=message_id, acting="me")
        return {"id": "out_1", "recipients": ["me"], "cc": []}


def _msg(i: int, sender: str) -> dict:
    return {
        "id": f"msg_{i}",
        "sender_address": f"agentbus+{sender}.ws@mail.test",
        "sender_display": f"{sender} via AgentBus",
    }


def _run(monkeypatch, bus: FakeBus, *, to_self: bool = False):
    monkeypatch.setattr(cli._common, "_bus", lambda _args: bus)
    args = argparse.Namespace(
        message_id="own_1",
        body="hello",
        reply_all=False,
        cc=[],
        priority=None,
        attach=[],
        subject=None,
        to_self=to_self,
        json=False,
        agent=None,
    )
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.cmd_reply(args)
    return rc, out.getvalue(), err.getvalue()


def test_cli_refuses_and_names_the_latest_message_from_the_other_party(monkeypatch):
    bus = FakeBus([_msg(1, "peer"), _msg(2, "me"), _msg(3, "peer"), _msg(4, "me")])
    rc, out, err = _run(monkeypatch, bus)
    assert rc == 2
    assert out == "", "nothing on stdout: a daemon must not log this as a send"
    assert "refused:" in err
    assert "agentbus reply msg_3 -b" in err, err  # latest from peer, not msg_4 (mine)
    assert "--to-self" in err


def test_cli_says_when_nobody_else_has_written(monkeypatch):
    bus = FakeBus([_msg(1, "me"), _msg(2, "me")])
    rc, _, err = _run(monkeypatch, bus)
    assert rc == 2
    assert "no message from anyone but you" in err


def test_cli_still_refuses_when_the_suggestion_lookup_fails(monkeypatch):
    """Never fall back to sending to self because the hint could not be built."""
    bus = FakeBus([], thread_lookup_fails=True)
    rc, _, err = _run(monkeypatch, bus)
    assert rc == 2
    assert "could not look up the thread" in err
    assert bus.reply_kwargs is not None and not bus.reply_kwargs.get("allow_self")


def test_cli_to_self_sends(monkeypatch):
    bus = FakeBus([])
    rc, out, _err = _run(monkeypatch, bus, to_self=True)
    assert rc == 0
    assert bus.reply_kwargs["allow_self"] is True
    assert "replied: out_1" in out
