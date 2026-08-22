"""#216: a reader must be told the conversation exists, and be able to read it.

THE TRAP THIS FILE EXISTS TO PIN, because it was nearly shipped.

The first implementation gated the "there are earlier messages" hint on
`thread_seq > 1`, which reads like a position in the conversation and is not one:
`thread_seq` counts THAT SENDER's own messages within the thread. Measured on a
real three-message exchange, the sequence numbers were 1, 2, 1 — a peer's first
reply to you is seq 1 while being the third message.

So the hint would have been silent on the single commonest case, someone
answering you, which is exactly the case where reading the rest matters. It would
have looked correct in every test written from the sender's point of view. The
regression tests below therefore assert from the RECIPIENT's side of a mixed
thread, which is where the old logic failed.
"""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout

from agentbus_client import cli


def _thread(n_messages: int) -> dict:
    """A thread whose thread_seq values are DELIBERATELY not positions.

    Two senders interleaved, exactly as a real exchange looks: 1, 2, 1.
    """
    seqs = [("alice", 1), ("alice", 2), ("bob", 1), ("alice", 3), ("bob", 2)][:n_messages]
    return {
        "thread": {"id": "th_1", "subject": "a conversation", "state": "open"},
        "messages": [
            {
                "id": f"msg_{i}",
                "thread_seq": seq,
                "sender_display": sender,
                "sender_address": f"{sender}@example.test",
                "created_at": f"2026-08-16T0{i}:00:00Z",
                "text_body": f"body of message {i}",
                "attachment_count": 1 if i == 2 else 0,
                "payload": {"k": "v"} if i == 3 else None,
                "payload_schema_ref": "schema://x" if i == 3 else None,
            }
            for i, (sender, seq) in enumerate(seqs, start=1)
        ],
    }


class FakeBus:
    def __init__(self, delivery: dict, thread: dict) -> None:
        self._delivery = delivery
        self._thread = thread
        self.thread_calls: list[str] = []

    def read(self, delivery_id: str, raw: bool = False) -> dict:
        return self._delivery

    def thread(self, thread_id: str) -> dict:
        self.thread_calls.append(thread_id)
        return self._thread


def _run_show(monkeypatch, delivery: dict, thread_data: dict, **flags) -> tuple[str, FakeBus]:
    bus = FakeBus(delivery, thread_data)
    monkeypatch.setattr(cli._common, "_bus", lambda _args: bus)
    args = argparse.Namespace(
        delivery_id="del_1", json=False, thread=flags.get("thread", False), agent=None
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cli.cmd_show(args) == 0
    return buf.getvalue(), bus


def _delivery(**over) -> dict:
    base = {
        "message_id": "msg_3",
        "thread_id": "th_1",
        "subject": "a conversation",
        "sender_display": "bob",
        "sender_address": "bob@example.test",
        "text_body": "body of message 3",
        "recipients": [{"recipient": "alice", "kind": "to"}],
        "your_role": "to",
        "thread_seq": 1,
        "thread_message_count": 5,
    }
    base.update(over)
    return base


def test_the_hint_fires_for_a_peers_first_reply(monkeypatch):
    """THE REGRESSION. thread_seq is 1 here — a `thread_seq > 1` gate says nothing,
    and this is the commonest message an agent ever reads."""
    out, _ = _run_show(monkeypatch, _delivery(thread_seq=1, thread_message_count=5), _thread(5))
    assert "4 other message(s) in this conversation" in out
    assert "READ THEM BEFORE REPLYING" in out
    assert "agentbus show del_1 --thread" in out


def test_the_hint_is_silent_on_a_one_message_thread(monkeypatch):
    """Advice printed on every message is advice that gets tuned out."""
    out, _ = _run_show(monkeypatch, _delivery(thread_message_count=1), _thread(1))
    assert "other message(s) in this conversation" not in out


def test_the_hint_is_silent_when_the_server_does_not_send_the_count(monkeypatch):
    """An older server omits thread_message_count. Degrade quietly rather than
    printing '-1 other messages' or crashing on None."""
    delivery = _delivery()
    del delivery["thread_message_count"]
    out, _ = _run_show(monkeypatch, delivery, _thread(5))
    assert "other message(s) in this conversation" not in out


def test_thread_flag_renders_every_message_in_order(monkeypatch):
    out, bus = _run_show(monkeypatch, _delivery(), _thread(5), thread=True)
    assert bus.thread_calls == ["th_1"]
    for i in range(1, 6):
        assert f"body of message {i}" in out
    # Positions are counted, NOT read from thread_seq (which goes 1,2,1,3,2).
    assert "[1/5]" in out and "[3/5]" in out and "[5/5]" in out
    assert "[4/4]" not in out


def test_all_is_accepted_as_an_alias_for_thread():
    parser = cli.build_parser()
    assert parser.parse_args(["show", "del_1", "--all"]).thread is True
    assert parser.parse_args(["show", "del_1", "--thread"]).thread is True
    assert parser.parse_args(["show", "del_1"]).thread is False


def test_every_message_carries_its_id_so_it_can_be_cited(monkeypatch):
    """Citing the delivery id instead is the mistake this client warns about
    elsewhere: delivery ids are per-recipient and do not resolve for the peer."""
    out, _ = _run_show(monkeypatch, _delivery(), _thread(5), thread=True)
    for i in range(1, 6):
        assert f"message msg_{i}" in out


def test_the_message_you_opened_is_marked(monkeypatch):
    out, _ = _run_show(monkeypatch, _delivery(message_id="msg_3"), _thread(5), thread=True)
    marked = [line for line in out.splitlines() if "the one you opened" in line]
    assert len(marked) == 1, out
    assert "[3/5]" in marked[0]


def test_attachments_and_payloads_are_announced_not_hidden(monkeypatch):
    """#212 applied to the thread: a message whose whole point was the thing it
    carried must not render as an empty remark."""
    out, _ = _run_show(monkeypatch, _delivery(), _thread(5), thread=True)
    assert "1 attachment(s)" in out
    assert "carries a structured payload (schema://x)" in out


def test_thread_view_states_how_many_messages_there_are(monkeypatch):
    out, _ = _run_show(monkeypatch, _delivery(), _thread(5), thread=True)
    assert "5 message(s)" in out
    assert "thread th_1" in out


# ------------------------------------------------------- sealed body rendering


def test_thread_render_says_so_instead_of_dumping_ciphertext():
    """A READER THAT CANNOT DECRYPT MUST SAY SO.

    `unseal_message`'s docstring states the rule and `show` honours it, but
    `_render_thread` printed `text_body` unconditionally — which on an
    un-openable message is the raw age armor. An operator reading a thread
    got a wall of base64 with no explanation: the exact "returning
    ciphertext as if it were content" failure the rule exists to prevent.

    Reachable in ordinary use: a thread you participate in can contain
    messages sealed only to OTHER recipients. Found on a live 5-message
    thread where 3 opened and 2 did not.
    """
    import contextlib
    import io

    from agentbus_client import cli

    armor = "-----BEGIN AGE ENCRYPTED FILE-----\nYWdlLWVuY3J5cHRpb24ub3JnL3Yx\n"
    result = {
        "thread": {"id": "01T", "subject": "s", "state": "open"},
        "messages": [
            {
                "id": "01M1",
                "sender_display": "peer",
                "created_at": "2026-08-18T00:00:00Z",
                "text_body": "readable body",
            },
            {
                "id": "01M2",
                "sender_display": "peer",
                "created_at": "2026-08-18T00:01:00Z",
                "text_body": armor,
                "sealed_unreadable": "sealed to keys this machine does not hold",
            },
        ],
    }

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        cli._render_thread(result)
    out = buf.getvalue()

    assert "readable body" in out, "the openable message must still render"
    assert "BEGIN AGE ENCRYPTED FILE" not in out, (
        "raw ciphertext was dumped into the thread view instead of an explanation"
    )
    assert "cannot read on this machine" in out
    assert "sealed to keys this machine does not hold" in out
