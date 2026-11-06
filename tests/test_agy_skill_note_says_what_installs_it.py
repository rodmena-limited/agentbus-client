"""#94 item 11: the agy skill note names a command that does what the note says.

The note used to say `agentbus refresh-skill` installs the flavour at agy's path, and that
command writes only the Claude or opencode flavour. These cases serve the skill from a real
local HTTP server and run the command the note prints.
"""

from __future__ import annotations

import http.server
import json
import os
import re
import shlex
import shutil
import subprocess
import threading
from typing import TYPE_CHECKING, Any

import pytest

from agentbus_client.onboarding import _agy_setup, _skill

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

SERVED_BYTES = ("antigravity flavour " * 60).encode()
CLAUDE_BYTES = ("claude flavour " * 60).encode()
COMMAND = re.compile(r"`(curl [^`]+)`")


def _server_for(pages: dict[str, bytes]) -> Iterator[str]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = pages.get(self.path)
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def serve() -> Iterator[Callable[[dict[str, bytes]], str]]:
    started: list[Iterator[str]] = []

    def start(pages: dict[str, bytes]) -> str:
        running = _server_for(pages)
        started.append(running)
        return next(running)

    yield start
    for running in started:
        next(running, None)


def _index(harnesses: list[str], aliases: dict[str, str] | None = None) -> bytes:
    return json.dumps({"harnesses": harnesses, "aliases": aliases or {}}).encode()


def _target(home: Path) -> Path:
    return home / ".gemini" / "config" / "skills" / "agentbus" / "SKILL.md"


def _run_printed_command(note: str, home: Path) -> None:
    found = COMMAND.search(note)
    assert found is not None, note
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    subprocess.run(shlex.split(found.group(1)), env=env, check=True, timeout=30)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl")
def test_the_printed_command_installs_the_flavour_where_agy_reads_it(
    home: Path, serve: Callable[[dict[str, bytes]], str]
) -> None:
    base = serve(
        {
            "/skills/index.json": _index(["claude-code", "antigravity"]),
            "/skills/antigravity.md": SERVED_BYTES,
        }
    )
    note = _agy_setup._skill_note(base)
    assert note.startswith("skill: SERVED")
    _run_printed_command(note, home)
    assert _target(home).read_bytes() == SERVED_BYTES


@pytest.mark.skipif(shutil.which("curl") is None, reason="needs curl")
def test_the_printed_command_follows_the_name_the_index_publishes(
    home: Path, serve: Callable[[dict[str, bytes]], str]
) -> None:
    base = serve(
        {
            "/skills/index.json": _index(["agy"], {"antigravity": "agy"}),
            "/skills/agy.md": SERVED_BYTES,
        }
    )
    note = _agy_setup._skill_note(base)
    assert f"{base}/skills/agy.md" in note
    _run_printed_command(note, home)
    assert _target(home).read_bytes() == SERVED_BYTES


def test_a_home_with_a_space_is_quoted_in_the_printed_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, serve: Callable[[dict[str, bytes]], str]
) -> None:
    spaced = tmp_path / "a home"
    spaced.mkdir()
    monkeypatch.setenv("HOME", str(spaced))
    base = serve({"/skills/index.json": _index(["antigravity"])})
    found = COMMAND.search(_agy_setup._skill_note(base))
    assert found is not None
    assert str(_target(spaced)) in shlex.split(found.group(1))


def test_the_note_keeps_the_warning_about_a_copy_already_there(
    home: Path, serve: Callable[[dict[str, bytes]], str]
) -> None:
    base = serve({"/skills/index.json": _index(["antigravity"])})
    note = _agy_setup._skill_note(base)
    assert f"Back up any copy already at {_target(home)} first" in note


def test_refresh_skill_really_leaves_the_agy_path_alone_as_the_note_says(
    home: Path, monkeypatch: pytest.MonkeyPatch, serve: Callable[[dict[str, bytes]], str]
) -> None:
    base = serve(
        {
            "/skills/index.json": _index(["claude-code", "antigravity"]),
            "/skills/claude-code.md": CLAUDE_BYTES,
            "/skills/antigravity.md": SERVED_BYTES,
        }
    )
    monkeypatch.chdir(home)
    note = _agy_setup._skill_note(base)
    assert "`agentbus refresh-skill` does not write that path" in note
    state, _detail = _skill.refresh_skill(base_url=base)
    assert state == "installed"
    assert (home / ".claude" / "skills" / "agentbus" / "SKILL.md").read_bytes() == CLAUDE_BYTES
    assert not _target(home).exists()


def test_an_index_without_the_flavour_prints_no_command(
    home: Path, serve: Callable[[dict[str, bytes]], str]
) -> None:
    base = serve({"/skills/index.json": _index(["claude-code", "opencode"])})
    note = _agy_setup._skill_note(base)
    assert "SERVED" not in note
    assert COMMAND.search(note) is None
