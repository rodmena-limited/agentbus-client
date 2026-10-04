from __future__ import annotations

import hashlib
import json

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from agentbus_client import _signing, sealing
from agentbus_client._agefmt import bech32_decode
from agentbus_client.client import AgentBus

AGENT = "alice-1a2b3c"
PRIVATE = "ABSIGSEC1N4SMR800L4DXPW5YFT6F9MPVC3ZYN3TF0VEXJXTS8WKQX89W0ASQ0XV08F"
PUBLIC = "absig16adfsqvzky9t042tlmfujeq88g8wzuhnm2nzxfd0qgdx3ac82ydqm7nhdg"
FINGERPRINT = "21fe31dfa154a261"
BODY = "ledger balanced\n"
BODY_SHA = "d64a09a1f258e68accb6d2e75cce753bd82efe4bb6452b364337c84b6ec02d17"
CANONICAL = (
    b"agentbus-sig-v1\n"
    b"from: alice-1a2b3c\n"
    b"to: bob,zed\n"
    b"cc: al,carol\n"
    b"subject: Quarterly close\n"
    b"priority: urgent\n"
    b"body-sha256: " + BODY_SHA.encode() + b"\n"
)
CANONICAL_SIG = (
    "absigv1a52wlv256zp5plz6mfqwxeks004yvnc7hy67u20u0slnxkqs860slvflpfv5yjdc3r"
    "xvj8cduaav4lrv0quph5u2l0s9wv0qkjxukpskufzzr"
)
FOREIGN_SIG = (
    "absigv1ea3ldysch3h6ug4gtjyhxd93hd3vu8xhy9udzknsj36j8t7nn453t3lj3azex6kxe4lt"
    "gk3d3wa0yzkukymqd22etk0j7rlwg3ylgpqf5vkwr"
)


class _Server:
    def __init__(self, resolve=None, resolve_reply=None, delivery=None, keys=None):
        self.resolve = resolve
        self.resolve_reply = resolve_reply
        self.delivery = delivery
        self.keys = keys
        self.posted: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else None
        if path == "/v1/recipients/resolve":
            if self.resolve is None:
                return httpx.Response(404, json={"detail": "no such route"})
            return httpx.Response(200, json=self.resolve)
        if path == "/v1/recipients/resolve-reply":
            if self.resolve_reply is None:
                return httpx.Response(404, json={"detail": "no such route"})
            return httpx.Response(200, json=self.resolve_reply)
        if request.method == "GET" and path.startswith("/v1/deliveries/"):
            if self.delivery is None:
                return httpx.Response(404, json={"detail": "not found"})
            return httpx.Response(200, json=self.delivery)
        if request.method == "GET" and path == f"/v1/agents/{AGENT}/pubkey":
            assert request.url.params.get("algorithm") == "ed25519"
            return httpx.Response(200, json={"keys": self.keys or []})
        if request.method == "POST" and path.startswith("/v1/messages"):
            self.posted.append((path, body))
            return httpx.Response(200, json={"message_id": "m-1", "deliveries": []})
        return httpx.Response(599, json={"detail": f"unexpected {request.method} {path}"})


def _bus(server: _Server) -> AgentBus:
    bus = AgentBus(api_key="ab_sk_test_test", agent=AGENT, base_url="https://bus.test")
    bus._client.close()
    bus._client = httpx.Client(base_url=bus.base_url, transport=httpx.MockTransport(server))
    return bus


def _install_pinned_key() -> None:
    path = sealing.signing_key_path(AGENT)
    sealing.create_secret_exclusive(path, PRIVATE + "\n")


def _send(bus: AgentBus) -> None:
    bus.send(
        to=["zed", "bob"],
        cc=["carol", "al"],
        subject="Quarterly close",
        priority="urgent",
        text=BODY,
    )


def test_send_with_pinned_key_carries_the_known_answer_signature():
    _install_pinned_key()
    server = _Server()
    _send(_bus(server))
    assert len(server.posted) == 1
    path, body = server.posted[0]
    assert path == "/v1/messages"
    assert body["signature"] == CANONICAL_SIG
    assert body["signing_key_fingerprint"] == FINGERPRINT
    assert body["text"] == BODY


def test_send_with_generated_key_verifies_under_independent_ed25519():
    private, public = sealing.ensure_signing_keypair(AGENT)
    server = _Server()
    _send(_bus(server))
    _path, body = server.posted[0]
    raw_public = bech32_decode(public)
    Ed25519PublicKey.from_public_bytes(raw_public).verify(
        bech32_decode(body["signature"]), CANONICAL
    )
    assert body["signing_key_fingerprint"] == hashlib.sha256(raw_public).hexdigest()[:16]
    assert len(body["signing_key_fingerprint"]) == 16
    assert private.startswith("ABSIGSEC1")


def test_send_without_a_key_is_unsigned():
    server = _Server()
    _send(_bus(server))
    _path, body = server.posted[0]
    assert "signature" not in body
    assert "signing_key_fingerprint" not in body


def test_send_signs_over_the_resolver_answer():
    _install_pinned_key()
    server = _Server(
        resolve={
            "encrypted": False,
            "to": ["bob", "zed"],
            "cc": ["al", "carol"],
            "subject": "Quarterly close",
        }
    )
    bus = _bus(server)
    bus.send(to=["room:ops"], subject="ignored", priority="urgent", text=BODY)
    _path, body = server.posted[0]
    assert body["signature"] == CANONICAL_SIG


def test_reply_signs_over_server_derived_recipients_and_subject():
    _install_pinned_key()
    server = _Server(
        resolve_reply={
            "encrypted": False,
            "to": ["zed", "bob"],
            "cc": ["carol", "al"],
            "subject": "Quarterly close",
        }
    )
    _bus(server).reply("m-parent", BODY, priority="urgent")
    path, body = server.posted[-1]
    assert path == "/v1/messages/m-parent/reply"
    assert body["signature"] == CANONICAL_SIG
    assert body["signing_key_fingerprint"] == FINGERPRINT


def _delivery(*, subject="Quarterly close", body_sha=BODY_SHA, signature=CANONICAL_SIG):
    return {
        "delivery_id": "d-1",
        "message_id": "m-1",
        "sender_agent_name": AGENT,
        "subject": subject,
        "priority": "urgent",
        "text_body": BODY,
        "body_sha256": body_sha,
        "provenance": {
            "signature": {
                "signed": True,
                "state": "valid",
                "key_fingerprint": FINGERPRINT,
                "signature": signature,
                "verify_yourself": {
                    "signed_recipients": {"to": ["zed", "bob"], "cc": ["carol", "al"]}
                },
            }
        },
    }


def _verify(delivery) -> dict:
    keys = [{"fingerprint": FINGERPRINT, "public_key": PUBLIC}]
    return _bus(_Server(delivery=delivery, keys=keys)).verify("d-1")


def test_verify_sender_reports_valid_for_genuine_signature():
    result = _verify(_delivery())
    assert result["verified"] is True
    assert result["verdict"] == "valid"
    assert result["signed_by"] == AGENT
    assert result["key_fingerprint"] == FINGERPRINT
    assert result["platform_said"] == "valid"


@pytest.mark.parametrize(
    "overrides",
    [
        {"subject": "Quarterly close (edited)"},
        {"body_sha": hashlib.sha256(b"ledger unbalanced\n").hexdigest()},
        {"signature": FOREIGN_SIG},
    ],
)
def test_verify_sender_reports_invalid_for_tampering(overrides):
    result = _verify(_delivery(**overrides))
    assert result["verified"] is False
    assert result["verdict"] == "invalid"
    assert result["reason"] == "signature does not verify: signature does not verify"
    assert result["platform_said"] == "valid"


def test_verify_sender_unverifiable_when_fingerprint_unpublished():
    delivery = _delivery()
    server = _Server(delivery=delivery, keys=[{"fingerprint": "0" * 16, "public_key": PUBLIC}])
    result = _bus(server).verify("d-1")
    assert result["verified"] is False
    assert result["verdict"] == "unverifiable"
    assert result["reason"] == "signing key not published for this fingerprint"


def test_send_then_verify_round_trip_through_the_wire():
    _private, public = sealing.ensure_signing_keypair(AGENT)
    sender = _Server()
    _send(_bus(sender))
    _path, sent = sender.posted[0]
    delivery = _delivery(signature=sent["signature"])
    delivery["provenance"]["signature"]["key_fingerprint"] = sent["signing_key_fingerprint"]
    keys = [{"fingerprint": sent["signing_key_fingerprint"], "public_key": public}]
    result = _bus(_Server(delivery=delivery, keys=keys)).verify("d-1")
    assert result["verdict"] == "valid"


def test_read_opens_an_armored_sealed_body():
    _private, public = sealing.ensure_keypair(AGENT)
    delivery = _delivery()
    delivery["text_body"] = sealing.seal_for("the plaintext", [public])
    message = _bus(_Server(delivery=delivery)).read("d-1")
    assert message["sealed_opened"] is True
    assert message["text_body"] == "the plaintext"


def test_read_leaves_a_plain_body_alone():
    sealing.ensure_keypair(AGENT)
    delivery = _delivery()
    message = _bus(_Server(delivery=delivery)).read("d-1")
    assert message["text_body"] == BODY
    assert "sealed_opened" not in message
    assert "sealed_unreadable" not in message


def test_foreign_signature_is_genuine_for_its_own_key():
    foreign = bytes.fromhex("3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c")
    Ed25519PublicKey.from_public_bytes(foreign).verify(bech32_decode(FOREIGN_SIG), CANONICAL)
    with pytest.raises(_signing.BadSignature):
        _signing.verify(PUBLIC, CANONICAL, FOREIGN_SIG)
