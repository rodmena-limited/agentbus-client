from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentbus_client import cli
from agentbus_client.cli import _watch_argv

if TYPE_CHECKING:
    from collections.abc import Iterator

AGENT = "zz-argv-probe-4f1c"
SHELL_PS = (
    f"  501 bash -c agentbus watch-status --agent {AGENT}; echo agentbus watch --agent {AGENT}\n"
)
UV_PY = "/home/u/.local/share/uv/tools/rodmena-agentbus/bin/python"
UV_BIN = "/home/u/.local/bin/agentbus"


@pytest.mark.parametrize(
    "argv",
    [
        [UV_PY, UV_BIN, "watch", "--agent", AGENT, "--state", "/s.json", "--exec", "hook x"],
        ["/usr/bin/python3", "-m", "agentbus_client.cli", "watch", "--agent", AGENT],
        [
            "/usr/bin/python3.12",
            "-I",
            "-X",
            "utf8",
            "-m",
            "agentbus_client.cli",
            "watch",
            f"--agent={AGENT}",
        ],
        ["/usr/local/bin/agentbus", "watch", "--agent", AGENT],
        ["python3", "agentbus", "watch", "--agent", f"'{AGENT}'"],
        [UV_PY, UV_BIN, "watch", "--agent", "other", "--agent", AGENT],
    ],
)
def test_watcher_argv_shapes_are_recognised(argv: list[str]) -> None:
    assert _watch_argv.watch_argv_agent(argv) == AGENT
    assert _watch_argv.argv_is_watcher(argv, AGENT) is True


@pytest.mark.parametrize(
    "argv",
    [
        [
            "bash",
            "-c",
            f"agentbus watch-status --agent {AGENT}; echo agentbus watch --agent {AGENT}",
        ],
        "bash -c agentbus watch-status --agent X; echo agentbus watch --agent X".replace(
            "X", AGENT
        ).split(),
        ["/bin/zsh", "-c", f"agentbus watch --agent {AGENT}"],
        [UV_PY, UV_BIN, "watch-status", "--agent", AGENT],
        [UV_PY, UV_BIN, "watch-stop", "--agent", AGENT],
        [UV_PY, UV_BIN, "watch", "--agent", f"{AGENT}-x"],
        [UV_PY, UV_BIN, "watch", "--agent", AGENT, "--agent", "other"],
        [UV_PY, UV_BIN, "watch"],
        [UV_PY, UV_BIN, "watch", "--agent"],
        ["python3", "-c", f"agentbus watch --agent {AGENT}"],
        ["python3", "-m", "other_pkg", "watch", "--agent", AGENT],
        ["python3", "/opt/notagentbus", "watch", "--agent", AGENT],
        ["vim", "agentbus", "watch", "--agent", AGENT],
        ["grep", "agentbus watch --agent", AGENT],
        [],
    ],
)
def test_non_watcher_argv_is_rejected(argv: list[str]) -> None:
    assert _watch_argv.argv_is_watcher(argv, AGENT) is False


def _ps_returning(output: str) -> Any:
    def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(stdout=output, returncode=0)

    return fake_run


def test_scan_skips_a_shell_line_and_returns_the_real_watcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ps = SHELL_PS + f"  777 {UV_PY} {UV_BIN} watch --agent {AGENT} --exec agentbus-hook inject\n"
    monkeypatch.setattr(_watch_argv, "proc_argv", lambda _pid: None)
    monkeypatch.setattr(subprocess, "run", _ps_returning(ps))
    assert cli._scan_watch_process(AGENT) == 777


def test_scan_with_only_a_shell_line_finds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    ps = SHELL_PS
    monkeypatch.setattr(_watch_argv, "proc_argv", lambda _pid: None)
    monkeypatch.setattr(subprocess, "run", _ps_returning(ps))
    assert cli._scan_watch_process(AGENT) is None


def test_scan_prefers_exact_proc_argv_when_it_matches_ps(monkeypatch: pytest.MonkeyPatch) -> None:
    exact = [UV_PY, UV_BIN, "watch", "--agent", AGENT, "--exec", "hook --agent other"]
    ps = f"  777 {' '.join(exact)}\n"
    monkeypatch.setattr(_watch_argv, "proc_argv", lambda pid: exact if pid == 777 else None)
    monkeypatch.setattr(subprocess, "run", _ps_returning(ps))
    assert cli._scan_watch_process(AGENT) == 777
    assert cli._scan_watch_process("other") is None


def test_scan_ignores_proc_argv_of_a_different_process(monkeypatch: pytest.MonkeyPatch) -> None:
    ps = f"  777 {UV_PY} {UV_BIN} watch --agent {AGENT}\n"
    monkeypatch.setattr(_watch_argv, "proc_argv", lambda _pid: ["/sbin/init"])
    monkeypatch.setattr(subprocess, "run", _ps_returning(ps))
    assert cli._scan_watch_process(AGENT) == 777


needs_ps = pytest.mark.skipif(shutil.which("ps") is None, reason="no ps on this host")


@pytest.fixture
def fake_watcher(tmp_path: Path) -> Iterator[subprocess.Popen[bytes]]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "agentbus"
    script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    script.chmod(0o755)
    proc = subprocess.Popen([sys.executable, str(script), "watch", "--agent", AGENT])
    time.sleep(0.3)
    yield proc
    proc.kill()
    proc.wait()


@pytest.fixture
def naming_shell() -> Iterator[subprocess.Popen[bytes]]:
    proc = subprocess.Popen(
        ["bash", "-c", f"sleep 60; echo agentbus watch --agent {AGENT}"],
    )
    time.sleep(0.3)
    yield proc
    proc.kill()
    proc.wait()


@needs_ps
def test_live_shell_naming_the_agent_is_not_a_watcher(
    naming_shell: subprocess.Popen[bytes],
) -> None:
    assert cli._pid_is_watcher(naming_shell.pid, AGENT) is False
    assert cli._scan_watch_process(AGENT) is None


@needs_ps
def test_live_watcher_shaped_process_is_found(
    fake_watcher: subprocess.Popen[bytes], naming_shell: subprocess.Popen[bytes]
) -> None:
    assert cli._pid_is_watcher(fake_watcher.pid, AGENT) is True
    assert cli._scan_watch_process(AGENT) == fake_watcher.pid


def _run_watch_status_from_naming_shell(cfg: Path) -> subprocess.CompletedProcess[str]:
    cfg.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "AGENTBUS_CONFIG_DIR": str(cfg), "PYTHONPATH": str(REPO / "src")}
    for name in ("AGENTBUS_AGENT", "AGENTBUS_WAKE_DIR"):
        env.pop(name, None)
    inner = (
        f"{sys.executable} -m agentbus_client.cli watch-status --agent {AGENT}; rc=$?; "
        f"echo agentbus watch --agent {AGENT} >/dev/null; exit $rc"
    )
    return subprocess.run(
        ["bash", "-c", inner], capture_output=True, text=True, env=env, timeout=60, check=False
    )


@needs_ps
def test_cli_watch_status_from_a_naming_shell_reports_not_running(tmp_path: Path) -> None:
    result = _run_watch_status_from_naming_shell(tmp_path / "cfg")
    assert f"watcher NOT running for {AGENT}" in result.stdout, result.stdout + result.stderr
    assert "RUNNING for" not in result.stdout.replace("NOT running", "")
    assert result.returncode == 1


@needs_ps
def test_cli_watch_status_from_a_naming_shell_still_finds_a_real_watcher(
    tmp_path: Path, fake_watcher: subprocess.Popen[bytes]
) -> None:
    result = _run_watch_status_from_naming_shell(tmp_path / "cfg")
    assert f"watcher RUNNING for {AGENT} (pid {fake_watcher.pid})" in result.stdout, (
        result.stdout + result.stderr
    )
    assert result.returncode == 10


@pytest.mark.parametrize(
    ("wake", "orphan", "expected"),
    [("ok", 4242, 10), ("broken", 4242, 3), ("ok", None, 1), ("broken", None, 3)],
)
def test_watch_status_exit_code_for_a_scan_found_watcher(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    wake: str,
    orphan: int | None,
    expected: int,
) -> None:
    from agentbus_client.cli import _common, _watch_omo, _watch_status

    monkeypatch.setattr(_watch_status, "_report_wake_path", lambda _agent: wake == "broken")
    monkeypatch.setattr(_watch_status, "_watch_pids", lambda _agent: {})
    monkeypatch.setattr(_watch_omo, "print_omo_status", lambda _agent: False)
    monkeypatch.setattr(_watch_status, "_scan_watch_process", lambda _agent: orphan)
    monkeypatch.setattr(_watch_status, "_existing_logfile", lambda *_a: None)
    monkeypatch.setattr(_common, "require_acting_agent", lambda _args: AGENT)
    args = SimpleNamespace(agent=AGENT, state=None)
    assert _watch_status.cmd_watch_status(args) == expected  # type: ignore[arg-type]
    out = capsys.readouterr().out
    if orphan:
        assert f"watcher RUNNING for {AGENT} (pid {orphan})" in out
    else:
        assert f"watcher NOT running for {AGENT}" in out
