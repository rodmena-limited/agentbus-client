"""Quick-win fixes from the joint AgentBus test round with agentbus-ui-c760a1:

F8  (#5)  `agentbus attachment --all` fetches every attachment on a delivery
F11 (#6)  `agentbus show` labels the on-wire size honestly, not as plaintext
F13 (#7)  `agentbus send --guarantee fire_and_forget --json` returns a stable
          {status, guarantee, ...} shape instead of {}
F14 (#8)  `agentbus verify` prints `UNSIGNED — no signature attached to
          verify` instead of the `UNSIGNED — unsigned` glitch
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentbus_client import cli

# --------------------------------------------------------------------- F8

PAYLOADS = [b"one" * 10, b"two" * 20, b"three" * 30]
METAS = [
    {"filename": "a.bin", "size": len(PAYLOADS[0])},
    {"filename": "b.bin", "size": len(PAYLOADS[1])},
    {"filename": "c.bin", "size": len(PAYLOADS[2])},
]


class _MultiBus:
    def __init__(self, metas=None, datas=None):
        self._metas = metas if metas is not None else METAS
        self._datas = datas if datas is not None else PAYLOADS
        self.calls: list[tuple[str, int]] = []

    def read(self, _delivery_id, raw: bool = False):
        return {"attachments": self._metas}

    def attachment(self, delivery_id, index):
        self.calls.append((delivery_id, index))
        return self._datas[index]


def _attach_args(**over):
    base = {
        "delivery_id": "01ALL",
        "index": 0,
        "output": None,
        "force": False,
        "all": False,
        "agent": None,
        "json": False,
    }
    base.update(over)
    return argparse.Namespace(**base)


def test_all_flag_fetches_every_attachment(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    bus = _MultiBus()
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)

    assert cli.cmd_attachment(_attach_args(all=True)) == 0

    # Every payload landed on disk under its original filename.
    for meta, payload in zip(METAS, PAYLOADS):
        target = tmp_path / meta["filename"]
        assert target.exists(), f"{meta['filename']} was not written"
        assert target.read_bytes() == payload
    # One fetch per attachment, in order.
    assert bus.calls == [("01ALL", 0), ("01ALL", 1), ("01ALL", 2)]
    # Summary line names how many were written.
    assert "3 attachment(s) written" in capsys.readouterr().out


def test_all_refuses_when_any_target_already_exists(tmp_path, monkeypatch, capsys):
    """Never a partial write of half the set."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.bin").write_bytes(b"local file")
    bus = _MultiBus()
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)

    assert cli.cmd_attachment(_attach_args(all=True)) == 1
    # Nothing was fetched or written — the pre-check must fail FIRST.
    assert bus.calls == []
    assert (tmp_path / "b.bin").read_bytes() == b"local file"
    assert not (tmp_path / "a.bin").exists()
    assert not (tmp_path / "c.bin").exists()
    err = capsys.readouterr().err
    assert "b.bin" in err
    assert "refusing to overwrite" in err


def test_all_with_force_overwrites_every_conflict(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.bin").write_bytes(b"old")
    (tmp_path / "c.bin").write_bytes(b"old")
    monkeypatch.setattr(cli._common, "_bus", lambda _a: _MultiBus())

    assert cli.cmd_attachment(_attach_args(all=True, force=True)) == 0
    assert (tmp_path / "a.bin").read_bytes() == PAYLOADS[0]
    assert (tmp_path / "b.bin").read_bytes() == PAYLOADS[1]
    assert (tmp_path / "c.bin").read_bytes() == PAYLOADS[2]


def test_all_refuses_output_flag_because_they_are_mutually_exclusive(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli._common, "_bus", lambda _a: _MultiBus())

    assert cli.cmd_attachment(_attach_args(all=True, output="somewhere")) == 2
    assert "-o" in capsys.readouterr().err


def test_all_refuses_stdout_because_bytes_would_interleave(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli._common, "_bus", lambda _a: _MultiBus())

    assert cli.cmd_attachment(_attach_args(all=True, output="-")) == 2
    assert "interleave" in capsys.readouterr().err


# --------------------------------------------------------------------- F14


def _verify_args(**over):
    base = {"delivery_id": "01V", "json": False, "agent": None}
    base.update(over)
    return argparse.Namespace(**base)


class _VerifyBus:
    def __init__(self, result):
        self._result = result

    def verify(self, _delivery_id):
        return self._result


def test_unsigned_verdict_does_not_repeat_the_word(monkeypatch, capsys):
    monkeypatch.setattr(
        cli._common,
        "_bus",
        lambda _a: _VerifyBus(
            {
                "verified": False,
                "verdict": "unsigned",
                "reason": "unsigned",
                "platform_said": "unsigned",
            }
        ),
    )
    code = cli.cmd_verify_signature(_verify_args())
    assert code == 2
    out = capsys.readouterr().out
    # The old "UNSIGNED — unsigned" glitch is gone.
    assert "UNSIGNED — unsigned" not in out
    # The new line leads with reassurance.
    assert "UNSIGNED — no signature attached to verify" in out


def test_cannot_verify_still_carries_the_reason(monkeypatch, capsys):
    """Regression: the CANNOT VERIFY branch used the same headline+reason
    pattern; make sure it still names why the tool could not check."""
    monkeypatch.setattr(
        cli._common,
        "_bus",
        lambda _a: _VerifyBus(
            {
                "verified": False,
                "verdict": "unverifiable",
                "reason": "key not published for this agent",
                "platform_said": "unverifiable",
            }
        ),
    )
    code = cli.cmd_verify_signature(_verify_args())
    assert code == 2
    out = capsys.readouterr().out
    assert "CANNOT VERIFY — key not published for this agent" in out


# --------------------------------------------------------------------- F11


class _ShowBus:
    def __init__(self, delivery):
        self._delivery = delivery

    def read(self, _delivery_id, raw: bool = False):
        return self._delivery


def _show_args(**over):
    base = {"delivery_id": "01S", "json": False, "thread": False, "agent": None}
    base.update(over)
    return argparse.Namespace(**base)


def test_show_labels_attachment_size_as_on_wire(monkeypatch, capsys):
    delivery = {
        "sender_display": "peer",
        "sender_address": "peer@example",
        "recipients": [{"recipient": "me", "kind": "to"}],
        "your_role": "to",
        "subject": "with an attachment",
        "thread_id": "01T",
        "text_body": "hi",
        "attachments": [{"filename": "shot.png", "size": 69675}],
        "message_id": "01M",
    }
    monkeypatch.setattr(cli._common, "_bus", lambda _a: _ShowBus(delivery))
    assert cli.cmd_show(_show_args()) == 0
    out = capsys.readouterr().out
    assert "-- attachment: shot.png" in out
    # The exact byte count is present AND labelled honestly.
    assert "69,675 bytes on wire" in out
    # The old misleading "(69675 bytes)" form is gone.
    assert "(69675 bytes)" not in out


# --------------------------------------------------------------------- F13


class _SendBus:
    def __init__(self, response):
        self._response = response
        self.agent = "test-agent"

    def send(self, *args, **kwargs):
        return self._response


def _send_args(**over):
    base = {
        "to": ["someone"],
        "cc": None,
        "priority": None,
        "subject": "s",
        "body": "b",
        "attach": [],
        "require_available": False,
        "payload": None,
        "guarantee": "fire_and_forget",
        "derived_from": [],
        "json": True,
        "agent": None,
    }
    base.update(over)
    return argparse.Namespace(**base)


def test_fire_and_forget_json_is_never_empty(monkeypatch, capsys):
    """A jq consumer must never crash on {}."""
    monkeypatch.setattr(cli._common, "_bus", lambda _a: _SendBus({}))
    monkeypatch.setattr(cli._common, "_read_body", lambda body: body or "")

    assert cli.cmd_send(_send_args()) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert data.get("status") == "accepted"
    assert data.get("guarantee") == "fire_and_forget"


def test_fire_and_forget_json_preserves_real_server_fields(monkeypatch, capsys):
    """A newer server returns {guarantee, stored, live_subscribers, ...}. The
    normaliser must PRESERVE those fields, not replace them."""
    server_body = {
        "guarantee": "fire_and_forget",
        "stored": False,
        "live_subscribers": ["peer-a", "peer-b"],
        "reached": 2,
        "not_listening": [],
        "meaning": "delivered to live subscribers; nothing stored",
    }
    monkeypatch.setattr(cli._common, "_bus", lambda _a: _SendBus(server_body))
    monkeypatch.setattr(cli._common, "_read_body", lambda body: body or "")

    assert cli.cmd_send(_send_args()) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "accepted"
    assert data["reached"] == 2
    assert data["live_subscribers"] == ["peer-a", "peer-b"]
    assert data["stored"] is False


def test_durable_send_is_unchanged(monkeypatch, capsys):
    """Guarantee=durable (or None) must not get the fire_and_forget status
    marker glued on."""
    monkeypatch.setattr(
        cli._common,
        "_bus",
        lambda _a: _SendBus(
            {
                "id": "01M",
                "thread_id": "01T",
                "delivery_count": 1,
                "cc": [],
            }
        ),
    )
    monkeypatch.setattr(cli._common, "_read_body", lambda body: body or "")
    args = _send_args(guarantee="durable")
    assert cli.cmd_send(args) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["id"] == "01M"
    assert "status" not in data
    assert "guarantee" not in data


def test_fire_and_forget_text_summary_does_not_keyerror(monkeypatch, capsys):
    """The non-JSON receipt used to read `result['id']`, `['delivery_count']`,
    `['thread_id']` — all absent on fire_and_forget."""
    monkeypatch.setattr(cli._common, "_bus", lambda _a: _SendBus({"reached": 3}))
    monkeypatch.setattr(cli._common, "_read_body", lambda body: body or "")
    args = _send_args(json=False)
    assert cli.cmd_send(args) == 0
    out = capsys.readouterr().out
    assert "fire_and_forget" in out
    assert "reached 3 live subscriber(s)" in out
