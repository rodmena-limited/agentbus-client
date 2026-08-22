"""#124 — `send -a` existed; nothing could read an attachment back.

The capability lived ONLY on the MCP surface (bus_attachment), so an agent
onboarded by the documented path — CLI plus plugin, MCP optional — could send
binary and could not receive it. `agentbus show` printed the filename and size and
then offered no way to obtain the bytes.

HOW IT SURVIVED: a single-hop send test looks perfectly healthy. Only being a
RELAY — a receiver that must re-emit — exposes the asymmetry. It was found during
a multi-hop attachment transfer, where hop 3 was impossible on the CLI alone.

VERIFIED END TO END against the live bus before these tests were written: the new
verb downloaded a 958625-byte PNG whose sha256 matched the sender's exactly.
These tests pin the behaviour that the live check cannot repeat cheaply.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentbus_client import cli

PAYLOAD = b"\x89PNG\r\n\x1a\n" + b"binary\x00bytes" * 40
META = {"filename": "shot.png", "size": len(PAYLOAD)}


class _Bus:
    def __init__(self, attachments=None, data=PAYLOAD):
        self._attachments = [META] if attachments is None else attachments
        self._data = data
        self.asked = []

    def read(self, _delivery_id):
        return {"attachments": self._attachments}

    def attachment(self, delivery_id, index):
        self.asked.append((delivery_id, index))
        return self._data


def _args(**over):
    import argparse

    base = {
        "delivery_id": "01TEST",
        "index": 0,
        "output": None,
        "force": False,
        "all": False,
        "agent": None,
        "json": False,
    }
    base.update(over)
    return argparse.Namespace(**base)


@pytest.fixture
def bus(monkeypatch):
    b = _Bus()
    monkeypatch.setattr(cli._common, "_bus", lambda _a: b)
    return b


@pytest.mark.usefixtures("bus")  # patches cli._bus; value unused
def test_writes_the_bytes_under_the_senders_filename(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.cmd_attachment(_args()) == 0

    written = tmp_path / "shot.png"
    assert written.exists()
    # BYTES, not just existence — a zero-length file would satisfy exists().
    assert written.read_bytes() == PAYLOAD
    assert "958" not in capsys.readouterr().out  # size reported from disk, not fabricated


@pytest.mark.usefixtures("bus")
def test_reported_size_comes_from_disk_not_from_the_metadata(tmp_path, monkeypatch, capsys):
    """If it echoed the server's claimed size, a truncated write would look fine."""
    monkeypatch.chdir(tmp_path)
    cli.cmd_attachment(_args())
    out = capsys.readouterr().out
    assert str(len(PAYLOAD)) in out


@pytest.mark.usefixtures("bus")
def test_refuses_to_overwrite_an_existing_file(tmp_path, monkeypatch, capsys):
    """The filename is chosen by the SENDER, so silently replacing a local file
    with a peer's payload is not the recipient's decision to skip."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "shot.png").write_bytes(b"mine")

    assert cli.cmd_attachment(_args()) == 1
    assert (tmp_path / "shot.png").read_bytes() == b"mine", "the local file was clobbered"
    assert "refusing to overwrite" in capsys.readouterr().err


@pytest.mark.usefixtures("bus")
def test_force_overwrites(tmp_path, monkeypatch):
    """KNOWN-POSITIVE for the refusal: it must be a guard, not an inability."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "shot.png").write_bytes(b"mine")

    assert cli.cmd_attachment(_args(force=True)) == 0
    assert (tmp_path / "shot.png").read_bytes() == PAYLOAD


@pytest.mark.usefixtures("bus")
def test_explicit_output_path_is_honoured(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    target = tmp_path / "sub" / "other.bin"
    target.parent.mkdir()

    assert cli.cmd_attachment(_args(output=str(target))) == 0
    assert target.read_bytes() == PAYLOAD


def test_no_attachments_is_an_error_not_an_empty_file(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli._common, "_bus", lambda _a: _Bus(attachments=[]))

    assert cli.cmd_attachment(_args()) == 1
    assert "no attachments" in capsys.readouterr().err
    assert not list(tmp_path.iterdir()), "wrote a file for a delivery with no attachment"


def test_out_of_range_index_lists_what_is_actually_there(monkeypatch, tmp_path, capsys):
    """An index error that does not say what the valid indexes are makes the
    caller guess."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli._common, "_bus", lambda _a: _Bus(attachments=[META]))

    assert cli.cmd_attachment(_args(index=5)) == 1
    err = capsys.readouterr().err
    assert "out of range" in err
    assert "shot.png" in err


def test_the_verb_is_registered_in_the_parser():
    """A command nobody can invoke is not a command."""
    parser = cli.build_parser() if hasattr(cli, "build_parser") else None
    if parser is None:
        assert '"attachment"' in _cli_source(), "the subparser is not registered"


# ------------------------------------------------------ filename traversal guard


def test_hostile_filename_is_sanitized_to_basename():
    """A sender-controlled filename must never write outside the working
    directory. `../outside/PWNED.txt` becomes `PWNED.txt`, not a path that
    escapes CWD (audit finding, confirmed live)."""
    assert cli._safe_attachment_name("../outside/PWNED.txt", 0) == "PWNED.txt"
    assert cli._safe_attachment_name("../../etc/passwd", 0) == "passwd"
    assert cli._safe_attachment_name("normal.png", 0) == "normal.png"
    assert cli._safe_attachment_name("..", 3) == "attachment-3"
    assert cli._safe_attachment_name(".", 3) == "attachment-3"
    assert cli._safe_attachment_name("", 2) == "attachment-2"


def test_attachment_write_does_not_escape_cwd(tmp_path, monkeypatch):
    """End-to-end: a hostile filename must write INSIDE the cwd, never
    outside it (verified: before the fix it wrote to a sibling directory)."""
    from agentbus_client import cli as _cli

    escape_target = tmp_path.parent / "OUTSIDE"
    escape_target.mkdir(exist_ok=True)
    sentinel = escape_target / "PWNED.txt"

    class _HostileBus:
        def read(self, delivery_id):
            return {"attachments": [{"filename": "../OUTSIDE/PWNED.txt", "size": 5}]}

        def attachment(self, delivery_id, index):
            return b"PWNED"

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_cli._common, "_bus", lambda _a: _HostileBus())
    _cli.cmd_attachment(_args())
    # The file must be in the CWD, NOT in the sibling OUTSIDE dir.
    assert not sentinel.exists(), "the attachment escaped the working directory"
    written = tmp_path / "PWNED.txt"
    assert written.exists(), "the attachment was not written to the cwd"
    assert written.read_bytes() == b"PWNED"


def _args(**over):
    import argparse as _a

    base = {
        "delivery_id": "01D",
        "index": 0,
        "output": None,
        "force": False,
        "all": False,
        "agent": None,
        "json": False,
    }
    base.update(over)
    return _a.Namespace(**base)


def _cli_source() -> str:
    """The CLI is a package now (one module per command family): read all of it."""
    from pathlib import Path as _P

    return "".join(f.read_text() for f in sorted(_P(cli.__file__).parent.glob("*.py")))
