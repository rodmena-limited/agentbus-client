from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from agentbus_client import cli


class _Bus:
    def __init__(self, attachments: list[dict[str, object]], data: list[bytes]) -> None:
        self._attachments = attachments
        self._data = data
        self.fetched: list[int] = []

    def read(self, _delivery_id: str) -> dict[str, object]:
        return {"attachments": self._attachments}

    def attachment(self, _delivery_id: str, index: int) -> bytes:
        self.fetched.append(index)
        return self._data[index]


def _args(**over: object) -> argparse.Namespace:
    base: dict[str, object] = {
        "delivery_id": "01TEST",
        "index": 0,
        "output": None,
        "force": False,
        "all": True,
        "agent": None,
        "json": False,
    }
    base.update(over)
    return argparse.Namespace(**base)


def _install(monkeypatch: pytest.MonkeyPatch, bus: _Bus) -> None:
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)


@pytest.mark.parametrize("force", [False, True])
def test_two_attachments_with_one_name_are_refused_before_any_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], force: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    bus = _Bus(
        [{"filename": "report.pdf", "size": 5}, {"filename": "report.pdf", "size": 6}],
        [b"FIRST", b"SECOND"],
    )
    _install(monkeypatch, bus)
    assert cli.cmd_attachment(_args(force=force)) == 1
    assert list(tmp_path.iterdir()) == []
    assert bus.fetched == []
    err = capsys.readouterr().err
    assert "report.pdf (attachments 0, 1)" in err


def test_names_that_collide_only_after_sanitising_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    bus = _Bus(
        [{"filename": "../notes.txt", "size": 1}, {"filename": "notes.txt", "size": 1}],
        [b"a", b"b"],
    )
    _install(monkeypatch, bus)
    assert cli.cmd_attachment(_args()) == 1
    assert list(tmp_path.iterdir()) == []


def test_distinct_names_are_all_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    bus = _Bus(
        [{"filename": "a.txt", "size": 5}, {"filename": "b.txt", "size": 6}],
        [b"FIRST", b"SECOND"],
    )
    _install(monkeypatch, bus)
    assert cli.cmd_attachment(_args()) == 0
    assert (tmp_path / "a.txt").read_bytes() == b"FIRST"
    assert (tmp_path / "b.txt").read_bytes() == b"SECOND"
