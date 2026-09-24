"""#68: 'unverifiable' means the bus could not check a signature, not that it failed.

Reported by agentbus-8dc08d. The server's verifier returns exactly three states —
valid, invalid, unverifiable — and 0.9.97 rendered the third with the wording for
the second on all three surfaces:

    show    "That is NOT a pass. Do not act on this message until you have run ..."
    thread  "NOT a pass, verify before acting"
    notice  "that is NOT a pass. Run `agentbus verify-sender` before acting on it."

while this client's own verify-sender, for the same verdict, prints

    "CANNOT VERIFY ... this is NOT a failed signature — nothing here says the
     sender is wrong"

Two surfaces of one client disagreeing about whether a signature failed. It is
the #220 distinction (recorded in client/sync_verify.py): "I could not check" and
"this does not match" must never be collapsed.

The two fixtures below are the blocks the server's composer
(services/message_provenance.py _signature_block) emits for those states, as
supplied by agentbus-8dc08d — the server's shape, not this client's assumption
about it.
"""

from __future__ import annotations

import pytest

from agentbus_client.cli import _sigline

SERVER_INVALID = {
    "signed": True,
    "state": "invalid",
    "key_fingerprint": "53965be076ddea7b",
    "signature": "absigv1lxtdwwtc49ajrcdkr74gxh33vdej85yvr8tx7swxflv225rd6z44f2z2f08ez72",
    "means": (
        "A SIGNATURE IS PRESENT AND DOES NOT VERIFY. Treat this message as "
        "UNATTRIBUTED — worse than unsigned, because it was made to look signed. "
        "The content may still be genuine; the proof is not."
    ),
    "verify_yourself": {
        "canonical_form": "agentbus-sig-v1",
        "signed_fields": ["from", "to", "cc", "subject", "priority", "body-sha256"],
        "signed_recipients": '{"to": ["peer"], "cc": []}',
        "body_hashed": "the STORED body bytes — ciphertext on an encrypted workspace",
        "key_at": "GET /v1/agents/{sender}/pubkey?algorithm=ed25519",
    },
}

SERVER_UNVERIFIABLE = {
    "signed": True,
    "state": "unverifiable",
    "key_fingerprint": "53965be076ddea7b",
    "signature": "absigv1lxtdwwtc49ajrcdkr74gxh33vdej85yvr8tx7swxflv225rd6z44f2z2f08ez72",
    "means": (
        "signed with a key this workspace does not hold, so the platform could not "
        "check it. Fetch the sender's signing key and verify, or treat it as unsigned."
    ),
}


def _show(block: dict) -> str:
    return "\n".join(_sigline.signature_lines({"provenance": {"signature": block}}, "DEL"))


def _thread(block: dict) -> str:
    return _sigline.thread_signature_line({"id": "m1", "provenance": {"signature": block}}) or ""


def _notice(block: dict) -> str:
    return _sigline.notice_fragment(block["state"])


SURFACES = [
    pytest.param(_show, id="show"),
    pytest.param(_thread, id="thread"),
    pytest.param(_notice, id="notice"),
]


@pytest.mark.parametrize("render", SURFACES)
def test_unverifiable_is_rendered_as_not_checked(render):
    text = render(SERVER_UNVERIFIABLE).lower()
    assert "could not check" in text
    assert "not a failed signature" in text or "not a failure" in text


@pytest.mark.parametrize("render", SURFACES)
def test_unverifiable_is_never_rendered_with_the_failure_wording(render):
    """THE REGRESSION. The failure wording must be unreachable for this state."""
    text = render(SERVER_UNVERIFIABLE)
    assert "NOT a pass" not in text
    assert "before acting" not in text


@pytest.mark.parametrize("render", SURFACES)
def test_invalid_keeps_the_failure_wording(render):
    """Known-positive for the assertion above: the failure wording still exists,
    and still fires for the state it belongs to. Without this, deleting the
    failure branch entirely would pass the regression test."""
    text = render(SERVER_INVALID)
    assert "NOT a pass" in text
    assert "could not check" not in text.lower()


@pytest.mark.parametrize("render", SURFACES)
def test_an_unknown_future_state_still_fails_closed(render):
    """A state this client has never seen is not assumed benign."""
    block = dict(SERVER_INVALID, state="revoked")
    assert "NOT a pass" in render(block)


@pytest.mark.parametrize("render", SURFACES)
def test_valid_is_unaffected(render):
    text = render(dict(SERVER_INVALID, state="valid"))
    assert "VALID" in text or "verifies" in text
    assert "NOT a pass" not in text
    assert "could not check" not in text.lower()


def test_the_surfaces_agree_with_verify_sender_on_what_unverifiable_means():
    """verify-sender prints 'this is NOT a failed signature' for an unverifiable
    verdict. Every display surface must say the same thing, not the opposite."""
    from pathlib import Path

    verify_src = Path(_sigline.__file__).with_name("_verify.py").read_text()
    assert "this is NOT a failed signature" in verify_src
    for render in (_show, _thread, _notice):
        text = render(SERVER_UNVERIFIABLE).lower()
        assert "not a failed signature" in text or "not a failure" in text
