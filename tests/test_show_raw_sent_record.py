"""#97: `show --raw` on a message the caller SENT.

Reported by ledger-ae6b91. GET /v1/deliveries/<message id> for the sender's own
message answers with the message record, which carries `record: "message"` and
no `sealed` key. The keys below are the set production returned on 2026-11-16
for message 01M6S7395E6VQ9XEX6BW3S3RJS, with the values shortened.
"""

from __future__ import annotations

import io
import json
import sys
from typing import Any

import httpx
import pytest

from agentbus_client import cli
from agentbus_client.client import AgentBus

BASE = "https://bus.test"
ARMOR = (
    "-----BEGIN AGE ENCRYPTED FILE-----\n"
    "YWdlLWVuY3J5cHRpb24ub3JnL3YxCi0+IFgyNTUxOSBRbDNCdS9FUUR3dEJhMkcx\n"
    "-----END AGE ENCRYPTED FILE-----\n"
)


def _sent_record(text_body: str) -> dict[str, Any]:
    return {
        "attachments": [],
        "auth_verdicts": None,
        "body_sha256": "0" * 64,
        "created_at": "2026-11-16T20:00:00Z",
        "derived_from": None,
        "direction": "bus",
        "html_body": None,
        "id": "01M6S7395E6VQ9XEX6BW3S3RJS",
        "in_reply_to": None,
        "inbound_source": None,
        "lane": None,
        "payload": None,
        "payload_schema_ref": None,
        "priority": "normal",
        "provenance": {"level": "platform_attested"},
        "raw_reconstructed": False,
        "recipient_count": 1,
        "recipients": [{"recipient": "peer-1", "kind": "to"}],
        "record": "message",
        "sender_address": "agentbus+me@example.test",
        "sender_display": "me via AgentBus",
        "signature": None,
        "signature_state": None,
        "signed_recipients": None,
        "signing_key_fingerprint": None,
        "subject": "s",
        "text_body": text_body,
        "thread_id": "01THREAD",
        "thread_seq": 1,
    }


def _show_raw(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    record: dict[str, Any],
) -> tuple[int, bytes, str, list[httpx.Request]]:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=record)

    bus = AgentBus(api_key="ab_sk_x_y", base_url=BASE, agent="me-1")
    bus._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)
    monkeypatch.setattr(cli._common, "_bus", lambda _args: bus)
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="utf-8"))
    code = cli.main(["show", record.get("id", "01DEL"), "--raw"])
    sys.stdout.flush()
    return code, raw.getvalue(), capsys.readouterr().err, requests


def test_the_wire_record_really_has_no_sealed_key() -> None:
    record = _sent_record(ARMOR)
    assert "sealed" not in record
    assert record["record"] == "message"


def test_sent_ciphertext_is_reported_as_ciphertext(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err, requests = _show_raw(monkeypatch, capsys, _sent_record(ARMOR))
    assert code == 0
    assert out == ARMOR.encode()
    assert "NOT sealed" not in err
    assert err == (
        "note: the server did not say whether this record is sealed; the stored "
        "bytes below are age ciphertext.\n"
    )
    assert [r.url.path for r in requests] == ["/v1/deliveries/01M6S7395E6VQ9XEX6BW3S3RJS"]


def test_sent_plaintext_with_no_sealed_key_is_not_called_ciphertext(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err, _ = _show_raw(monkeypatch, capsys, _sent_record("api key: sk_live_1\n"))
    assert code == 0
    assert out == b"api key: sk_live_1\n"
    assert "age ciphertext" in err
    assert "are not age ciphertext" in err
    assert "prove nothing about encryption" in err


def test_armor_not_at_the_start_is_not_ciphertext(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    body = "quoted below:\n" + ARMOR
    _, out, err, _ = _show_raw(monkeypatch, capsys, _sent_record(body))
    assert out == body.encode()
    assert "are not age ciphertext" in err


@pytest.mark.parametrize(
    ("sealed", "body", "expected"),
    [
        (True, ARMOR, ""),
        (
            True,
            "plain\n",
            (
                "note: the server marks this delivery sealed, but the stored bytes "
                "below are not age ciphertext.\n"
            ),
        ),
        (
            False,
            ARMOR,
            (
                "note: this delivery is NOT sealed — the bytes below are the stored "
                "plaintext, not ciphertext, and prove nothing about encryption.\n"
            ),
        ),
        (
            False,
            "plain\n",
            (
                "note: this delivery is NOT sealed — the bytes below are the stored "
                "plaintext, not ciphertext, and prove nothing about encryption.\n"
            ),
        ),
        (
            "true",
            ARMOR,
            (
                "note: the server did not say whether this record is sealed; the stored "
                "bytes below are age ciphertext.\n"
            ),
        ),
    ],
)
def test_every_seal_state_gets_its_own_note(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sealed: object,
    body: str,
    expected: str,
) -> None:
    record = _sent_record(body)
    record["sealed"] = sealed
    code, out, err, _ = _show_raw(monkeypatch, capsys, record)
    assert code == 0
    assert out == body.encode()
    assert err == expected


def test_json_raw_on_a_sent_record_prints_the_record_and_no_note(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    record = _sent_record(ARMOR)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=record)

    bus = AgentBus(api_key="ab_sk_x_y", base_url=BASE, agent="me-1")
    bus._client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)
    monkeypatch.setattr(cli._common, "_bus", lambda _args: bus)
    assert cli.main(["--json", "show", record["id"], "--raw"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["text_body"] == ARMOR
    assert captured.err == ""
