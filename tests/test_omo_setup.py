"""#99: `agentbus setup omo`.

The extension is pinned to one commit of rodmena-limited/agentbus-omo-plugin
and each file's sha256 is checked before anything is written. The digests here
are swapped for digests of stand-in bytes, so the tests exercise the checks and
the writes without the network.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentbus_client import _omo, cli
from agentbus_client.onboarding import _doctor_omo, _omo_extension, _omo_setup

FILES = {"index.ts": b"export default function () {}\n" * 20, "package.json": b'{"name":"x"}\n'}


@pytest.fixture
def pinned(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    digests = {name: hashlib.sha256(data).hexdigest() for name, data in FILES.items()}
    monkeypatch.setattr(_omo_extension, "EXTENSION_FILES", digests)
    return {"fetched": []}


def _fetch(log: dict[str, list[str]], files: dict[str, bytes] = FILES) -> Any:
    def fetch(url: str) -> bytes:
        log["fetched"].append(url)
        return files[url.rsplit("/", 1)[1]]

    return fetch


def test_the_pin_is_a_full_commit_and_real_digests() -> None:
    assert len(_omo_extension.EXTENSION_COMMIT) == 40
    assert set(_omo_extension.EXTENSION_FILES) == {"index.ts", "package.json"}
    assert all(len(d) == 64 for d in _omo_extension.EXTENSION_FILES.values())
    assert _omo_extension.extension_url("index.ts") == (
        "https://raw.githubusercontent.com/rodmena-limited/agentbus-omo-plugin/"
        "166de8cb19e4cf414378962c7bb4e0521ada9a3f/index.ts"
    )


def test_a_fresh_install_writes_the_checked_files(pinned: dict[str, list[str]]) -> None:
    report: list[str] = []
    assert _omo_extension.install_extension(report, fetch=_fetch(pinned)) is True
    directory = _omo.omo_extension_dir()
    assert directory == Path.home() / ".omo" / "agent" / "extensions" / "agentbus"
    for name, data in FILES.items():
        assert (directory / name).read_bytes() == data
        assert (directory / name).stat().st_mode & 0o777 == 0o644
    assert len(pinned["fetched"]) == 2
    assert "installed rodmena-limited/agentbus-omo-plugin at 166de8c" in report[0]
    assert _omo_extension.extension_state() == (_omo_extension.PINNED, [])


def test_tampered_bytes_write_nothing(pinned: dict[str, list[str]]) -> None:
    bad = {**FILES, "package.json": b'{"name":"evil"}\n'}
    report: list[str] = []
    assert _omo_extension.install_extension(report, fetch=_fetch(pinned, bad)) is False
    assert not _omo.omo_extension_dir().exists()
    assert "Nothing was written" in report[0]


def test_a_failed_fetch_writes_nothing(pinned: dict[str, list[str]]) -> None:
    def fetch(url: str) -> bytes:
        msg = f"{url} answered 404"
        raise OSError(msg)

    report: list[str] = []
    assert _omo_extension.install_extension(report, fetch=fetch) is False
    assert not _omo.omo_extension_dir().exists()
    assert "answered 404" in report[0]


def test_a_current_install_is_not_fetched_again(pinned: dict[str, list[str]]) -> None:
    _omo_extension.install_extension([], fetch=_fetch(pinned))
    pinned["fetched"].clear()
    report: list[str] = []
    assert _omo_extension.install_extension(report, fetch=_fetch(pinned)) is True
    assert pinned["fetched"] == []
    assert "already at 166de8c; left as is" in report[0]


def test_a_changed_install_is_left_alone(pinned: dict[str, list[str]]) -> None:
    directory = _omo.omo_extension_dir()
    directory.mkdir(parents=True)
    (directory / "index.ts").write_text("// my local edit\n")
    report: list[str] = []
    assert _omo_extension.install_extension(report, fetch=_fetch(pinned)) is True
    assert (directory / "index.ts").read_text() == "// my local edit\n"
    assert pinned["fetched"] == []
    assert "WARNING, NOT replaced" in report[0]
    assert "index.ts" in report[0]


def _use_fetch(monkeypatch: pytest.MonkeyPatch, fetch: Any) -> None:
    real = _omo_extension.install_extension
    monkeypatch.setattr(_omo_setup, "install_extension", lambda report: real(report, fetch=fetch))


class _Resp:
    def __init__(self, status: int, payload: Any = None, content: bytes = b"") -> None:
        self.status_code = status
        self._payload = payload
        self.content = content

    def json(self) -> Any:
        return self._payload


def _wire_setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, index: dict[str, Any]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(_omo_setup, "_provision_project_agent", lambda a, r, h: "omo-0a1b2c")
    monkeypatch.setattr(_omo_setup, "_publish_sealing_key", lambda b, n, r: r.append("sealing: ok"))

    def fake_get(url: str, **_kw: Any) -> _Resp:
        if url.endswith("/skills/index.json"):
            return _Resp(200, index)
        if url.endswith("/skills/omo.md"):
            return _Resp(200, content=b"# AgentBus for omo\n" + b"x" * 600)
        return _Resp(404)

    monkeypatch.setattr(httpx, "get", fake_get)
    return repo


def test_setup_omo_wires_identity_extension_and_says_no_skill_is_served(
    pinned: dict[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _use_fetch(monkeypatch, _fetch(pinned))
    repo = _wire_setup(
        monkeypatch, tmp_path, {"harnesses": ["claude-code", "opencode"], "aliases": {}}
    )
    assert cli.main(["setup", "omo", "--role", "omo"]) == 0
    out = capsys.readouterr().out
    assert (repo / ".agentbus" / "agent").read_text().strip() == "omo-0a1b2c"
    assert (_omo.omo_extension_dir() / "index.ts").read_bytes() == FILES["index.ts"]
    assert "lists no omo flavour (it lists claude-code, opencode)" in out
    assert not _omo.omo_skill_path().exists()
    assert "refuses to start (exit 9)" in out


def test_setup_omo_installs_a_served_skill(
    pinned: dict[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _use_fetch(monkeypatch, _fetch(pinned))
    _wire_setup(monkeypatch, tmp_path, {"harnesses": ["claude-code", "omo"], "aliases": {}})
    assert cli.main(["setup", "omo", "--role", "omo"]) == 0
    assert _omo.omo_skill_path().read_bytes().startswith(b"# AgentBus for omo")
    assert "skill: installed /skills/omo.md" in capsys.readouterr().out


def test_setup_omo_exits_1_when_the_extension_is_not_installed(
    pinned: dict[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fetch(url: str) -> bytes:
        msg = "offline"
        raise OSError(msg)

    _use_fetch(monkeypatch, fetch)
    repo = _wire_setup(monkeypatch, tmp_path, {"harnesses": []})
    assert cli.main(["setup", "omo", "--role", "omo"]) == 1
    assert (repo / ".agentbus" / "agent").exists()
    assert "extension: NOT installed" in capsys.readouterr().out


def test_the_pin_is_166de8c_with_the_digests_omo_test_named() -> None:
    assert _omo_extension.EXTENSION_COMMIT == "166de8cb19e4cf414378962c7bb4e0521ada9a3f"
    assert _omo_extension.EXTENSION_FILES == {
        "index.ts": "557998d6009b5f3dfe2fee394f26fb55bb9d8a51686c756b4765c5bcec6d5790",
        "package.json": "c0b1e3c49f0f152772d0f7e0cfdca2dcb9d96c83d3e9d7c41b2c3e8756695268",
    }


def test_an_install_at_the_previous_pin_is_left_alone(pinned: dict[str, list[str]]) -> None:
    directory = _omo.omo_extension_dir()
    directory.mkdir(parents=True)
    previous = b"previous pin 181469c\n"
    (directory / "index.ts").write_bytes(previous)
    (directory / "package.json").write_bytes(FILES["package.json"])
    report: list[str] = []
    assert _omo_extension.install_extension(report, fetch=_fetch(pinned)) is True
    assert (directory / "index.ts").read_bytes() == previous
    assert pinned["fetched"] == []
    assert _omo_extension.extension_state() == (_omo_extension.DIFFERS, ["index.ts"])


def test_setup_omo_exits_0_when_a_differing_install_is_left_alone(
    pinned: dict[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    directory = _omo.omo_extension_dir()
    directory.mkdir(parents=True)
    (directory / "index.ts").write_text("// my local edit\n")
    _use_fetch(monkeypatch, _fetch(pinned))
    repo = _wire_setup(monkeypatch, tmp_path, {"harnesses": []})
    assert cli.main(["setup", "omo", "--role", "omo"]) == 0
    out = capsys.readouterr().out
    assert (repo / ".agentbus" / "agent").read_text().strip() == "omo-0a1b2c"
    assert (directory / "index.ts").read_text() == "// my local edit\n"
    assert pinned["fetched"] == []
    assert "extension: WARNING, NOT replaced" in out
    assert "agentbus setup omo` again" in out


def test_setup_omo_exits_1_when_fetched_bytes_do_not_match(
    pinned: dict[str, list[str]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bad = {**FILES, "index.ts": b"tampered\n"}
    _use_fetch(monkeypatch, _fetch(pinned, bad))
    _wire_setup(monkeypatch, tmp_path, {"harnesses": []})
    assert cli.main(["setup", "omo", "--role", "omo"]) == 1
    assert not _omo.omo_extension_dir().exists()
    assert "Nothing was written" in capsys.readouterr().out


def test_doctor_wake_reports_a_live_omo_watcher(
    pinned: dict[str, list[str]], capsys: pytest.CaptureFixture[str]
) -> None:
    _omo_extension.install_extension([], fetch=_fetch(pinned))
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        time.sleep(0.1)
        lock = _omo.omo_lock_path("omo-0a1b2c")
        assert lock is not None
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(f"{proc.pid}\n")
        assert _doctor_omo.doctor_wake_omo("omo-0a1b2c") == 0
        out = capsys.readouterr().out
        assert "harness: omo" in out
        assert f"[ok] omo watcher: pid {proc.pid} holds {lock}" in out
        assert "[ok] omo extension:" in out
        assert "did not test that stream" in out
    finally:
        proc.kill()
        proc.wait()
    assert _doctor_omo.doctor_wake_omo("omo-0a1b2c") is None
    assert "no omo session holds the stream for omo-0a1b2c (stale" in capsys.readouterr().out


def test_doctor_wake_says_nothing_about_omo_where_omo_is_absent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _doctor_omo.doctor_wake_omo("builder-ffffff") is None
    assert capsys.readouterr().out == ""
