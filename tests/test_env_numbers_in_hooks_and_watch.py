"""#94 item 4: the timing settings read outside the SDK.

AGENTBUS_REWAKE_WINDOW, AGENTBUS_REWAKE_INTERVAL and AGENTBUS_EXEC_TIMEOUT went
through a bare `int()` or `float()`. A value that does not parse ended the wake
hook, and a value of 0 made every `--exec` time out before the command ran.
"""

from __future__ import annotations

import io
import json
import time
from contextlib import redirect_stdout
from typing import TYPE_CHECKING, Any

import pytest

from agentbus_client import _env_numbers, _watch_handlers, rewake
from agentbus_client.hooks import _antigravity, claude_code
from agentbus_client.onboarding._paths import AGY_WAKE_WINDOW_SEC

if TYPE_CHECKING:
    from pathlib import Path

WINDOW = "AGENTBUS_REWAKE_WINDOW"
INTERVAL = "AGENTBUS_REWAKE_INTERVAL"
TIMEOUT = "AGENTBUS_EXEC_TIMEOUT"
HUGE = "1" + "0" * 400

MONITOR_CASES = [
    pytest.param(None, None, 600, 15, id="unset"),
    pytest.param("0", "1", 0, 1, id="one-pass"),
    pytest.param("120", "30", 120, 30, id="usable"),
    pytest.param("abc", "abc", 600, 15, id="words"),
    pytest.param("", "", 600, 15, id="empty"),
    pytest.param("1.5", "1.5", 600, 15, id="fractions"),
    pytest.param("-1", "-1", 600, 1, id="negative"),
    pytest.param("0", "0", 0, 1, id="zero-interval-floor"),
    pytest.param(HUGE, HUGE, 600, 15, id="too-large"),
    pytest.param("99999999999999", "99999999999999", 600, 15, id="too-long"),
]
AGY_CASES = [
    pytest.param(None, None, AGY_WAKE_WINDOW_SEC, 5, id="unset"),
    pytest.param("0", "1", 0, 1, id="one-pass"),
    pytest.param("abc", "abc", AGY_WAKE_WINDOW_SEC, 5, id="words"),
    pytest.param("-1", "0", AGY_WAKE_WINDOW_SEC, 1, id="floors"),
    pytest.param(HUGE, HUGE, AGY_WAKE_WINDOW_SEC, 5, id="too-large"),
]


@pytest.fixture(autouse=True)
def _nothing_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_env_numbers, "_REPORTED", set())
    for name in (WINDOW, INTERVAL, TIMEOUT):
        monkeypatch.delenv(name, raising=False)


def _set(monkeypatch: pytest.MonkeyPatch, window: str | None, interval: str | None) -> None:
    if window is not None:
        monkeypatch.setenv(WINDOW, window)
    if interval is not None:
        monkeypatch.setenv(INTERVAL, interval)


def _recording_poll(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    polled: list[tuple[int, int]] = []

    def _poll(_agent: str, *, window: int, interval: int) -> None:
        polled.append((window, interval))

    monkeypatch.setattr(rewake, "poll_for_fresh_mail", _poll)
    return polled


@pytest.mark.parametrize(("window", "interval", "used_window", "used_interval"), MONITOR_CASES)
def test_the_claude_code_monitor_polls_with_usable_timing(
    window: str | None,
    interval: str | None,
    used_window: int,
    used_interval: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_code, "_resolve_agent", lambda: "some-agent")
    polled = _recording_poll(monkeypatch)
    _set(monkeypatch, window, interval)
    assert rewake.monitor() == 0
    assert polled == [(used_window, used_interval)]


@pytest.mark.parametrize(("window", "interval", "used_window", "used_interval"), AGY_CASES)
def test_the_antigravity_stop_hook_polls_with_usable_timing(
    window: str | None,
    interval: str | None,
    used_window: int,
    used_interval: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_antigravity, "_payload", dict)
    monkeypatch.setattr(_antigravity, "_agent_for", lambda _p: "some-agent")
    monkeypatch.setattr(_antigravity, "_with_credential", lambda _a: True)
    polled = _recording_poll(monkeypatch)
    _set(monkeypatch, window, interval)
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        assert _antigravity.agy_stop(None) == 0
    assert json.loads(buffer.getvalue()) == {"decision": "stop"}
    assert polled == [(used_window, used_interval)]


def test_a_bad_timing_value_is_named_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(claude_code, "_resolve_agent", lambda: "some-agent")
    _recording_poll(monkeypatch)
    _set(monkeypatch, "abc", "99999999999999")
    assert rewake.monitor() == 0
    assert rewake.monitor() == 0
    assert capsys.readouterr().err.splitlines() == [
        (
            f"agentbus: {WINDOW}='abc' is not a whole number from 0 to 31536000. The client is "
            "using 600. Set it to a value in that range, or unset it for the default of 600."
        ),
        (
            f"agentbus: {INTERVAL}='99999999999999' is not a whole number from 1 to 31536000. "
            "The client is using 15. Set it to a value in that range, or unset it for the "
            "default of 15."
        ),
    ]


@pytest.mark.parametrize("value", ["abc", "", "99999999999999", HUGE])
def test_the_poll_still_works_with_a_bad_interval(
    value: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(INTERVAL, value)
    monkeypatch.setattr(rewake, "_unread_text", lambda *_a, **_k: "some mail")
    assert rewake._build_resilient_poll("some-agent", wait=0)() == "some mail"
    assert "poll failed" not in capsys.readouterr().err


def _run_notify(template: str) -> None:
    _watch_handlers.notify_command(template)({"subject": "s"})


@pytest.mark.parametrize("value", ["0", "-1", "abc", "", "nan", "inf", "1e14"])
def test_a_bad_exec_timeout_still_runs_the_command(
    value: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ran = tmp_path / "ran"
    monkeypatch.setenv(TIMEOUT, value)
    _run_notify(f"touch {ran}")
    assert ran.exists()
    assert capsys.readouterr().err == (
        f"agentbus: {TIMEOUT}={value!r} is not a number of seconds above 0 and up to 31536000. "
        "The client is using 5. Set it to a value in that range, or unset it for the "
        "default of 5.\n"
    )


@pytest.mark.parametrize(
    ("value", "used"), [(None, 5.0), ("abc", 5.0), ("nan", 5.0), ("inf", 5.0), ("0.5", 0.5)]
)
def test_the_exec_timeout_reaches_the_process_call(
    value: str | None, used: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """nan and inf took away the bound on a stuck command, without any error."""
    seen: list[Any] = []

    def _run(*_a: Any, **kwargs: Any) -> None:
        seen.append(kwargs["timeout"])

    monkeypatch.setattr("agentbus_client._watch_handlers.subprocess.run", _run)
    if value is not None:
        monkeypatch.setenv(TIMEOUT, value)
    _run_notify("true")
    assert seen == [used]


def test_a_short_exec_timeout_still_stops_a_stuck_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(TIMEOUT, "0.2")
    started = time.monotonic()
    _run_notify("exec sleep 30")
    assert time.monotonic() - started < 3
    assert "--exec timed out after 0.2s" in capsys.readouterr().err
