from __future__ import annotations

import errno
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING, Any

import pytest

from agentbus_client.onboarding import _dump_json

if TYPE_CHECKING:
    from pathlib import Path

not_root = pytest.mark.skipif(os.geteuid() == 0, reason="root writes every file")


def _entries(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def test_an_interrupted_write_leaves_the_previous_file_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "settings.json"
    original = json.dumps({"keep": list(range(500))}, indent=2) + "\n"
    target.write_text(original)

    def die(_fd: int) -> None:
        raise OSError(errno.EIO, "Input/output error")

    monkeypatch.setattr(os, "fsync", die)
    with pytest.raises(OSError, match="Input/output error") as caught:
        _dump_json(target, {"new": "x" * 5000})
    assert caught.value.filename == str(target)
    assert target.read_text() == original
    assert _entries(tmp_path) == ["settings.json"]


CRASH = """
import os, signal, sys
from pathlib import Path
from agentbus_client.onboarding import _dump_json
os.replace = lambda *_a: os.kill(os.getpid(), signal.SIGKILL)
_dump_json(Path(sys.argv[1]), {"new": "x" * 100000})
"""


def test_a_process_killed_mid_write_leaves_the_previous_file_whole(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    original = '{"keep": true}\n'
    target.write_text(original)
    proc = subprocess.run(
        [sys.executable, "-c", CRASH, str(target)], capture_output=True, timeout=60, check=False
    )
    assert proc.returncode == -signal.SIGKILL
    assert target.read_text() == original
    leftovers = [n for n in _entries(tmp_path) if n != "settings.json"]
    assert all(n.startswith(".settings.json.") for n in leftovers)


def test_a_completed_write_replaces_the_content(tmp_path: Path) -> None:
    target = tmp_path / "sub" / "settings.json"
    _dump_json(target, {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1}
    _dump_json(target, {"b": 2})
    assert json.loads(target.read_text()) == {"b": 2}
    assert _entries(target.parent) == ["settings.json"]


@not_root
def test_a_read_only_file_is_not_replaced(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text('{"mine": true}\n')
    target.chmod(0o400)
    with pytest.raises(PermissionError) as caught:
        _dump_json(target, {"theirs": True})
    assert caught.value.filename == str(target)
    assert caught.value.strerror == "Permission denied"
    assert target.read_text() == '{"mine": true}\n'
    assert stat.S_IMODE(target.stat().st_mode) == 0o400
    assert _entries(tmp_path) == ["settings.json"]


def test_an_existing_mode_is_kept(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text("{}\n")
    target.chmod(0o640)
    _dump_json(target, {"a": 1})
    assert stat.S_IMODE(target.stat().st_mode) == 0o640


def test_a_new_file_follows_the_umask(tmp_path: Path) -> None:
    old = os.umask(0o027)
    try:
        _dump_json(tmp_path / "settings.json", {"a": 1})
    finally:
        os.umask(old)
    assert stat.S_IMODE((tmp_path / "settings.json").stat().st_mode) == 0o640


def test_a_private_file_is_0600_from_birth_and_after(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "opencode.json"
    target.write_text("{}\n")
    target.chmod(0o644)
    seen: list[int] = []
    real_mkstemp = tempfile.mkstemp

    def spy(*args: Any, **kwargs: Any) -> tuple[int, str]:
        fd, name = real_mkstemp(*args, **kwargs)
        seen.append(stat.S_IMODE(os.fstat(fd).st_mode))
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", spy)
    _dump_json(target, {"key": "ab_sk_x"}, private=True)
    assert seen == [0o600]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_a_symlinked_settings_file_keeps_its_link(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text("{}\n")
    link = tmp_path / "project" / "settings.json"
    link.parent.mkdir()
    link.symlink_to(real)
    _dump_json(link, {"a": 1})
    assert link.is_symlink()
    assert json.loads(real.read_text()) == {"a": 1}
    assert _entries(link.parent) == ["settings.json"]
    assert _entries(real.parent) == ["settings.json"]
