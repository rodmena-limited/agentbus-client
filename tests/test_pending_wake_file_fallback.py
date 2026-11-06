"""`agentbus-hook pending` when the server cannot be asked.

The wake file written by `agentbus-hook notify` is then the only signal. The hook
prints what the file holds, says it may be stale, and keeps the file so the next
successful run can reconcile it. When the server answers, the server decides and
the file is cleared unprinted.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest

import agentbus_client
from agentbus_client.client.errors import TransportError
from agentbus_client.hooks import _state, _turn

AGENT = "wake-agent"
PROMPT = json.dumps({"session_id": "s", "prompt": "hello"})
UNREACHABLE = (
    "AgentBus: server unreachable; {count} captured arrival(s) "
    "in the local wake file (may already be read):"
)


class _FakeStdin(io.StringIO):
    def isatty(self) -> bool:
        return False


class _UnreachableBus:
    def __init__(self, *_a: Any, **_k: Any) -> None:
        pass

    def whoami(self) -> NoReturn:
        msg = "connection refused"
        raise TransportError(msg, code="transport_error")


class _EmptyInboxBus:
    def __init__(self, *_a: Any, **_k: Any) -> None:
        pass

    def whoami(self) -> dict[str, Any]:
        return {"unread": {"count": 0}}


@pytest.fixture(autouse=True)
def _agent_in_a_plain_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENTBUS_AGENT", AGENT)


def _capture(sender: str, subject: str, delivery: str) -> None:
    args = argparse.Namespace(subject=subject, sender=sender, delivery=delivery)
    assert _turn.notify(args) == 0


def _pending(monkeypatch: pytest.MonkeyPatch, bus: type) -> int:
    monkeypatch.setattr("agentbus_client.client.AgentBus", bus)
    monkeypatch.setattr("sys.stdin", _FakeStdin(PROMPT))
    return _turn.pending(cast("argparse.Namespace", None))


def test_an_unreachable_server_prints_every_captured_arrival(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _capture("alice", "first", "d1")
    _capture("bob", "second", "d2")

    assert _pending(monkeypatch, _UnreachableBus) == 0

    captured = capsys.readouterr()
    assert captured.out.splitlines() == [
        UNREACHABLE.format(count=2),
        "  alice: first  (agentbus show d1)",
        "  bob: second  (agentbus show d2)",
        "",
    ]
    assert "falling back to locally captured wakes" in captured.err


def test_the_wake_file_survives_the_fallback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The claim stays on disk so a later run can check it against the server."""
    _capture("alice", "first", "d1")
    wake = _state._wake_file(AGENT)
    before = wake.read_bytes()

    _pending(monkeypatch, _UnreachableBus)

    assert capsys.readouterr().out.startswith(UNREACHABLE.format(count=1))
    assert wake.read_bytes() == before


def test_the_listing_stops_at_ten_and_the_count_does_not(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for n in range(12):
        _capture("alice", f"subject-{n}", f"d{n}")

    _pending(monkeypatch, _UnreachableBus)

    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == UNREACHABLE.format(count=12)
    assert lines[1:] == [f"  alice: subject-{n}  (agentbus show d{n})" for n in range(10)] + [""]


def test_an_unreachable_server_with_nothing_captured_prints_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _pending(monkeypatch, _UnreachableBus) == 0
    assert capsys.readouterr().out == ""


def test_a_server_answering_zero_clears_the_file_unprinted(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _capture("alice", "already read", "d1")
    wake = _state._wake_file(AGENT)
    assert wake.exists()

    assert _pending(monkeypatch, _EmptyInboxBus) == 0

    assert capsys.readouterr().out == ""
    assert not wake.exists()


def _hook(env: dict[str, str], *argv: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "agentbus_client.hooks.claude_code", *argv],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )


def test_the_hook_commands_fall_back_on_a_refused_connection(tmp_path: Path) -> None:
    """`notify` and `pending` as the harness runs them, against a port nothing listens on."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "AGENTBUS_CONFIG_DIR": str(tmp_path / "config"),
        "AGENTBUS_AGENT": AGENT,
        "AGENTBUS_API_KEY": "ab_sk_test_secret",
        "AGENTBUS_BASE_URL": f"http://127.0.0.1:{port}",
        "AGENTBUS_SDK_MAX_RETRIES": "0",
        "PYTHONPATH": str(Path(agentbus_client.__file__).parents[1]),
    }

    wrote = _hook(env, "notify", "--subject", "first", "--sender", "alice", "--delivery", "d1")
    assert wrote.returncode == 0, wrote.stderr

    read = _hook(env, "pending", stdin=PROMPT)

    assert read.returncode == 0, read.stderr
    assert read.stdout.splitlines() == [
        UNREACHABLE.format(count=1),
        "  alice: first  (agentbus show d1)",
        "",
    ]
