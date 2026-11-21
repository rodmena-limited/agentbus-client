"""#100: the omo watcher is visible, and only one watcher holds an identity.

The lock format is the one omo's extension writes at agentbus-omo-plugin
181469c, index.ts acquireLock(): `${process.pid}\n`, mode 0600, at
`$AGENTBUS_CONFIG_DIR/omo-watch-<agent>.lock`. A real child process stands in
for the omo session, so liveness is checked against the operating system.
"""

from __future__ import annotations

import argparse
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from agentbus_client import _omo, cli
from agentbus_client.cli import _watch_omo, _watch_runtime

AGENT = f"builder-{secrets.token_hex(3)}"


def _lock() -> Path:
    path = _omo.omo_lock_path(AGENT)
    assert path is not None
    return path


def _write_lock(pid: int) -> Path:
    path = _lock()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n")
    path.chmod(0o600)
    return path


@pytest.fixture
def omo_session() -> Any:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    time.sleep(0.1)
    _write_lock(proc.pid)
    yield proc
    proc.kill()
    proc.wait()


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


def test_the_lock_path_is_the_one_omo_writes(tmp_path: Path) -> None:
    assert _lock() == Path(os.environ["AGENTBUS_CONFIG_DIR"]) / f"omo-watch-{AGENT}.lock"


@pytest.mark.parametrize("name", ["../operator", "a/b", ""])
def test_a_name_that_is_not_a_filename_never_reads_a_lock(name: str) -> None:
    assert _omo.omo_lock_path(name) is None
    assert _omo.omo_holder(name).state == _omo.ABSENT


def test_no_lock_is_absent() -> None:
    holder = _omo.omo_holder(AGENT)
    assert holder.state == _omo.ABSENT
    assert holder.live is False


def test_a_running_holder_is_live_and_its_death_frees_the_lock(omo_session: Any) -> None:
    holder = _omo.omo_holder(AGENT)
    assert holder.state == _omo.LIVE
    assert holder.pid == omo_session.pid
    omo_session.kill()
    omo_session.wait()
    after = _omo.omo_holder(AGENT)
    assert after.state == _omo.STALE
    assert after.reason == f"pid {omo_session.pid} is not running"


def test_a_dead_pid_is_stale() -> None:
    pid = _dead_pid()
    _write_lock(pid)
    assert _omo.omo_holder(AGENT).state == _omo.STALE


def test_a_reused_pid_is_stale(omo_session: Any) -> None:
    started = _omo.process_start_epoch(omo_session.pid)
    assert started is not None
    os.utime(_lock(), (started - 3600, started - 3600))
    holder = _omo.omo_holder(AGENT)
    assert holder.state == _omo.STALE
    assert "started after the lock was written" in holder.reason


def test_process_start_time_is_measured_not_guessed() -> None:
    before = time.time()
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        started = _omo.process_start_epoch(proc.pid)
        assert started is not None
        assert before - 2 <= started <= time.time() + 2
        own = _omo.process_start_epoch(os.getpid())
        assert own is not None
        assert own < before
    finally:
        proc.kill()
        proc.wait()


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("05:06", 306), ("01:02:03", 3723), ("1-02:03:04", 93784), ("   42:00\n", 2520), ("", None)],
)
def test_ps_elapsed_time_parses(text: str, seconds: int | None) -> None:
    assert _omo._parse_etime(text) == seconds


@pytest.mark.parametrize(("content", "fragment"), [("hello\n", "not a pid"), ("0\n", "pid 0")])
def test_an_unreadable_lock_says_why(content: str, fragment: str) -> None:
    _lock().parent.mkdir(parents=True, exist_ok=True)
    _lock().write_text(content)
    holder = _omo.omo_holder(AGENT)
    assert holder.state == _omo.UNREADABLE
    assert fragment in holder.reason


def _main(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setenv("AGENTBUS_API_KEY", "ab_sk_x_y")
    monkeypatch.setenv("AGENTBUS_BASE_URL", "https://bus.invalid")
    return cli.main(argv)


def test_watch_refuses_while_omo_holds_the_stream_and_starts_after(
    omo_session: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started: list[Any] = []
    import agentbus_client.watch as watch_mod

    def fake_run(self: Any, *, once: bool = False) -> int:
        started.append(once)
        return 0

    monkeypatch.setattr(watch_mod.Watcher, "run", fake_run)
    monkeypatch.setattr(cli._common, "_bus", lambda _a: _NoNetworkBus())
    code = _main(["watch", "--agent", AGENT, "--state", "s.json"], monkeypatch)
    out = capsys.readouterr().out
    assert code == _watch_omo.EXIT_HELD_BY_OTHER_HARNESS == 9
    assert f"an omo session (pid {omo_session.pid}) already holds the stream" in out
    assert started == []

    omo_session.kill()
    omo_session.wait()
    code = _main(["watch", "--agent", AGENT, "--state", "s.json"], monkeypatch)
    assert code == 0
    assert started == [False]


def test_watch_once_is_not_refused(omo_session: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    assert _watch_omo.watch_refusal(argparse.Namespace(once=True), AGENT) is None
    assert _watch_omo.watch_refusal(argparse.Namespace(once=False), AGENT) == 9


class _NoNetworkBus:
    agent = AGENT
    base_url = "https://bus.invalid"

    def whoami(self, *a: Any, **k: Any) -> dict[str, Any]:
        return {"agent": {"name": AGENT}, "workspace": {"slug": "ws"}}

    def __getattr__(self, name: str) -> Any:
        msg = f"unexpected network call: {name}"
        raise AssertionError(msg)


def _check(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setenv("AGENTBUS_AGENT", AGENT)
    return cli.main(["watch-check", *argv])


def test_watch_check_for_claude_sees_the_omo_lock_both_ways(
    omo_session: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _check([], monkeypatch) == 4
    out = capsys.readouterr().out
    assert f"held: {AGENT} already has a watcher:" in out
    assert f"omo session, pid {omo_session.pid}" in out
    omo_session.kill()
    omo_session.wait()
    assert _check([], monkeypatch) == 0
    assert f"free: no other watcher holds {AGENT}" in capsys.readouterr().out


def test_watch_check_for_omo_sees_a_client_watcher(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pids = {f"monitor-{AGENT}-aaaa.json": 4242}
    monkeypatch.setattr(_watch_omo, "_watch_pids", lambda agent: dict(pids))
    assert _check(["--harness", "omo"], monkeypatch) == 4
    assert f"agentbus watch, pid 4242 [monitor-{AGENT}-aaaa.json]" in capsys.readouterr().out
    assert _check(["--harness", "claude", "--session", "aaaa"], monkeypatch) == 0
    pids.clear()
    assert _check(["--harness", "omo"], monkeypatch) == 0


def test_watch_check_for_omo_ignores_its_own_lock(
    omo_session: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_watch_omo, "_watch_pids", lambda agent: {})
    assert _check(["--harness", "omo"], monkeypatch) == 0


def test_watch_check_reads_real_pidfiles(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    script = tmp_path / "agentbus"
    script.write_text("import time\ntime.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, str(script), "watch", "--agent", AGENT])
    try:
        time.sleep(0.3)
        pidfile = _watch_runtime._watch_pidfile(AGENT, "monitor-x-1111.json")
        pidfile.write_text(str(proc.pid))
        assert _check([], monkeypatch) == 4
        assert f"agentbus watch, pid {proc.pid}" in capsys.readouterr().out
    finally:
        proc.kill()
        proc.wait()
    assert _check([], monkeypatch) == 0


def test_watch_status_reports_the_omo_watcher(
    omo_session: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENTBUS_AGENT", AGENT)
    code = cli.main(["watch-status"])
    out = capsys.readouterr().out
    assert f"omo watcher RUNNING for {AGENT} (pid {omo_session.pid})" in out
    assert "watcher NOT running" not in out
    assert code in (0, 3)
    omo_session.kill()
    omo_session.wait()
    code = cli.main(["watch-status"])
    out = capsys.readouterr().out
    assert f"omo lock STALE for {AGENT}: pid {omo_session.pid} is not running" in out
    assert f"watcher NOT running for {AGENT}" in out
    assert code in (1, 3)


def test_watch_stop_leaves_the_omo_session_running(
    omo_session: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENTBUS_AGENT", AGENT)
    assert cli.main(["watch-stop"]) == 1
    out = capsys.readouterr().out
    assert f"not stopped: the omo watcher for {AGENT} (pid {omo_session.pid})" in out
    time.sleep(0.1)
    assert omo_session.poll() is None
