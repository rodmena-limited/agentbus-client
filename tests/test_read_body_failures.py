"""#93 item 23: a body that cannot be read is one plain line, not a traceback.

`_read_body` backs every verb that takes `@file` or `@-`. Each failure below used
to escape main() as a traceback. The in-process tests walk every verb with a
stub bus; the subprocess tests run the real entry point, because Python prints a
SystemExit message at the process boundary and nothing else proves the output.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from agentbus_client import cli
from agentbus_client.cli import _common, _memory

SRC = Path(cli.__file__).resolve().parents[2]
ADVICE = "Check the path, or pipe text that starts with @ through @-."
STDIN_ADVICE = "Pipe UTF-8 text in, or pass the text on the command line."


class _StubBus:
    agent = "t"
    base_url = "https://stub"

    def read(self, *_a: Any, **_k: Any) -> dict[str, Any]:
        return {"message_id": "01M", "subject": "s", "text_body": "original body"}

    def __getattr__(self, name: str) -> Any:
        def _called(*_a: Any, **_k: Any) -> None:
            message = f"the body failure must stop the verb before bus.{name} is called"
            raise AssertionError(message)

        return _called


def _verbs(spec: str) -> dict[str, list[str]]:
    return {
        "send": ["send", "bob", "-s", "subject", "-b", spec],
        "send_payload": ["send", "bob", "-b", "text", "--payload", spec],
        "reply": ["reply", "01MESSAGE", "-b", spec],
        "forward": ["forward", "01DELIVERY", "bob", "-b", spec],
        "draft": ["draft", "bob", "-b", spec],
        "schema": ["schema", "ops", "--set", spec],
        "memory": ["memory", spec],
        "memory_add": ["memory", "add", spec],
        "remind": ["remind", "-m", spec, "--delay", "1h"],
    }


VERBS = sorted(_verbs("x"))


@pytest.fixture
def stub_bus(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli._common, "_bus", lambda _args: _StubBus())
    monkeypatch.setattr(_memory, "_bus", lambda _args: _StubBus())


@pytest.fixture(params=VERBS)
def verb(request: pytest.FixtureRequest) -> str:
    return str(request.param)


def _refused(verb: str, spec: str) -> str:
    with pytest.raises(SystemExit) as caught:
        cli.main(_verbs(spec)[verb])
    code = caught.value.code
    assert isinstance(code, str), f"exit code {code!r} prints no message"
    assert "\n" not in code
    return code


@pytest.mark.usefixtures("stub_bus")
def test_a_missing_file_is_one_plain_line(verb: str, tmp_path: Path) -> None:
    spec = f"@{tmp_path}/missing.txt"
    assert _refused(verb, spec) == (f"cannot read {spec!r}: No such file or directory. {ADVICE}")


@pytest.mark.usefixtures("stub_bus")
def test_a_bare_at_sign_is_one_plain_line(verb: str) -> None:
    assert _refused(verb, "@") == f"cannot read '@': No such file or directory. {ADVICE}"


@pytest.mark.usefixtures("stub_bus")
def test_a_directory_is_one_plain_line(verb: str, tmp_path: Path) -> None:
    spec = f"@{tmp_path}"
    assert _refused(verb, spec) == f"cannot read {spec!r}: Is a directory. {ADVICE}"


@pytest.mark.usefixtures("stub_bus")
def test_a_nul_byte_in_the_path_is_one_plain_line(verb: str) -> None:
    assert _refused(verb, "@a\0b") == (f"cannot read '@a\\x00b': embedded null byte. {ADVICE}")


@pytest.mark.usefixtures("stub_bus")
def test_a_binary_file_is_one_plain_line(verb: str, tmp_path: Path) -> None:
    binary = tmp_path / "blob.bin"
    binary.write_bytes(b"\xff\xfe\x00\x80")
    spec = f"@{binary}"
    assert _refused(verb, spec) == f"cannot read {spec!r}: it is not UTF-8 text. {ADVICE}"


@pytest.mark.usefixtures("stub_bus")
def test_a_name_too_long_for_the_filesystem_is_one_plain_line(verb: str) -> None:
    spec = "@" + "n" * 5000
    assert _refused(verb, spec) == f"cannot read {spec!r}: File name too long. {ADVICE}"


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads every file")
@pytest.mark.usefixtures("stub_bus")
def test_a_file_without_read_permission_is_one_plain_line(verb: str, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("x")
    secret.chmod(0)
    spec = f"@{secret}"
    assert _refused(verb, spec) == f"cannot read {spec!r}: Permission denied. {ADVICE}"


def test_a_good_file_still_reads(tmp_path: Path) -> None:
    good = tmp_path / "note.txt"
    good.write_text("the note\n", encoding="utf-8")
    assert _common._read_body(f"@{good}") == "the note\n"


def test_stdin_still_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("from a pipe"))
    assert _common._read_body("@-") == "from a pipe"


def test_text_without_an_at_sign_passes_through() -> None:
    assert _common._read_body("plain text") == "plain text"
    assert _common._read_body(None) is None


def test_the_literal_path_refusal_is_unchanged(tmp_path: Path) -> None:
    existing = tmp_path / "note.txt"
    existing.write_text("x")
    with pytest.raises(SystemExit) as caught:
        _common._read_body(str(existing))
    assert caught.value.code == (
        f"refusing to send the literal path '{existing}'. Use '@{existing}' to send the "
        "file's contents."
    )


def test_undecodable_stdin_is_one_plain_line(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.TextIOWrapper(io.BytesIO(b"\xff\xfe"), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", stream)
    with pytest.raises(SystemExit) as caught:
        _common._read_body("@-")
    assert caught.value.code == f"cannot read stdin for '@-': it is not UTF-8 text. {STDIN_ADVICE}"


def test_closed_stdin_is_one_plain_line(monkeypatch: pytest.MonkeyPatch) -> None:
    stream = io.StringIO("gone")
    stream.close()
    monkeypatch.setattr(sys, "stdin", stream)
    with pytest.raises(SystemExit) as caught:
        _common._read_body("@-")
    assert caught.value.code == f"cannot read stdin for '@-': stdin is closed. {STDIN_ADVICE}"


def _real_cli(args: list[str], tmp_path: Path, shell_prefix: str = "") -> Any:
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC),
        "HOME": str(tmp_path),
        "AGENTBUS_API_KEY": "ab_sk_stub",
        "AGENTBUS_BASE_URL": "http://127.0.0.1:1",
        "AGENTBUS_AGENT": "t",
    }
    argv = [sys.executable, "-m", "agentbus_client.cli", *args]
    if shell_prefix:
        script = f'{shell_prefix} exec "$@"'
        argv = ["/bin/sh", "-c", script, "sh", *argv]
    return subprocess.run(
        argv,
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


@pytest.mark.parametrize(
    "args",
    [
        ["memory", "@missing.txt"],
        ["remind", "-m", "@nofile", "--delay", "1h"],
        ["send", "bob", "-b", "@"],
        ["send", "bob", "-b", "@."],
        ["draft", "bob", "-b", "@missing.txt"],
    ],
    ids=["memory", "remind", "send_bare_at", "send_directory", "draft"],
)
def test_the_real_process_prints_one_line_and_exits_one(args: list[str], tmp_path: Path) -> None:
    done = _real_cli(args, tmp_path)
    assert done.returncode == 1
    assert done.stdout == ""
    assert done.stderr.count("\n") == 1
    assert done.stderr.startswith("cannot read '@")
    assert "Traceback" not in done.stderr


def test_the_real_process_handles_a_closed_stdin(tmp_path: Path) -> None:
    done = _real_cli(["memory", "@-"], tmp_path, shell_prefix="exec <&-;")
    assert done.returncode == 1
    assert done.stderr == f"cannot read stdin for '@-': stdin is closed. {STDIN_ADVICE}\n"
