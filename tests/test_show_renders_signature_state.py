"""#64: `show` must say something about the signature on every delivery.

Live-confirmed branches, against agentbus.rodmena.co.uk on 2026-09-22 with the
working tree installed in .venv:

    01M33CVNJMHDEND4PS603456R2  signature_state 'valid'     -> the signed render
    01M18JA0WVM1F8NDR7QYA7Y0BS  signature_state None, 0 att -> "nothing to verify"
    01M33D0HF4QRKKM9KG94NCH774  signature_state None, 1 att -> the structural render

The 'invalid' and no-verdict branches cannot be produced against the real bus
without forging a signature, so they are covered here only at the renderer.
"""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout

from agentbus_client import cli
from agentbus_client.cli import _sigline

_UNSIGNED = {"signature": {"signed": False, "means": "no signature."}}


def _delivery(**over) -> dict:
    base = {
        "message_id": "msg_1",
        "thread_id": "th_1",
        "subject": "a message",
        "sender_display": "peer",
        "sender_address": "peer@example.test",
        "text_body": "the body",
        "recipients": [{"recipient": "me", "kind": "to"}],
        "your_role": "to",
        "thread_message_count": 1,
    }
    base.update(over)
    return base


class FakeBus:
    def __init__(self, delivery: dict) -> None:
        self._delivery = delivery

    def read(self, delivery_id: str, raw: bool = False) -> dict:
        return self._delivery

    def thread(self, thread_id: str) -> dict:
        raise AssertionError("show must not fetch the thread to render Signed:")


def _run_show(monkeypatch, delivery: dict) -> str:
    monkeypatch.setattr(cli._common, "_bus", lambda _args: FakeBus(delivery))
    args = argparse.Namespace(delivery_id="del_1", json=False, thread=False, agent=None)
    buf = io.StringIO()
    with redirect_stdout(buf):
        assert cli.cmd_show(args) == 0
    return buf.getvalue()


def test_a_valid_signature_is_rendered_with_its_key_and_the_check_command(monkeypatch):
    out = _run_show(
        monkeypatch,
        _delivery(signature_state="valid", signing_key_fingerprint="53965be076ddea7b"),
    )
    assert "Signed:" in out
    assert "53965be076ddea7b" in out
    assert "agentbus verify-sender del_1" in out


def test_show_never_claims_the_word_this_client_reserves_for_a_real_check(monkeypatch):
    """`verify-sender` prints VERIFIED after checking on this machine. `show`
    has checked nothing, so it must not borrow the word (#173)."""
    out = _run_show(
        monkeypatch,
        _delivery(signature_state="valid", signing_key_fingerprint="53965be076ddea7b"),
    )
    assert "VERIFIED" not in out
    assert "BUS says" in out


def test_an_unsigned_delivery_still_gets_a_line(monkeypatch):
    """THE TRAP. `if delivery.get("signature_state"):` is falsy on None, so the
    delivery a reader is actually exposed to renders as silence — the reported
    bug preserved. Memory #4: null must render differently from a real value,
    not identically to 'field absent'."""
    out = _run_show(monkeypatch, _delivery(provenance=_UNSIGNED))
    assert "Signed:  no" in out
    assert "NOT a failed" in out
    assert "no signing key" in out


def test_signed_and_unsigned_do_not_render_the_same(monkeypatch):
    signed = _run_show(
        monkeypatch, _delivery(signature_state="valid", signing_key_fingerprint="abc")
    )
    unsigned = _run_show(monkeypatch, _delivery(provenance=_UNSIGNED))
    assert "Signed:" in signed and "Signed:" in unsigned
    assert signed != unsigned


def test_an_attachment_explains_the_absence_without_attesting_to_it(monkeypatch):
    out = _run_show(
        monkeypatch,
        _delivery(provenance=_UNSIGNED, attachments=[{"filename": "bundle.git", "size": 10}]),
    )
    assert "1 attachment(s)" in out
    assert "agentbus-sig-v1" in out
    assert "does not attest to it" in out
    assert "stripped" in out


def test_an_html_body_or_a_payload_explains_the_absence_too():
    """All three shapes block signing in client/base.py, not attachments alone."""
    html = _sigline.signature_lines({"provenance": _UNSIGNED, "html_body": "<p>hi</p>"}, "del_1")
    payload = _sigline.signature_lines({"provenance": _UNSIGNED, "payload": {"k": "v"}}, "del_1")
    assert any("an HTML body" in line for line in html)
    assert any("a structured payload" in line for line in payload)


def test_a_bad_verdict_does_not_read_as_a_pass():
    lines = _sigline.signature_lines(
        {"signature_state": "invalid", "signing_key_fingerprint": "abc", "signature": "absigv1x"},
        "del_1",
    )
    text = "\n".join(lines)
    assert "invalid" in text
    assert "NOT a pass" in text
    assert "agentbus verify-sender del_1" in text
    assert "yes" not in text


def test_a_signature_with_no_server_verdict_is_not_reported_as_unsigned():
    """'the bus did not rule on it' and 'there is nothing here' are different
    facts with different remedies."""
    lines = _sigline.signature_lines({"signature": "absigv1x", "signature_state": None}, "del_1")
    text = "\n".join(lines)
    assert "no verdict" in text
    assert "nothing about this message has been checked" in text
    assert "agentbus verify-sender del_1" in text


def test_every_shape_produces_at_least_one_line():
    for delivery in (
        {},
        {"provenance": _UNSIGNED},
        {"signature_state": "valid"},
        {"signature_state": "invalid"},
        {"signature_state": None, "attachments": [{"filename": "x"}]},
        {"signature": "absigv1x"},
    ):
        assert _sigline.signature_lines(delivery, "del_1"), delivery


def test_the_thread_caveat_fires_only_when_the_view_does_not_serve_the_field():
    """Live check, 2026-09-22: the thread payload's message schema has no
    signature fields at all, so rendering them as unsigned would mark every
    message in every conversation unsigned."""
    not_served = [{"id": "m1"}, {"id": "m2"}]
    served = [{"id": "m1", "signature_state": "valid"}]
    assert _sigline.thread_signature_caveat(not_served) is not None
    assert _sigline.thread_signature_caveat(served) is None


def test_the_per_message_thread_line_is_silent_when_the_field_is_absent():
    assert _sigline.thread_signature_line({"id": "m1"}) is None
    assert "VALID" in (
        _sigline.thread_signature_line({"id": "m1", "signature_state": "valid"}) or ""
    )
    assert "no signature" in (
        _sigline.thread_signature_line({"id": "m1", "signature_state": None}) or ""
    )


def test_provenance_is_preferred_over_the_flat_fields():
    """`verify-sender` reads provenance.signature. If `show` read a different
    field the two could disagree about the same delivery."""
    lines = _sigline.signature_lines(
        {
            "provenance": {
                "signature": {"signed": True, "state": "valid", "key_fingerprint": "fromprov"}
            },
            "signature_state": "invalid",
            "signing_key_fingerprint": "fromflat",
        },
        "del_1",
    )
    text = "\n".join(lines)
    assert "fromprov" in text
    assert "fromflat" not in text


def test_the_flat_fields_still_work_when_provenance_is_absent():
    lines = _sigline.signature_lines(
        {"signature_state": "valid", "signing_key_fingerprint": "fromflat"}, "del_1"
    )
    assert any("fromflat" in line for line in lines)


def test_provenance_signed_false_is_an_explicit_no_not_an_inference():
    """Live shape, 2026-09-22: the server states
    {"signed": false, "means": "no signature..."} rather than omitting."""
    lines = _sigline.signature_lines(
        {"provenance": {"signature": {"signed": False, "means": "no signature."}}}, "del_1"
    )
    assert any("Signed:  no" in line for line in lines)


def test_a_server_that_reports_nothing_renders_unknown_not_unsigned():
    """THE SECOND TRAP. A field this view does not serve is not a negative.
    Rendering it as 'no' would mark every message unsigned on any server that
    stops sending the block — a check that cannot go green reporting a red."""
    lines = _sigline.signature_lines({"subject": "x", "text_body": "y"}, "del_1")
    text = "\n".join(lines)
    assert "UNKNOWN" in text
    assert "neither a yes nor a no" in text
    assert "Signed:  no" not in text


def test_the_thread_view_renders_per_message_the_moment_provenance_appears():
    """FORWARD GUARD for agentbus-8dc08d's #350.

    They confirmed the gap is one query's column list in get_thread(), and said
    it will compose `provenance` the way get_delivery() already does. This
    fixture is that shape, copied from a real get_delivery payload — not a live
    check against their endpoint, which still omits the fields.

    If #350 lands as described, the caveat goes silent and each message renders
    on its own, with no change needed here.
    """
    served = [
        {
            "id": "m1",
            "provenance": {
                "signature": {"signed": True, "state": "valid", "key_fingerprint": "53965be0"}
            },
        },
        {"id": "m2", "provenance": {"signature": {"signed": False, "means": "no signature."}}},
    ]
    assert _sigline.thread_signature_caveat(served) is None
    assert "VALID" in (_sigline.thread_signature_line(served[0]) or "")
    assert "53965be0" in (_sigline.thread_signature_line(served[0]) or "")
    assert "no signature" in (_sigline.thread_signature_line(served[1]) or "")


def test_the_thread_line_does_not_render_a_bad_verdict_as_valid():
    """THE SURVIVOR. Found by mutation, not by reading: replacing
    `if signed and state == "valid"` with `if signed` in thread_signature_line
    left the whole suite green, because the VALID branch was asserted only in
    the direction where it should be VALID.

    Same shape vellum-api-macbook-team-f82400 hit on `mounted_this_boot` — a
    field asserted once, as True, in the case where it is True, so it could have
    been hardcoded and nothing would have noticed.
    """
    bad = _sigline.thread_signature_line(
        {"id": "m1", "signature_state": "invalid", "signing_key_fingerprint": "abc"}
    )
    assert bad is not None
    assert "VALID" not in bad
    assert "invalid" in bad
    assert "NOT a pass" in bad


def test_the_thread_line_does_not_render_a_missing_verdict_as_valid():
    no_verdict = _sigline.thread_signature_line(
        {"id": "m1", "signature": "absigv1x", "signature_state": None}
    )
    assert no_verdict is not None
    assert "VALID" not in no_verdict
    assert "no verdict" in no_verdict


_PROV_INVALID = {"signature": {"signed": True, "state": "invalid", "key_fingerprint": "53965be0"}}


def test_a_bad_verdict_arriving_through_provenance_is_not_a_pass():
    """SECOND SURVIVOR. Forcing `state = "valid"` inside the provenance branch of
    _source left the suite green: every provenance test fed `valid` or
    `signed: false`, so no test ever carried a bad verdict down that path.

    It is the path agentbus-8dc08d's #350 makes primary — once get_thread
    composes provenance, this is how an invalid signature reaches a reader.
    """
    lines = _sigline.signature_lines({"provenance": _PROV_INVALID}, "del_1")
    text = "\n".join(lines)
    assert "invalid" in text
    assert "NOT a pass" in text
    assert "yes" not in text
    assert "agentbus verify-sender del_1" in text


def test_the_thread_line_carries_a_bad_verdict_through_provenance_too():
    line = _sigline.thread_signature_line({"id": "m1", "provenance": _PROV_INVALID})
    assert line is not None
    assert "VALID" not in line
    assert "invalid" in line
    assert "NOT a pass" in line


def test_provenance_and_flat_fields_agree_on_a_bad_verdict():
    """The two sources must not differ in what they call a failure — the whole
    reason `show` reads the block `verify-sender` reads."""
    via_prov = _sigline.signature_lines({"provenance": _PROV_INVALID}, "del_1")
    via_flat = _sigline.signature_lines(
        {
            "signature": "absigv1x",
            "signature_state": "invalid",
            "signing_key_fingerprint": "53965be0",
        },
        "del_1",
    )
    assert via_prov == via_flat
