from __future__ import annotations

import json
import secrets
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import pytest

from agentbus_client import _omo, cli
from agentbus_client.cli import _watch_omo, _watch_runtime

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

AGENT = f"builder-{secrets.token_hex(3)}"
SESSION = "61cc04b6-499d-4a88-b46e-b623fa4d2835"


def _spawn_watcher(tmp_path: Path, name: str) -> subprocess.Popen[bytes]:
    script = tmp_path / name / "agentbus"
    script.parent.mkdir(parents=True)
    script.write_text("import time\ntime.sleep(60)\n")
    return subprocess.Popen([sys.executable, str(script), "watch", "--agent", AGENT])


@pytest.fixture
def three_holders(tmp_path: Path) -> Iterator[dict[str, int]]:
    monitor = _spawn_watcher(tmp_path, "monitor")
    daemon = _spawn_watcher(tmp_path, "daemon")
    omo = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    time.sleep(0.3)
    _watch_runtime._watch_pidfile(AGENT, f"monitor-{AGENT}-{SESSION}.json").write_text(
        str(monitor.pid)
    )
    _watch_runtime._watch_pidfile(AGENT, "watch-daemon.json").write_text(str(daemon.pid))
    lock = _omo.omo_lock_path(AGENT)
    assert lock is not None
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(f"{omo.pid}\n")
    lock.chmod(0o600)
    yield {"monitor": monitor.pid, "daemon": daemon.pid, "omo": omo.pid}
    for proc in (monitor, daemon, omo):
        proc.kill()
        proc.wait()


def _run(argv: list[str], monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setenv("AGENTBUS_AGENT", AGENT)
    return cli.main(argv)


def test_json_names_each_holder_kind(
    three_holders: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run(["watch-check", "--json"], monkeypatch) == 4
    doc = json.loads(capsys.readouterr().out)
    assert doc["agent"] == AGENT
    assert doc["free"] is False
    by_kind = {h["kind"]: h for h in doc["holders"]}
    assert set(by_kind) == {"omo", "session_monitor", "watch"}
    assert by_kind["omo"]["pid"] == three_holders["omo"]
    assert by_kind["omo"]["path"] == str(_omo.omo_lock_path(AGENT))
    assert by_kind["omo"]["session"] is None
    assert by_kind["session_monitor"]["pid"] == three_holders["monitor"]
    assert by_kind["session_monitor"]["session"] == SESSION
    assert by_kind["session_monitor"]["state_key"].endswith(f"-{SESSION}.json.pid")
    assert by_kind["watch"]["pid"] == three_holders["daemon"]
    assert by_kind["watch"]["session"] is None
    assert by_kind["watch"]["state_key"] == f"{AGENT}-watch-daemon.json.pid"


def test_global_json_flag_gives_the_same_document(
    three_holders: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run(["--json", "watch-check"], monkeypatch) == 4
    doc = json.loads(capsys.readouterr().out)
    assert {h["pid"] for h in doc["holders"]} == set(three_holders.values())


def test_session_and_harness_filters_apply_to_json(
    three_holders: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv = ["watch-check", "--json", "--harness", "omo", "--session", SESSION]
    assert _run(argv, monkeypatch) == 4
    doc = json.loads(capsys.readouterr().out)
    assert [h["kind"] for h in doc["holders"]] == ["watch"]
    assert doc["holders"][0]["pid"] == three_holders["daemon"]


def test_text_output_is_unchanged(
    three_holders: dict[str, int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _run(["watch-check"], monkeypatch) == 4
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"held: {AGENT} already has a watcher:"
    assert lines[1] == f"  omo session, pid {three_holders['omo']} [{_omo.omo_lock_path(AGENT)}]"
    assert sorted(lines[2:]) == sorted(
        [
            (
                f"  agentbus watch, pid {three_holders['monitor']} "
                f"[{AGENT}-monitor-{AGENT}-{SESSION}.json.pid]"
            ),
            f"  agentbus watch, pid {three_holders['daemon']} [{AGENT}-watch-daemon.json.pid]",
        ]
    )


def test_free_agent_json_and_exit_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _run(["watch-check", "--json"], monkeypatch) == 0
    assert json.loads(capsys.readouterr().out) == {"agent": AGENT, "free": True, "holders": []}


@pytest.mark.parametrize(
    ("state_key", "kind", "session"),
    [
        (f"a-monitor-a-{SESSION}.json.pid", "session_monitor", SESSION),
        ("a-watch-daemon.json.pid", "watch", None),
        ("(legacy)", "watch", None),
        (f"a-{SESSION}-extra.json.pid", "watch", None),
    ],
)
def test_registration_classification(state_key: str, kind: str, session: str | None) -> None:
    holder = _watch_omo._registration_holder(state_key, 7)
    assert holder == {"kind": kind, "pid": 7, "state_key": state_key, "session": session}
