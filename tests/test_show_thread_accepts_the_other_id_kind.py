"""#54 (SPECS/0054): `show <thread_id> --thread` and `thread <delivery_id>` each
try the other id kind once, on the failure path only. A ULID carries no type;
reported by a platform that paged `inbox --limit 300` because `show` said
not_found on a thread id."""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stderr, redirect_stdout

import pytest

from agentbus_client import cli
from agentbus_client.client import AgentBusError, NotFoundError

THREAD = {
    "thread": {"id": "th_1", "subject": "conv", "state": "open"},
    "messages": [
        {
            "id": "msg_1",
            "thread_seq": 1,
            "sender_display": "peer",
            "sender_address": "peer@x",
            "created_at": "2026-09-01T00:00:00Z",
            "text_body": "hello",
            "attachment_count": 0,
            "payload": None,
        }
    ],
}


class FakeBus:
    def __init__(self, *, deliveries: dict[str, dict], threads: dict[str, dict]):
        self._d = deliveries
        self._t = threads
        self.reads: list[str] = []
        self.threads: list[str] = []

    def read(self, ident, raw=False, **_):
        self.reads.append(ident)
        if ident in self._d:
            return self._d[ident]
        raise NotFoundError("no", code="not_found", status=404)

    def thread(self, ident):
        self.threads.append(ident)
        if ident in self._t:
            return self._t[ident]
        raise NotFoundError("no", code="not_found", status=404)


def _show(monkeypatch, bus, ident, *, thread: bool, json: bool = False):
    monkeypatch.setattr(cli._common, "_bus", lambda _args: bus)
    args = argparse.Namespace(delivery_id=ident, json=json, thread=thread, raw=False, agent=None)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.cmd_show(args)
    return rc, out.getvalue(), err.getvalue()


def _thread(monkeypatch, bus, ident):
    monkeypatch.setattr(cli._common, "_bus", lambda _args: bus)
    args = argparse.Namespace(thread_id=ident, json=False, agent=None)
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.cmd_thread(args)
    return rc, out.getvalue(), err.getvalue()


def test_show_thread_accepts_a_thread_id_and_names_the_direct_verb(monkeypatch):
    bus = FakeBus(deliveries={}, threads={"th_1": THREAD})
    rc, out, err = _show(monkeypatch, bus, "th_1", thread=True)
    assert rc == 0
    assert "# conv" in out and "msg_1" in out
    assert "agentbus thread th_1" in err
    assert bus.reads == ["th_1"] and bus.threads == ["th_1"]


def test_show_without_thread_flag_does_not_guess(monkeypatch):
    """A single-delivery read of a thread id has no sensible answer."""
    bus = FakeBus(deliveries={}, threads={"th_1": THREAD})
    with pytest.raises(NotFoundError):
        _show(monkeypatch, bus, "th_1", thread=False)
    assert bus.threads == []


def test_show_both_unknown_explains_the_two_kinds(monkeypatch):
    bus = FakeBus(deliveries={}, threads={})
    with pytest.raises(NotFoundError):
        _rc, _out, _err = _show(monkeypatch, bus, "zzz", thread=True)
    assert bus.reads == ["zzz"] and bus.threads == ["zzz"]


def test_show_fallback_costs_nothing_on_a_correct_call(monkeypatch):
    delivery = {
        "message_id": "msg_1",
        "thread_id": "th_1",
        "sender_display": "peer",
        "sender_address": "peer@x",
        "text_body": "hello",
        "recipients": [],
    }
    bus = FakeBus(deliveries={"del_1": delivery}, threads={"th_1": THREAD})
    rc, _, err = _show(monkeypatch, bus, "del_1", thread=True)
    assert rc == 0
    assert bus.reads == ["del_1"] and bus.threads == ["th_1"]
    assert "is a THREAD id" not in err


def test_thread_accepts_a_delivery_id(monkeypatch):
    bus = FakeBus(deliveries={"del_1": {"thread_id": "th_1"}}, threads={"th_1": THREAD})
    rc, out, err = _thread(monkeypatch, bus, "del_1")
    assert rc == 0
    assert "# conv" in out
    assert "is a DELIVERY id; its thread is th_1" in err
    assert bus.threads == ["del_1", "th_1"]


def test_thread_both_unknown_raises_the_original(monkeypatch):
    bus = FakeBus(deliveries={}, threads={})
    with pytest.raises(NotFoundError):
        _thread(monkeypatch, bus, "zzz")


def test_non_404_errors_are_not_swallowed(monkeypatch):
    class Boom(FakeBus):
        def read(self, ident, raw=False, **_):
            raise AgentBusError("down", code="server_error", status=500)

    with pytest.raises(AgentBusError) as info:
        _show(monkeypatch, Boom(deliveries={}, threads={}), "x", thread=True)
    assert info.value.status == 500
