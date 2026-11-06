"""#93 item 12: the re-wake poll absorbs failures, not requests to stop.

`resilient()` caught BaseException so a single 502 could not end the window, and
that also swallowed KeyboardInterrupt and SystemExit. watch.py re-raises in the
same place. These tests pin both halves: an ordinary failure still degrades to
an empty poll, and a stop request ends the hook with no traceback.
"""

from __future__ import annotations

import io
import json
import os
import signal
import socket
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any, NoReturn

import pytest

from agentbus_client import rewake
from agentbus_client.hooks import _antigravity, claude_code

SRC = Path(rewake.__file__).resolve().parents[1]
STOPS = [KeyboardInterrupt(), SystemExit(3)]


def _raising(exc: BaseException) -> Any:
    def _fail(*_a: Any, **_k: Any) -> NoReturn:
        raise exc

    return _fail


def _without_interrupt(hook: Any) -> int:
    """An interrupt that escapes would abort the whole pytest session."""
    try:
        rc: int = hook(None)
    except KeyboardInterrupt:
        pytest.fail("the interrupt escaped the hook")
    return rc


@pytest.mark.parametrize("stop", STOPS, ids=["KeyboardInterrupt", "SystemExit"])
def test_the_poll_lets_a_stop_request_through(
    stop: BaseException, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before the fix `poll()` returned "" and the loop kept polling."""
    monkeypatch.setattr(rewake, "_unread_text", _raising(stop))
    poll = rewake._build_resilient_poll("some-agent", wait=0)
    with pytest.raises(type(stop)):
        poll()


def test_the_poll_still_absorbs_an_ordinary_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The other direction: a real failure yields nothing and says why."""
    monkeypatch.setattr(rewake, "_unread_text", _raising(RuntimeError("bad day")))
    poll = rewake._build_resilient_poll("some-agent", wait=0)
    assert poll() == ""
    assert capsys.readouterr().err == (
        "agentbus rewake: poll failed (bad day); will retry on the next interval\n"
    )


def _armed_monitor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stop: BaseException) -> None:
    monkeypatch.setattr(claude_code, "_resolve_agent", lambda: "some-agent")
    monkeypatch.setenv("AGENTBUS_REWAKE_STATE", str(tmp_path / "ledger.txt"))
    monkeypatch.setenv("AGENTBUS_REWAKE_WINDOW", "3")
    monkeypatch.setattr(rewake, "_unread_text", _raising(stop))


def test_an_interrupted_monitor_exits_zero_and_quietly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit 2 is the only non-zero code this hook may use: it means new mail."""
    _armed_monitor(monkeypatch, tmp_path, KeyboardInterrupt())
    assert _without_interrupt(rewake.monitor) == 0
    assert capsys.readouterr() == ("", "")


def test_a_monitor_told_to_exit_keeps_the_requested_code(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _armed_monitor(monkeypatch, tmp_path, SystemExit(3))
    with pytest.raises(SystemExit) as caught:
        rewake.monitor()
    assert caught.value.code == 3


@pytest.mark.parametrize(
    ("hook", "expected"),
    [(_antigravity.agy_stop, {"decision": "stop"}), (_antigravity.agy_preinvocation, {})],
    ids=["agy_stop", "agy_preinvocation"],
)
def test_an_interrupted_antigravity_hook_still_answers_the_harness(
    hook: Any, expected: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """agy reads one JSON object from stdout, so no answer is a broken harness."""
    monkeypatch.setattr(_antigravity, "_payload", dict)
    monkeypatch.setattr(_antigravity, "_agent_for", lambda _p: "some-agent")
    monkeypatch.setattr(_antigravity, "_with_credential", lambda _a: True)
    monkeypatch.setattr(rewake, "poll_for_fresh_mail", _raising(KeyboardInterrupt()))
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert _without_interrupt(hook) == 0
    assert json.loads(buffer.getvalue()) == expected


HOOK_WITH_SIGINT_RESTORED = (
    "import signal\n"
    "signal.signal(signal.SIGINT, signal.default_int_handler)\n"
    "from agentbus_client.hooks.claude_code import main\n"
    "raise SystemExit(main(['monitor']))\n"
)


def test_sigint_ends_a_real_monitor_blocked_on_the_bus(tmp_path: Path) -> None:
    """A real process parked on a socket that never answers, stopped by SIGINT.

    The script restores the default handler because a test run in the background
    inherits SIGINT as ignored, and Python then never raises KeyboardInterrupt.
    """
    bus = socket.create_server(("127.0.0.1", 0))
    bus.settimeout(20)
    port = bus.getsockname()[1]
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC),
        "HOME": str(tmp_path),
        "AGENTBUS_AGENT": "some-agent",
        "AGENTBUS_API_KEY": "ab_sk_test_key",
        "AGENTBUS_BASE_URL": f"http://127.0.0.1:{port}",
        "AGENTBUS_REWAKE_STATE": str(tmp_path / "ledger.txt"),
        "AGENTBUS_REWAKE_WINDOW": "60",
    }
    child = subprocess.Popen(
        [sys.executable, "-c", HOOK_WITH_SIGINT_RESTORED],
        env=env,
        cwd=tmp_path,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        connection, _ = bus.accept()
        child.send_signal(signal.SIGINT)
        out, err = child.communicate(timeout=15)
    finally:
        child.kill()
        bus.close()
    connection.close()
    assert child.returncode == 0, err.decode()
    assert out == b""
    assert b"Traceback" not in err
