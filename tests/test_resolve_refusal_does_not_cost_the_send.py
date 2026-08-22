"""A refused `recipients/resolve` must not swallow the send.

Client 0.5.0 calls resolve on EVERY send — that is how it learns whether the
workspace is encrypted. When the server refuses that call, the pre-fix client
raised and the message was never posted at all.

The server-side fix (send scope may reach resolve) is the real one and is
pinned in test_send_scope_can_reach_the_seal_path.py. This is the other half:
an OLD server, or one whose allowlist has not caught up, must still be able to
carry a send. Falling through is not a bypass — `accept_message` refuses an
unsealed body on an encrypted workspace, so the surface that owns the guarantee
still enforces it.

Both directions in one file, deliberately: the fallback must fire on a refusal
AND sealing must still happen when resolve answers normally. A fallback tested
only for firing is how sealing gets quietly disabled for everyone.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentbus_client import sealing
from agentbus_client.client import AgentBus, AgentBusError


def _bus() -> AgentBus:
    return AgentBus(base_url="https://example.invalid", agent="tester", api_key="k")


PAYLOAD: dict[str, Any] = {"to": ["peer"], "text": "the plaintext", "attachments": []}


@pytest.mark.parametrize("status", [403, 404, 405])
def test_a_refused_resolve_leaves_the_payload_alone_instead_of_raising(status: int) -> None:
    bus = _bus()

    def _refuse(*_a: Any, **_k: Any) -> Any:
        raise AgentBusError("nope", code="permission_denied", status=status)

    bus._request = _refuse
    # #220: the sealer hands back the resolver's answer too, so a reply can
    # sign over what the SERVER will store. A refusal means no answer at all.
    out, resolved = bus._seal_if_needed(dict(PAYLOAD), None)
    assert out["text"] == "the plaintext"
    assert not out.get("sealed")
    assert resolved is None, (
        "a refused resolve must report NO answer; signing over a half-answer "
        "would produce a signature the server reads as invalid"
    )


def test_an_unexpected_failure_still_propagates() -> None:
    """The negative. If every error fell through, a 500 mid-seal would post the
    plaintext to a workspace that may well be encrypted, and the fallback above
    would be indistinguishable from no sealing at all."""
    bus = _bus()

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AgentBusError("server exploded", status=500)

    bus._request = _boom
    with pytest.raises(AgentBusError):
        bus._seal_if_needed(dict(PAYLOAD), None)


def test_sealing_still_happens_when_resolve_answers(tmp_path: Path, monkeypatch) -> None:
    """The known-positive that makes the two tests above mean anything: with a
    working resolve the body must come back UNREADABLE, and readable again only
    with the recipient's private key."""
    monkeypatch.setattr(sealing, "config_dir", lambda: tmp_path)
    recipient_private, recipient_public = sealing.generate_keypair()

    bus = _bus()

    def _resolve(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {
            "encrypted": True,
            "keys": {"peer": [{"public_key": recipient_public, "fingerprint": "x"}]},
            "missing_keys": [],
            "external": [],
        }

    bus._request = _resolve
    out, resolved = bus._seal_if_needed(dict(PAYLOAD), None)

    assert resolved is not None, "a successful resolve must hand its answer back"
    assert out["sealed"] is True
    assert "the plaintext" not in out["text"]
    assert sealing.unseal_body(out["text"], recipient_private) == "the plaintext"
