from __future__ import annotations

from agentbus_client.cli import _sigline

CHECK = "         check it yourself:  agentbus verify-sender DEL"


def test_flat_signature_without_state_is_attached_no_verdict() -> None:
    lines = _sigline.signature_lines({"signature": "base64sig"}, "DEL")
    assert lines == [
        "Signed:  a signature is attached but the bus returned no verdict on it,",
        "         so nothing about this message has been checked by anyone;",
        CHECK,
    ]


def test_flat_signature_with_fingerprint_names_the_key() -> None:
    lines = _sigline.signature_lines(
        {"signature": "base64sig", "signing_key_fingerprint": "ab12"}, "DEL"
    )
    assert lines[0] == (
        "Signed:  a signature is attached (key ab12) but the bus returned no verdict on it,"
    )


def test_flat_signature_null_is_served_and_unsigned() -> None:
    lines = _sigline.signature_lines({"signature": None}, "DEL")
    assert lines == [
        "Signed:  no — there is no signature here to verify. This is NOT a failed",
        "         signature: the sender may publish no signing key at all.",
    ]


def test_nothing_served_is_unknown() -> None:
    lines = _sigline.signature_lines({"subject": "x"}, "DEL")
    assert lines == [
        "Signed:  UNKNOWN — this server did not report whether the message is",
        "         signed, so this is neither a yes nor a no. The only answer is",
        CHECK,
    ]


def test_flat_signature_with_valid_state() -> None:
    lines = _sigline.signature_lines(
        {"signature": "s", "signature_state": "valid", "signing_key_fingerprint": "ab12"}, "DEL"
    )
    assert lines[0] == "Signed:  yes (key ab12) — the BUS says it verifies."
    assert lines[2] == CHECK


def test_flat_state_alone_counts_as_signed() -> None:
    lines = _sigline.signature_lines({"signature_state": "invalid"}, "DEL")
    assert lines[0] == "Signed:  the bus reports 'invalid' for the signature on this message."


def test_thread_line_recognises_the_flat_signature_field() -> None:
    assert _sigline.thread_signature_line({"signature": "base64sig"}) == (
        "    signed: a signature is attached, with no verdict from the bus"
    )
    assert _sigline.thread_signature_line({"signature": None}) == (
        "    signed: no signature on this message"
    )
    assert _sigline.thread_signature_line({"subject": "x"}) is None


def test_thread_caveat_respects_the_flat_signature_field() -> None:
    assert _sigline.thread_signature_caveat([{"signature": None}]) is None
    assert _sigline.thread_signature_caveat([{"subject": "a"}, {"signature": "s"}]) is None
    assert _sigline.thread_signature_caveat([{"subject": "a"}]) == _sigline._UNKNOWN_IN_THREAD


def test_thread_caveat_absent_when_served_but_unsigned() -> None:
    messages = [{"provenance": {"signature": {"signed": False}}}, {"signature": None}]
    assert _sigline.thread_signature_caveat(messages) is None


def test_source_tuple_for_flat_fields() -> None:
    assert _sigline._source({"signature": "s", "signing_key_fingerprint": "fp"}) == (
        True,
        True,
        None,
        "fp",
    )
    assert _sigline._source({"signature": None}) == (True, False, None, None)
    assert _sigline._source({}) == (False, False, None, None)


def test_provenance_block_wins_over_flat_fields() -> None:
    delivery = {
        "signature": None,
        "provenance": {"signature": {"signed": True, "state": "valid", "key_fingerprint": "k9"}},
    }
    assert _sigline._source(delivery) == (True, True, "valid", "k9")
