"""#94 item 8: one bad line in the wake file hid the rest of it.

`_print_stale_wakes` wrapped the whole loop in one `suppress`, so the first line it
could not read ended the loop and every later arrival went unprinted. A byte that
is not UTF-8 lost the whole file. The good lines here come from the real writer,
`agentbus-hook notify`.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest

import agentbus_client
from agentbus_client.client.errors import TransportError
from agentbus_client.hooks import _state, _turn

SRC = Path(agentbus_client.__file__).resolve().parents[1]
AGENT = "wake-agent"
SEPARATOR = chr(0x2028)
HEADER = (
    "AgentBus: server unreachable; {count} captured arrival(s) "
    "in the local wake file (may already be read):"
)


@pytest.fixture(autouse=True)
def _agent_in_a_plain_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENTBUS_AGENT", AGENT)


def _wake() -> Path:
    return _state._wake_file(AGENT)


def _real_line(sender: str, subject: str, delivery: str) -> bytes:
    """What `agentbus-hook notify` really appends, read back off the disk."""
    path = _wake()
    before = path.read_bytes() if path.exists() else b""
    path.unlink(missing_ok=True)
    args = argparse.Namespace(subject=subject, sender=sender, delivery=delivery)
    assert _turn.notify(args) == 0
    line = path.read_bytes()
    path.write_bytes(before)
    return line


def _printed(capsys: pytest.CaptureFixture[str], wake: Path) -> tuple[bool, list[str]]:
    answer = _turn._print_stale_wakes(wake)
    return answer, capsys.readouterr().out.split("\n")[:-1]


def _file_of(*lines: bytes) -> Path:
    wake = _wake()
    wake.parent.mkdir(parents=True, exist_ok=True)
    wake.write_bytes(b"".join(lines))
    return wake


BAD_LINES = [
    pytest.param(b"{not json\n", id="broken-json"),
    pytest.param(b"[1, 2]\n", id="a-list"),
    pytest.param(b"7\n", id="a-number"),
    pytest.param(b"null\n", id="null"),
    pytest.param(b'"text"\n', id="a-string"),
    pytest.param(b'{"sender": "\xff\xfe"}\n', id="not-utf8"),
    pytest.param(b"\xff\xfe\xfd\n", id="only-bad-bytes"),
    pytest.param(b"[" * 200_000 + b"\n", id="nested-too-deep"),
]


@pytest.mark.parametrize("bad", BAD_LINES)
def test_a_bad_line_between_good_lines_skips_only_that_line(
    bad: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    first = _real_line("alice", "first", "d-1")
    last = _real_line("bob", "last", "d-2")
    assert _printed(capsys, _file_of(first, bad, last)) == (
        True,
        [
            HEADER.format(count=2),
            "  alice: first  (agentbus show d-1)",
            "  bob: last  (agentbus show d-2)",
        ],
    )


@pytest.mark.parametrize("bad", BAD_LINES)
def test_a_bad_first_line_does_not_stop_the_rest(
    bad: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    good = _real_line("alice", "only", "d-1")
    assert _printed(capsys, _file_of(bad, good)) == (
        True,
        [HEADER.format(count=1), "  alice: only  (agentbus show d-1)"],
    )


@pytest.mark.parametrize("bad", BAD_LINES)
def test_a_file_of_only_bad_lines_prints_nothing(
    bad: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _printed(capsys, _file_of(bad, bad)) == (False, [])


def test_several_bad_lines_are_skipped_one_at_a_time(
    capsys: pytest.CaptureFixture[str],
) -> None:
    lines = [
        b"{broken\n",
        _real_line("alice", "one", "d-1"),
        b"[1]\n",
        b"\n",
        b"   \n",
        _real_line("bob", "two", "d-2"),
        b"\xff\n",
        _real_line("carol", "three", "d-3"),
    ]
    assert _printed(capsys, _file_of(*lines)) == (
        True,
        [
            HEADER.format(count=3),
            "  alice: one  (agentbus show d-1)",
            "  bob: two  (agentbus show d-2)",
            "  carol: three  (agentbus show d-3)",
        ],
    )


def test_a_line_separator_inside_a_subject_does_not_split_the_line(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A writer may keep U+2028 raw. str.splitlines breaks a line there and
    bytes.splitlines does not."""
    raw = json.dumps(
        {"sender": "alice", "subject": f"a{SEPARATOR}b", "delivery_id": "d-1"}, ensure_ascii=False
    )
    wake = _file_of(raw.encode() + b"\n")
    assert _printed(capsys, wake) == (
        True,
        [HEADER.format(count=1), f"  alice: a{SEPARATOR}b  (agentbus show d-1)"],
    )


def test_every_good_line_prints_when_nothing_is_bad(
    capsys: pytest.CaptureFixture[str],
) -> None:
    lines = [_real_line(f"s{n}", f"subject {n}", f"d-{n}") for n in range(3)]
    assert _printed(capsys, _file_of(*lines)) == (
        True,
        [HEADER.format(count=3)]
        + [f"  s{n}: subject {n}  (agentbus show d-{n})" for n in range(3)],
    )


def test_only_the_first_ten_are_listed_and_the_header_counts_the_good_lines(
    capsys: pytest.CaptureFixture[str],
) -> None:
    lines = [b"{broken\n"]
    lines += [_real_line(f"s{n}", "x", f"d-{n}") for n in range(12)]
    answer, printed = _printed(capsys, _file_of(*lines))
    assert answer is True
    assert printed[0] == HEADER.format(count=12)
    assert printed[1:] == [f"  s{n}: x  (agentbus show d-{n})" for n in range(10)]


def test_an_unreadable_file_prints_nothing(capsys: pytest.CaptureFixture[str]) -> None:
    wake = _wake()
    wake.mkdir(parents=True)
    assert _printed(capsys, wake) == (False, [])


PRINT_THE_FILE = (
    "import pathlib, sys\n"
    "from agentbus_client.hooks import _turn\n"
    "print(_turn._print_stale_wakes(pathlib.Path(sys.argv[1])), file=sys.stderr)\n"
)


def _real_stdout(wake: Path, encoding: str) -> subprocess.CompletedProcess[bytes]:
    """A real process, because a capture stream can accept what a real pipe refuses."""
    env = {**os.environ, "PYTHONPATH": str(SRC), "PYTHONIOENCODING": encoding}
    return subprocess.run(
        [sys.executable, "-c", PRINT_THE_FILE, str(wake)],
        capture_output=True,
        check=False,
        timeout=30,
        env=env,
    )


def _entry(sender: str, subject: str, delivery: str) -> bytes:
    return json.dumps({"sender": sender, "subject": subject, "delivery_id": delivery}).encode()


def test_a_subject_the_pipe_cannot_encode_stops_nothing() -> None:
    """A non-UTF-8 byte in an argv subject reaches the file as a lone surrogate, and
    `print` raised on it, which ended the hook and hid every later line."""
    wake = _file_of(
        _real_line("alice", "before", "d-1"),
        _entry("mallory", "bad\udcffbyte", "d-2") + b"\n",
        _real_line("bob", "after", "d-3"),
    )
    done = _real_stdout(wake, "utf-8:strict")
    assert done.returncode == 0
    assert done.stderr == b"True\n"
    assert done.stdout.decode().splitlines() == [
        HEADER.format(count=3),
        "  alice: before  (agentbus show d-1)",
        "  mallory: bad\\udcffbyte  (agentbus show d-2)",
        "  bob: after  (agentbus show d-3)",
    ]


def test_a_character_an_ascii_pipe_cannot_carry_is_escaped() -> None:
    wake = _file_of(_entry("alice", "caf\u00e9", "d-1") + b"\n")
    done = _real_stdout(wake, "ascii:strict")
    assert done.returncode == 0
    assert done.stdout.decode().splitlines() == [
        HEADER.format(count=1),
        "  alice: caf\\xe9  (agentbus show d-1)",
    ]


def test_a_utf8_pipe_keeps_ordinary_text_unchanged() -> None:
    wake = _file_of(_entry("alice", "caf\u00e9", "d-1") + b"\n")
    done = _real_stdout(wake, "utf-8:strict")
    assert done.stdout.decode().splitlines() == [
        HEADER.format(count=1),
        "  alice: caf\u00e9  (agentbus show d-1)",
    ]


class _FakeStdin(io.StringIO):
    def isatty(self) -> bool:
        return False


class _UnreachableBus:
    def __init__(self, *_a: Any, **_k: Any) -> None:
        pass

    def whoami(self) -> NoReturn:
        msg = "connection refused"
        raise TransportError(msg, code="transport_error")


def test_the_pending_hook_prints_around_a_bad_line_and_keeps_the_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    wake = _file_of(
        _real_line("alice", "before", "d-1"),
        b"{torn write\n",
        _real_line("bob", "after", "d-2"),
    )
    on_disk = wake.read_bytes()
    monkeypatch.setattr("agentbus_client.client.AgentBus", _UnreachableBus)
    monkeypatch.setattr("sys.stdin", _FakeStdin(json.dumps({"session_id": "s", "prompt": "hi"})))
    assert _turn.pending(cast("argparse.Namespace", None)) == 0
    assert capsys.readouterr().out.splitlines() == [
        HEADER.format(count=2),
        "  alice: before  (agentbus show d-1)",
        "  bob: after  (agentbus show d-2)",
        "",
    ]
    assert wake.read_bytes() == on_disk
