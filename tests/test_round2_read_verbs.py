from __future__ import annotations

import argparse
import io
import json
import sys

import pytest

from agentbus_client import cli
from agentbus_client.cli import _read
from agentbus_client.client import Delivery

PAYLOAD = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) + b"\xff\xfe tail"


class _Bus:
    def __init__(self, inbox=None, labels=None, attachments=None) -> None:
        self.calls: list[tuple] = []
        self._inbox = inbox or []
        self._labels = labels or []
        self._attachments = attachments if attachments is not None else [{"filename": "f.bin"}]

    def ack(self, *args, **kwargs):
        self.calls.append(("ack", args, kwargs))
        return {"ok": True}

    def label(self, *args, **kwargs):
        self.calls.append(("label", args, kwargs))
        return self._labels

    def inbox(self, *args, **kwargs):
        self.calls.append(("inbox", args, kwargs))
        return self._inbox

    def read(self, *args, **kwargs):
        self.calls.append(("read", args, kwargs))
        return {"attachments": self._attachments}

    def attachment(self, *args, **kwargs):
        self.calls.append(("attachment", args, kwargs))
        return PAYLOAD


def _install(monkeypatch: pytest.MonkeyPatch, bus: _Bus) -> list:
    given: list = []

    def _factory(args):
        given.append(args)
        return bus

    monkeypatch.setattr(cli._common, "_bus", _factory)
    return given


def _binary_stdout(monkeypatch: pytest.MonkeyPatch, encoding: str = "utf-8") -> io.BytesIO:
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding=encoding))
    return raw


def _delivery(seq: int, *, read: bool = False, role: str | None = None, attachments: int = 0):
    data = {
        "delivery_id": f"01DEL{seq}",
        "agent_seq": seq,
        "state": "delivered",
        "subject": f"subject {seq}",
        "sender_display": f"peer{seq}",
        "read_at": "2026-10-01T00:00:00Z" if read else None,
        "attachment_count": attachments,
    }
    if role is not None:
        data["your_role"] = role
    return Delivery.from_api(data)


def test_ack_sends_each_id_and_prints_each(monkeypatch, capsys) -> None:
    bus = _Bus()
    given = _install(monkeypatch, bus)
    assert cli.main(["ack", "01AAA", "01BBB"]) == 0
    assert bus.calls == [("ack", ("01AAA",), {}), ("ack", ("01BBB",), {})]
    assert capsys.readouterr().out == "acked 01AAA\nacked 01BBB\n"
    assert len(given) == 1
    assert isinstance(given[0], argparse.Namespace)
    assert given[0].command == "ack"


def test_ack_json_flag_keeps_plain_confirmation(monkeypatch, capsys) -> None:
    bus = _Bus()
    _install(monkeypatch, bus)
    assert cli.main(["ack", "--json", "01AAA"]) == 0
    assert bus.calls == [("ack", ("01AAA",), {})]
    assert capsys.readouterr().out == "acked 01AAA\n"


def test_labels_forwards_id_add_and_remove(monkeypatch, capsys) -> None:
    bus = _Bus(labels=["urgent", "billing"])
    given = _install(monkeypatch, bus)
    argv = ["labels", "01DEL", "--add", "urgent", "--add", "billing", "--remove", "old"]
    assert cli.main(argv) == 0
    assert bus.calls == [
        ("label", ("01DEL",), {"add": ["urgent", "billing"], "remove": ["old"]}),
    ]
    assert capsys.readouterr().out == "labels: urgent, billing\n"
    assert given[0].command == "labels"


def test_labels_json_prints_the_list(monkeypatch, capsys) -> None:
    bus = _Bus(labels=["urgent", "billing"])
    _install(monkeypatch, bus)
    assert cli.main(["labels", "01DEL", "--remove", "x", "--json"]) == 0
    out = capsys.readouterr().out
    assert out == json.dumps(["urgent", "billing"], indent=2) + "\n"
    assert json.loads(out) == ["urgent", "billing"]
    assert bus.calls == [("label", ("01DEL",), {"add": [], "remove": ["x"]})]


def test_labels_empty_result(monkeypatch, capsys) -> None:
    bus = _Bus(labels=[])
    _install(monkeypatch, bus)
    assert cli.main(["labels", "01DEL"]) == 0
    assert capsys.readouterr().out == "labels: \n"


def test_inbox_forwards_every_query_argument(monkeypatch, capsys) -> None:
    bus = _Bus(inbox=[])
    given = _install(monkeypatch, bus)
    argv = ["inbox", "--cursor", "7", "--limit", "3", "--label", "ops", "--wait", "9", "--unread"]
    assert cli.main(argv) == 0
    assert bus.calls == [
        ("inbox", (7,), {"limit": 3, "label": "ops", "wait": 9, "unread": True}),
    ]
    assert capsys.readouterr().out == "no new messages\n"
    assert given[0].command == "inbox"


def test_inbox_defaults_forwarded(monkeypatch, capsys) -> None:
    bus = _Bus(inbox=[])
    _install(monkeypatch, bus)
    assert cli.main(["inbox"]) == 0
    assert bus.calls == [
        ("inbox", (0,), {"limit": 50, "label": None, "wait": 0, "unread": False}),
    ]
    assert capsys.readouterr().out == "your inbox is empty\n"


def test_inbox_empty_after_cursor(monkeypatch, capsys) -> None:
    _install(monkeypatch, _Bus(inbox=[]))
    assert cli.main(["inbox", "--cursor", "12"]) == 0
    assert capsys.readouterr().out == "no messages after cursor 12\n"


def test_inbox_json_prints_the_raw_list(monkeypatch, capsys) -> None:
    deliveries = [_delivery(1), _delivery(2, read=True, role="cc")]
    _install(monkeypatch, _Bus(inbox=deliveries))
    assert cli.main(["inbox", "--json"]) == 0
    out = capsys.readouterr().out
    raws = [d.raw for d in deliveries]
    assert out == json.dumps(raws, indent=2) + "\n"
    assert json.loads(out) == raws


def test_inbox_json_empty_list(monkeypatch, capsys) -> None:
    _install(monkeypatch, _Bus(inbox=[]))
    assert cli.main(["inbox", "--json"]) == 0
    assert capsys.readouterr().out == "[]\n"


def test_inbox_listing_lines(monkeypatch, capsys) -> None:
    deliveries = [
        _delivery(3, attachments=2, role="cc"),
        _delivery(4, read=True, role="to"),
    ]
    _install(monkeypatch, _Bus(inbox=deliveries))
    assert cli.main(["inbox", "--limit", "2"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "* #3  peer3  subject 3 [2 attachment(s)]  (cc)",
        "     01DEL3",
        "  #4  peer4  subject 4",
        "     01DEL4",
        "",
        "cursor: 4",
        "  more after this page:  agentbus inbox --cursor 4",
        "  oldest first. Only what is new:  agentbus inbox --unread",
    ]


def test_inbox_listing_unread_short_page(monkeypatch, capsys) -> None:
    _install(monkeypatch, _Bus(inbox=[_delivery(9)]))
    assert cli.main(["inbox", "--unread"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "* #9  peer9  subject 9",
        "     01DEL9",
        "",
        "cursor: 9",
    ]


def test_inbox_cc_marker_only_for_cc(monkeypatch, capsys) -> None:
    deliveries = [_delivery(1, role="cc"), _delivery(2, role="to"), _delivery(3)]
    _install(monkeypatch, _Bus(inbox=deliveries))
    cli.main(["inbox", "--unread"])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == "* #1  peer1  subject 1  (cc)"
    assert lines[2] == "* #2  peer2  subject 2"
    assert lines[4] == "* #3  peer3  subject 3"


def test_attachment_passes_id_and_index(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    bus = _Bus(attachments=[{"filename": "a.txt"}, {"filename": "b.bin"}, {"filename": "c.png"}])
    given = _install(monkeypatch, bus)
    assert cli.main(["attachment", "01DEL", "-i", "2"]) == 0
    assert bus.calls == [("read", ("01DEL",), {}), ("attachment", ("01DEL", 2), {})]
    assert (tmp_path / "c.png").read_bytes() == PAYLOAD
    assert capsys.readouterr().out == f"wrote c.png ({len(PAYLOAD)} bytes)\n"
    assert given[0].command == "attachment"


def test_attachment_dash_writes_raw_bytes_to_stdout(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    bus = _Bus(attachments=[{"filename": "a.txt"}, {"filename": "b.bin"}])
    _install(monkeypatch, bus)
    raw = _binary_stdout(monkeypatch)
    assert cli.main(["attachment", "01DEL", "-i", "1", "-o", "-"]) == 0
    sys.stdout.flush()
    assert raw.getvalue() == PAYLOAD
    assert bus.calls == [("read", ("01DEL",), {}), ("attachment", ("01DEL", 1), {})]
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_attachment_all_passes_each_index(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    bus = _Bus(attachments=[{"filename": "a..b"}, {"filename": "dir\\evil.txt"}])
    _install(monkeypatch, bus)
    assert cli.main(["attachment", "01DEL", "--all"]) == 0
    assert bus.calls == [
        ("read", ("01DEL",), {}),
        ("attachment", ("01DEL", 0), {}),
        ("attachment", ("01DEL", 1), {}),
    ]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["attachment-0", "evil.txt"]
    assert capsys.readouterr().out.splitlines() == [
        f"wrote attachment-0 ({len(PAYLOAD)} bytes)",
        f"wrote evil.txt ({len(PAYLOAD)} bytes)",
        "— 2 attachment(s) written",
    ]


def test_attachment_all_two_hostile_names_get_distinct_fallbacks(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.chdir(tmp_path)
    bus = _Bus(attachments=[{"filename": ".."}, {"filename": "."}])
    _install(monkeypatch, bus)
    assert cli.main(["attachment", "01DEL", "--all"]) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == ["attachment-0", "attachment-1"]
    capsys.readouterr()


def test_safe_attachment_name() -> None:
    assert _read._safe_attachment_name("a..b", 4) == "attachment-4"
    assert _read._safe_attachment_name("...", 2) == "attachment-2"
    assert _read._safe_attachment_name("..bashrc", 1) == "attachment-1"
    assert _read._safe_attachment_name("dir\\evil.txt", 0) == "evil.txt"
    assert _read._safe_attachment_name("../../.ssh/authorized_keys", 3) == "authorized_keys"
    assert _read._safe_attachment_name("..", 5) == "attachment-5"
    assert _read._safe_attachment_name(".", 6) == "attachment-6"
    assert _read._safe_attachment_name("", 7) == "attachment-7"
    assert _read._safe_attachment_name("dir/", 8) == "attachment-8"
    assert _read._safe_attachment_name("report.v2.pdf", 9) == "report.v2.pdf"
