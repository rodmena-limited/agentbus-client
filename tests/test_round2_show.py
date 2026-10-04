from __future__ import annotations

import io
import json
import sys

import pytest

from agentbus_client import cli
from agentbus_client.client import NotFoundError

BODY = "héllo wörld — ünïcode\nline two\n"


def _delivery(**over) -> dict:
    base = {
        "message_id": "msg_1",
        "thread_id": "th_1",
        "subject": "quarterly numbers",
        "sender_display": "Peer One",
        "sender_address": "peer@example.test",
        "text_body": BODY,
        "sealed": True,
        "recipients": [
            {"recipient": "me@example.test", "kind": "to"},
            {"recipient": "boss@example.test", "kind": "to"},
            {"recipient": "audit@example.test", "kind": "cc"},
        ],
        "your_role": "to",
    }
    base.update(over)
    return base


class _Bus:
    def __init__(self, delivery: dict, thread: dict | None = None, missing: bool = False) -> None:
        self.delivery = delivery
        self.thread_result = thread or {"thread_id": "th_1", "messages": []}
        self.missing = missing
        self.calls: list[tuple] = []

    def read(self, *args, **kwargs):
        self.calls.append(("read", args, kwargs))
        if self.missing:
            raise NotFoundError("no such delivery")
        return self.delivery

    def thread(self, *args, **kwargs):
        self.calls.append(("thread", args, kwargs))
        return self.thread_result


def _install(monkeypatch: pytest.MonkeyPatch, bus: _Bus) -> list:
    given: list = []

    def _factory(args):
        given.append(args)
        return bus

    monkeypatch.setattr(cli._common, "_bus", _factory)
    return given


def test_raw_writes_utf8_bytes_even_on_a_latin1_stdout(monkeypatch, capsys) -> None:
    bus = _Bus(_delivery())
    given = _install(monkeypatch, bus)
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="latin-1", newline="\r\n"))
    assert cli.main(["show", "01DEL", "--raw"]) == 0
    sys.stdout.flush()
    assert raw.getvalue() == BODY.encode("utf-8")
    assert bus.calls == [("read", ("01DEL",), {"raw": True})]
    assert given[0].command == "show"
    assert capsys.readouterr().err == ""


def test_raw_unsealed_warns_on_stderr_only(monkeypatch, capsys) -> None:
    _install(monkeypatch, _Bus(_delivery(sealed=False, text_body="plain é")))
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="latin-1"))
    assert cli.main(["show", "01DEL", "--raw"]) == 0
    sys.stdout.flush()
    assert raw.getvalue() == "plain é".encode()
    assert "is NOT sealed" in capsys.readouterr().err


def test_raw_json_prints_the_delivery(monkeypatch, capsys) -> None:
    delivery = _delivery()
    _install(monkeypatch, _Bus(delivery))
    assert cli.main(["show", "01DEL", "--raw", "--json"]) == 0
    out = capsys.readouterr().out
    assert out == json.dumps(delivery, indent=2, default=str) + "\n"


def test_raw_without_body_exits_one(monkeypatch, capsys) -> None:
    _install(monkeypatch, _Bus(_delivery(text_body=None)))
    assert cli.main(["show", "01DEL", "--raw"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "this delivery has no stored text body\n"


def test_plain_json_prints_the_delivery(monkeypatch, capsys) -> None:
    delivery = _delivery(payload={"total": 3})
    bus = _Bus(delivery)
    _install(monkeypatch, bus)
    assert cli.main(["show", "01DEL", "--json"]) == 0
    out = capsys.readouterr().out
    assert json.loads(out) == delivery
    assert out == json.dumps(delivery, indent=2, default=str) + "\n"
    assert bus.calls == [("read", ("01DEL",), {"raw": False})]


def test_thread_json_prints_the_thread(monkeypatch, capsys) -> None:
    thread = {"thread_id": "th_1", "messages": [{"id": "m1", "subject": "s"}]}
    bus = _Bus(_delivery(), thread=thread)
    _install(monkeypatch, bus)
    assert cli.main(["show", "01DEL", "--thread", "--json"]) == 0
    out = capsys.readouterr().out
    assert out == json.dumps(thread, indent=2, default=str) + "\n"
    assert bus.calls == [("read", ("01DEL",), {"raw": False}), ("thread", ("th_1",), {})]


def test_thread_id_fallback_json(monkeypatch, capsys) -> None:
    thread = {"thread_id": "01THR", "messages": []}
    bus = _Bus(_delivery(), thread=thread, missing=True)
    _install(monkeypatch, bus)
    assert cli.main(["show", "01THR", "--thread", "--json"]) == 0
    captured = capsys.readouterr()
    assert captured.out == json.dumps(thread, indent=2, default=str) + "\n"
    assert "is a THREAD id" in captured.err
    assert bus.calls[1] == ("thread", ("01THR",), {})


def test_envelope_lines(monkeypatch, capsys) -> None:
    delivery = _delivery(
        auth_verdicts={"dkim": "pass"},
        payload={"total": 3},
        payload_schema_ref="schema://numbers/v1",
        signature=None,
    )
    _install(monkeypatch, _Bus(delivery))
    assert cli.main(["show", "01DEL"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[:7] == [
        "From:    Peer One",
        "To:      me@example.test, boss@example.test",
        "Cc:      audit@example.test",
        "You:     TO  (you are expected to act)",
        "Subject: quarterly numbers",
        "Thread:  th_1",
        "Auth:    {'dkim': 'pass'}",
    ]
    assert lines[7].startswith("Signed:  no")
    payload_at = lines.index("-- payload (schema://numbers/v1):")
    assert lines[payload_at + 1 : payload_at + 4] == ["{", '  "total": 3', "}"]
    assert "reply in this thread:  agentbus reply 01DEL -b '...'" in lines
    assert "reply to everyone:     agentbus reply 01DEL --all -b '...'" in lines


def test_cc_reader_and_address_fallback(monkeypatch, capsys) -> None:
    delivery = _delivery(
        sender_display="",
        your_role="cc",
        recipients=[{"recipient": "solo@example.test", "kind": "cc"}],
    )
    _install(monkeypatch, _Bus(delivery))
    assert cli.main(["show", "01DEL"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[:4] == [
        "From:    peer@example.test",
        "Cc:      solo@example.test",
        "You:     CC  (copied for information)",
        "Subject: quarterly numbers",
    ]
    assert not any(line.startswith("To:") for line in lines)
    assert not any(line.startswith("reply to everyone") for line in lines)
    assert not any(line.startswith("Auth:") for line in lines)


def test_payload_without_ref_and_no_payload(monkeypatch, capsys) -> None:
    _install(monkeypatch, _Bus(_delivery(payload=[1, "two"])))
    assert cli.main(["show", "01DEL"]) == 0
    lines = capsys.readouterr().out.splitlines()
    at = lines.index("-- payload:")
    assert lines[at + 1 : at + 5] == ["[", "  1,", '  "two"', "]"]
    _install(monkeypatch, _Bus(_delivery()))
    assert cli.main(["show", "01DEL"]) == 0
    out = capsys.readouterr().out
    assert "-- payload" not in out
    assert "null" not in out
