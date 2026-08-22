"""#123 — one arrival must produce exactly ONE announcement, not two.

`inject()` used to print the stdout notification line unconditionally and THEN
write the messaging socket, so a single delivery surfaced twice in one turn: an
injected user turn PLUS a Monitor task-notification. Reproduced independently on
two hosts by two operators. Harmless when a message is only read; a double-ACTION
hazard when it asks for work.

THE ASYMMETRY THIS FILE GUARDS. The obvious fix — drop the stdout line — was
only safe once it was established that the socket alone wakes an IDLE session,
which was measured (a detached socket-only write started a turn 45s after the
session went idle) rather than assumed. Had it been assumed and been wrong, the
duplicate would have been traded for a LOST WAKE. So the invariant is not "stdout
is gone", it is:

    stdout fires UNLESS the socket write completed.

which strictly dominates the old behaviour: no wake that used to happen can stop
happening. Both halves are tested here, because a test that only checked the
silent case would pass just as happily on a build that never announces anything.

A CHECK THAT CANNOT GO GREEN CANNOT GO RED. `test_fallback_*` are the
known-positives for `test_no_duplicate_when_socket_accepts`: they prove this
harness can observe the notice on stdout at all. Without them, an empty stdout
would be evidence of nothing.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentbus_client.hooks import claude_code

SENDER = "peer-abc123 via AgentBus"
SUBJECT = "Re: the thing"
DELIVERY = "01TESTDELIVERYID0000000000"


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        sender=SENDER,
        subject=SUBJECT,
        delivery=DELIVERY,
        seq=7,
        direction="bus",
        inbound_source=None,
    )


def _serve_once(path: str) -> tuple[threading.Thread, list[bytes]]:
    """A real AF_UNIX listener, so `sendall` completing means it actually did.

    Mocking the socket would make the central assertion self-confirming: the test
    would prove that inject calls the function the test replaced, not that a
    payload was accepted by a listener.
    """
    received: list[bytes] = []
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)

    def _run() -> None:
        conn, _ = srv.accept()
        with conn:
            chunks = []
            while True:
                b = conn.recv(65536)
                if not b:
                    break
                chunks.append(b)
            received.append(b"".join(chunks))
        srv.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t, received


def test_no_duplicate_when_socket_accepts(tmp_path, capsys, monkeypatch):
    """Socket took the payload -> stdout stays SILENT. This is the #123 fix."""
    sock_path = str(tmp_path / "s.sock")
    thread, received = _serve_once(sock_path)
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", sock_path)

    rc = claude_code.inject(_args())
    thread.join(timeout=5)

    assert rc == 0
    # The socket really received it — asserted on the VALUE, not just that a
    # connection happened, so a listener that accepted and got nothing fails.
    assert received, (
        "listener accepted no payload; the silent-stdout assertion below would be vacuous"
    )
    body = received[0].decode()
    assert DELIVERY in body and SENDER in body

    out = capsys.readouterr().out
    assert out == "", (
        f"arrival announced TWICE: socket delivered it and stdout also printed {out!r}"
    )


def test_fallback_when_no_socket_configured(capsys, monkeypatch):
    """No socket is a supported configuration — the notice MUST still be printed."""
    monkeypatch.delenv("CLAUDE_CODE_MESSAGING_SOCKET", raising=False)

    rc = claude_code.inject(_args())

    assert rc == 0
    out = capsys.readouterr().out
    assert out.strip() == f"[7] {SENDER}: {SUBJECT}", (
        "with no socket the stdout notification is the ONLY wake path; losing it "
        "is worse than the duplicate this change removes"
    )


def test_fallback_when_socket_is_dead(tmp_path, capsys, monkeypatch):
    """A configured-but-dead socket did NOT deliver, so stdout must fire."""
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", str(tmp_path / "gone.sock"))

    rc = claude_code.inject(_args())

    assert rc == 3, "a dead session socket must still report itself as a non-delivery"
    captured = capsys.readouterr()
    assert captured.out.strip() == f"[7] {SENDER}: {SUBJECT}"
    assert "NOT delivered" in captured.err


def test_notice_matches_print_line_format_without_seq(capsys, monkeypatch):
    """The fallback is only a fallback if its shape is the one watch emits."""
    monkeypatch.delenv("CLAUDE_CODE_MESSAGING_SOCKET", raising=False)
    args = _args()
    args.seq = None

    claude_code.inject(args)

    assert capsys.readouterr().out.strip() == f"{SENDER}: {SUBJECT}"


@pytest.mark.skipif(os.name == "nt", reason="AF_UNIX")
def test_socket_payload_is_a_single_newline_delimited_user_turn(tmp_path, monkeypatch):
    """Harness contract: one JSON object, one trailing newline, role=user."""
    import json

    sock_path = str(tmp_path / "s2.sock")
    thread, received = _serve_once(sock_path)
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", sock_path)

    claude_code.inject(_args())
    thread.join(timeout=5)

    assert received
    raw = received[0].decode()
    assert raw.endswith("\n")
    assert raw.count("\n") == 1, "more than one line would be more than one turn"
    obj = json.loads(raw)
    assert obj["type"] == "user"
    assert obj["message"]["role"] == "user"
    assert "not operator instructions" in obj["message"]["content"]
