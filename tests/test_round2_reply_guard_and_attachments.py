from __future__ import annotations

import base64
from pathlib import Path

import pytest

from agentbus_client.client import AgentBusError, SelfReplyError
from agentbus_client.client._reply_guard import _refuse_self_reply
from agentbus_client.client.attachments import _encode_attachments

DETAIL = (
    "reply to msg_1 would reach only YOU (me): that id is a "
    "message you sent, so 'answer the sender' means yourself. Reply to the "
    "other party's message id instead, or pass allow_self=True / --to-self "
    "if a note to yourself is what you meant."
)


def _refusal(resolved: dict[str, object]) -> SelfReplyError:
    with pytest.raises(SelfReplyError) as info:
        _refuse_self_reply(resolved, "me", "msg_1", allow_self=False)
    return info.value


def test_refusal_body_is_the_resolved_recipient_set() -> None:
    exc = _refusal({"to": ["me"], "cc": []})
    assert exc.body == {"to": ["me"], "cc": []}
    assert list(exc.body) == ["to", "cc"]


def test_refusal_body_normalizes_missing_and_tuple_fields_to_lists() -> None:
    assert _refusal({"to": ("me",)}).body == {"to": ["me"], "cc": []}
    assert _refusal({"to": ["me"], "cc": None}).body == {"to": ["me"], "cc": []}
    assert type(_refusal({"to": ("me",), "cc": ()}).body["to"]) is list


def test_refusal_detail_explains_and_names_the_remedy() -> None:
    exc = _refusal({"to": ["me"], "cc": []})
    assert exc.detail == DETAIL
    assert str(exc) == DETAIL
    assert exc.code == "self_reply_refused"
    assert exc.status == 0
    assert exc.message_id == "msg_1"
    assert exc.acting == "me"
    assert isinstance(exc, AgentBusError)


def test_refusal_detail_carries_the_call_specific_names() -> None:
    with pytest.raises(SelfReplyError) as info:
        _refuse_self_reply({"to": ["alice"], "cc": []}, "alice", "01ABC", allow_self=False)
    assert info.value.detail.startswith("reply to 01ABC would reach only YOU (alice): ")
    assert info.value.body == {"to": ["alice"], "cc": []}


def test_a_reply_to_someone_else_or_to_two_selves_is_not_refused() -> None:
    assert _refuse_self_reply({"to": ["peer"], "cc": []}, "me", "m", allow_self=False) is None
    assert _refuse_self_reply({"to": ["me", "me"], "cc": []}, "me", "m", allow_self=False) is None
    assert _refuse_self_reply({"to": ["me"], "cc": ["me"]}, "me", "m", allow_self=False) is None
    assert _refuse_self_reply({}, "me", "m", allow_self=False) is None
    assert _refuse_self_reply({"to": ["me"]}, "", "m", allow_self=False) is None


def _write(path: Path, size: int) -> Path:
    path.write_bytes(b"a" * size)
    return path


def test_txt_is_declared_text_plain(tmp_path: Path) -> None:
    f = _write(tmp_path / "notes.txt", 5)
    assert _encode_attachments([str(f)]) == [
        {
            "filename": "notes.txt",
            "content_base64": base64.b64encode(b"aaaaa").decode(),
            "content_type": "text/plain",
        }
    ]


@pytest.mark.parametrize("name", ["blob.zzqq9", "README"])
def test_an_unguessable_name_is_declared_octet_stream(tmp_path: Path, name: str) -> None:
    f = tmp_path / name
    f.write_bytes(b"\x00\x01\xff")
    [entry] = _encode_attachments([str(f)])
    assert entry == {
        "filename": name,
        "content_base64": "AAH/",
        "content_type": "application/octet-stream",
    }


def test_several_files_keep_order_and_each_their_own_type(tmp_path: Path) -> None:
    a = _write(tmp_path / "a.json", 2)
    b = _write(tmp_path / "b.unknownext", 1)
    out = _encode_attachments([str(a), str(b)])
    assert [(e["filename"], e["content_type"]) for e in out] == [
        ("a.json", "application/json"),
        ("b.unknownext", "application/octet-stream"),
    ]
    assert [set(e) for e in out] == [{"filename", "content_base64", "content_type"}] * 2


def test_no_paths_is_an_empty_payload() -> None:
    assert _encode_attachments(None) == []
    assert _encode_attachments([]) == []


def test_exactly_the_server_cap_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTBUS_SERVER_MAX_ATTACHMENT_BYTES", "64")
    monkeypatch.setenv("AGENTBUS_MAX_ATTACHMENT_BYTES", "1000")
    f = _write(tmp_path / "edge.bin", 64)
    [entry] = _encode_attachments([str(f)])
    assert base64.b64decode(entry["content_base64"]) == b"a" * 64


def test_one_byte_over_the_server_cap_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTBUS_SERVER_MAX_ATTACHMENT_BYTES", "64")
    monkeypatch.setenv("AGENTBUS_MAX_ATTACHMENT_BYTES", "1000")
    f = _write(tmp_path / "over.bin", 65)
    with pytest.raises(AgentBusError) as info:
        _encode_attachments([str(f)])
    assert str(info.value).startswith(
        "attachment 'over.bin' is 65 bytes; the AgentBus server rejects attachments "
        "over 64 bytes (~0 MiB)."
    )
    assert "AGENTBUS_SERVER_MAX_ATTACHMENT_BYTES" in str(info.value)


def test_exactly_the_client_cap_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTBUS_SERVER_MAX_ATTACHMENT_BYTES", "1000")
    monkeypatch.setenv("AGENTBUS_MAX_ATTACHMENT_BYTES", "64")
    f = _write(tmp_path / "edge.bin", 64)
    assert len(_encode_attachments([str(f)])) == 1


def test_one_byte_over_the_client_cap_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTBUS_SERVER_MAX_ATTACHMENT_BYTES", "1000")
    monkeypatch.setenv("AGENTBUS_MAX_ATTACHMENT_BYTES", "64")
    f = _write(tmp_path / "over.bin", 65)
    with pytest.raises(AgentBusError) as info:
        _encode_attachments([str(f)])
    assert str(info.value).startswith(
        "attachment 'over.bin' is 65 bytes; the client cap is 64 bytes (~0 MB)."
    )
    assert "AGENTBUS_MAX_ATTACHMENT_BYTES" in str(info.value)


def test_the_server_cap_error_states_the_encrypted_effective_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTBUS_SERVER_MAX_ATTACHMENT_BYTES", str(10 * 1024 * 1024))
    f = tmp_path / "big.bin"
    with open(f, "wb") as handle:
        handle.seek(10 * 1024 * 1024)
        handle.write(b"\0")
    with pytest.raises(AgentBusError) as info:
        _encode_attachments([str(f)])
    msg = str(info.value)
    assert "is 10,485,761 bytes" in msg
    assert "over 10,485,760 bytes (~10 MiB)" in msg
    assert "sealing inflates by x1.806" in msg
    assert "largest raw file that fits is ~5,806,068 bytes (~5 MiB), not 10 MiB." in msg


def test_a_missing_file_is_a_named_error(tmp_path: Path) -> None:
    with pytest.raises(AgentBusError) as info:
        _encode_attachments([str(tmp_path / "gone.txt")])
    assert str(info.value).startswith(f"cannot read attachment '{tmp_path / 'gone.txt'}': ")
