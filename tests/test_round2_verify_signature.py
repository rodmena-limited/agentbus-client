from __future__ import annotations

import copy
import hashlib
import json

import httpx
import pytest

from agentbus_client import _signing, sealing
from agentbus_client.client import AgentBus

BASE = "https://bus.round2.test"
SENDER = "alice-sender-1"
READER = "reader-a-1"
TEXT = "the build on main is green at 3e1c6ac"
VALID_MEANS = "checked on THIS machine against the key you fetched"


class FakeBus:
    def __init__(self) -> None:
        self.deliveries: dict[str, object] = {}
        self.keys: dict[str, list[dict[str, str]]] = {}
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if request.method == "GET" and path.startswith("/v1/deliveries/"):
            delivery_id = path.rsplit("/", 1)[1]
            if delivery_id not in self.deliveries:
                return httpx.Response(404, json={"code": "not_found", "detail": "no such"})
            return httpx.Response(200, json=self.deliveries[delivery_id])
        if request.method == "GET" and path.startswith("/v1/agents/") and path.endswith("/pubkey"):
            name = path[len("/v1/agents/") : -len("/pubkey")]
            if request.url.params.get("algorithm") != "ed25519":
                return httpx.Response(200, json={"keys": []})
            return httpx.Response(200, json={"keys": self.keys.get(name, [])})
        return httpx.Response(404, json={"code": "not_found", "detail": path})


def _client(fake: FakeBus, agent: str | None = READER) -> AgentBus:
    bus = AgentBus(api_key="ab_live_round2", base_url=BASE, agent=agent)
    bus._client.close()
    bus._client = httpx.Client(transport=httpx.MockTransport(fake.handler), base_url=BASE)
    return bus


def _sender_signs(to, cc, subject, priority, text=TEXT, sender=SENDER):
    _private, public = sealing.ensure_signing_keypair(sender)
    origin = AgentBus(api_key="ab_live_sender", base_url=BASE, agent=sender)
    payload = {"to": to, "cc": cc, "subject": subject, "text": text}
    if priority is not None:
        payload["priority"] = priority
    signed = origin._sign_if_possible(payload, None)
    origin.close()
    assert signed["signature"].startswith("absigv1")
    assert signed["signing_key_fingerprint"] == _signing.fingerprint(public)
    return signed, public


def _delivery(signed, *, state="valid", priority="urgent", recipients=None):
    message = {
        "id": "dlv-1",
        "sender_agent_name": SENDER,
        "sender_display": f"{SENDER} via AgentBus",
        "subject": signed["subject"],
        "text_body": signed["text"],
        "body_sha256": hashlib.sha256(signed["text"].encode()).hexdigest(),
        "recipients": recipients or list(signed["to"]) + list(signed["cc"]),
        "provenance": {
            "signature": {
                "signed": True,
                "state": state,
                "key_fingerprint": signed["signing_key_fingerprint"],
                "signature": signed["signature"],
                "verify_yourself": {
                    "signed_recipients": {"to": list(signed["to"]), "cc": list(signed["cc"])}
                },
            }
        },
    }
    if priority is not None:
        message["priority"] = priority
    return message


@pytest.fixture
def world():
    fake = FakeBus()
    signed, public = _sender_signs(
        ["bob-2", "carol-3"], ["dave-4", "erin-5"], "deploy status", "urgent"
    )
    decoy_public = _signing.generate_keypair()[1]
    fake.keys[SENDER] = [
        {"fingerprint": _signing.fingerprint(decoy_public), "public_key": decoy_public},
        {"fingerprint": signed["signing_key_fingerprint"], "public_key": public},
    ]
    fake.deliveries["dlv-1"] = _delivery(signed)
    return fake, signed


def test_a_genuine_signature_verifies_with_the_exact_result_contract(world) -> None:
    fake, signed = world
    result = _client(fake).verify("dlv-1")
    assert result == {
        "verified": True,
        "verdict": "valid",
        "signed_by": SENDER,
        "key_fingerprint": signed["signing_key_fingerprint"],
        "platform_said": "valid",
        "means": VALID_MEANS,
    }


def test_verify_requests_the_delivery_then_the_ed25519_key_as_the_reader(world) -> None:
    fake, _ = world
    _client(fake).verify("dlv-1")
    assert [(r.method, r.url.path) for r in fake.requests] == [
        ("GET", "/v1/deliveries/dlv-1"),
        ("GET", f"/v1/agents/{SENDER}/pubkey"),
    ]
    assert dict(fake.requests[1].url.params) == {"algorithm": "ed25519"}
    for request in fake.requests:
        assert request.headers["X-AgentBus-Agent"] == READER
        assert request.headers["Authorization"] == "Bearer ab_live_round2"


def test_an_explicit_agent_is_the_acting_identity_on_both_requests(world) -> None:
    fake, _ = world
    result = _client(fake).verify("dlv-1", agent="other-agent-9")
    assert result["verdict"] == "valid"
    assert [r.headers["X-AgentBus-Agent"] for r in fake.requests] == [
        "other-agent-9",
        "other-agent-9",
    ]


def test_the_local_verdict_is_reported_beside_a_disagreeing_platform(world) -> None:
    fake, signed = world
    fake.deliveries["dlv-1"] = _delivery(signed, state="platform-said-invalid")
    result = _client(fake).verify("dlv-1")
    assert result["verified"] is True
    assert result["verdict"] == "valid"
    assert result["platform_said"] == "platform-said-invalid"


def _tamper_subject(m):
    m["subject"] = "deploy status (edited)"


def _tamper_priority(m):
    m["priority"] = "background"


def _tamper_body_hash(m):
    m["body_sha256"] = hashlib.sha256(b"a different body").hexdigest()


def _tamper_to_added(m):
    m["provenance"]["signature"]["verify_yourself"]["signed_recipients"]["to"].append("mallory")


def _tamper_to_removed(m):
    m["provenance"]["signature"]["verify_yourself"]["signed_recipients"]["to"] = ["bob-2"]


def _tamper_cc_dropped(m):
    m["provenance"]["signature"]["verify_yourself"]["signed_recipients"]["cc"] = []


def _tamper_cc_moved_to_to(m):
    m["provenance"]["signature"]["verify_yourself"]["signed_recipients"] = {
        "to": ["bob-2", "carol-3", "dave-4", "erin-5"],
        "cc": [],
    }


def _tamper_signature_bytes(m):
    block = m["provenance"]["signature"]
    other = _signing.sign(_signing.generate_keypair()[0], b"something else entirely")
    block["signature"] = other


@pytest.mark.parametrize(
    "tamper",
    [
        _tamper_subject,
        _tamper_priority,
        _tamper_body_hash,
        _tamper_to_added,
        _tamper_to_removed,
        _tamper_cc_dropped,
        _tamper_cc_moved_to_to,
        _tamper_signature_bytes,
    ],
)
def test_tampering_with_any_signed_field_is_reported_invalid(world, tamper) -> None:
    fake, _ = world
    tamper(fake.deliveries["dlv-1"])
    result = _client(fake).verify("dlv-1")
    assert set(result) == {"verified", "verdict", "reason", "platform_said"}
    assert result["verified"] is False
    assert result["verdict"] == "invalid"
    assert result["reason"].startswith("signature does not verify: ")
    assert result["platform_said"] == "valid"


def test_a_signature_by_another_key_claiming_the_senders_fingerprint_is_invalid(world) -> None:
    fake, _ = world
    attacker_private, _ = _signing.generate_keypair()
    message = fake.deliveries["dlv-1"]
    forged = _signing.sign(
        attacker_private,
        _signing.canonical_bytes(
            sender=SENDER,
            to=["bob-2", "carol-3"],
            cc=["dave-4", "erin-5"],
            subject="deploy status",
            priority="urgent",
            body_sha256=message["body_sha256"],
        ),
    )
    message["provenance"]["signature"]["signature"] = forged
    result = _client(fake).verify("dlv-1")
    assert result["verified"] is False
    assert result["verdict"] == "invalid"


def test_recipient_order_is_not_tampering(world) -> None:
    fake, _ = world
    rcpts = fake.deliveries["dlv-1"]["provenance"]["signature"]["verify_yourself"]
    rcpts["signed_recipients"] = {"to": ["carol-3", "bob-2"], "cc": ["erin-5", "dave-4"]}
    assert _client(fake).verify("dlv-1")["verdict"] == "valid"


def test_an_unpublished_fingerprint_is_unverifiable_never_invalid(world) -> None:
    fake, signed = world
    fake.keys[SENDER] = [
        k for k in fake.keys[SENDER] if k["fingerprint"] != signed["signing_key_fingerprint"]
    ]
    result = _client(fake).verify("dlv-1")
    assert result == {
        "verified": False,
        "verdict": "unverifiable",
        "reason": "signing key not published for this fingerprint",
        "platform_said": "valid",
    }


def test_no_published_keys_at_all_is_unverifiable(world) -> None:
    fake, _ = world
    fake.keys[SENDER] = []
    result = _client(fake).verify("dlv-1")
    assert result["verified"] is False
    assert result["verdict"] == "unverifiable"


def test_a_block_without_a_fingerprint_does_not_match_a_published_key(world) -> None:
    fake, _ = world
    del fake.deliveries["dlv-1"]["provenance"]["signature"]["key_fingerprint"]
    result = _client(fake).verify("dlv-1")
    assert result["verified"] is False
    assert result["verdict"] == "unverifiable"


@pytest.mark.parametrize(
    "provenance",
    [None, {}, {"signature": None}, {"signature": {"signed": False, "state": "unsigned"}}],
)
def test_an_unsigned_message_is_unsigned_and_no_key_is_fetched(world, provenance) -> None:
    fake, _ = world
    fake.deliveries["dlv-1"]["provenance"] = provenance
    result = _client(fake).verify("dlv-1")
    assert result == {
        "verified": False,
        "verdict": "unsigned",
        "reason": "unsigned",
        "platform_said": None,
    }
    assert [r.url.path for r in fake.requests] == ["/v1/deliveries/dlv-1"]


@pytest.mark.parametrize("missing", ["absent", None, ""])
def test_a_copy_without_a_body_hash_is_unverifiable_not_invalid(world, missing) -> None:
    fake, _ = world
    message = fake.deliveries["dlv-1"]
    if missing == "absent":
        del message["body_sha256"]
    else:
        message["body_sha256"] = missing
    result = _client(fake).verify("dlv-1")
    assert set(result) == {"verified", "verdict", "reason", "platform_said"}
    assert result["verified"] is False
    assert result["verdict"] == "unverifiable"
    assert result["platform_said"] == "valid"
    assert "body_sha256" in result["reason"]
    assert "NOT a failed signature" in result["reason"]


def test_the_display_name_fallback_strips_the_via_suffix(world) -> None:
    fake, _ = world
    del fake.deliveries["dlv-1"]["sender_agent_name"]
    result = _client(fake).verify("dlv-1")
    assert result["verdict"] == "valid"
    assert result["signed_by"] == SENDER
    assert fake.requests[1].url.path == f"/v1/agents/{SENDER}/pubkey"


def test_the_structured_sender_name_wins_over_the_display_string(world) -> None:
    fake, _ = world
    fake.deliveries["dlv-1"]["sender_display"] = "Somebody Else via AgentBus (encrypted)"
    result = _client(fake).verify("dlv-1")
    assert result["verdict"] == "valid"
    assert result["signed_by"] == SENDER


def test_a_claimed_sender_other_than_the_signer_is_not_valid(world) -> None:
    fake, _ = world
    fake.keys["mallory-6"] = list(fake.keys[SENDER])
    fake.deliveries["dlv-1"]["sender_agent_name"] = "mallory-6"
    result = _client(fake).verify("dlv-1")
    assert result["verified"] is False
    assert result["verdict"] == "invalid"
    assert fake.requests[1].url.path == "/v1/agents/mallory-6/pubkey"


def test_signed_recipients_published_as_a_json_string_verify(world) -> None:
    fake, _ = world
    block = fake.deliveries["dlv-1"]["provenance"]["signature"]
    block["verify_yourself"]["signed_recipients"] = json.dumps(
        block["verify_yourself"]["signed_recipients"]
    )
    assert _client(fake).verify("dlv-1")["verdict"] == "valid"


def test_a_room_send_verifies_against_the_as_typed_address_not_its_members() -> None:
    fake = FakeBus()
    signed, public = _sender_signs(["room:ops"], [], "ops notice", None)
    fake.keys[SENDER] = [{"fingerprint": signed["signing_key_fingerprint"], "public_key": public}]
    good = _delivery(signed, priority=None, recipients=["bob-2", "carol-3"])
    fake.deliveries["dlv-1"] = good
    tampered = copy.deepcopy(good)
    tampered["provenance"]["signature"]["verify_yourself"]["signed_recipients"] = {
        "to": ["bob-2", "carol-3"],
        "cc": [],
    }
    fake.deliveries["dlv-2"] = tampered
    bus = _client(fake)
    assert bus.verify("dlv-1")["verdict"] == "valid"
    assert bus.verify("dlv-2")["verdict"] == "invalid"


def test_absent_priority_is_verified_as_normal() -> None:
    fake = FakeBus()
    signed, public = _sender_signs(["bob-2"], ["dave-4"], "no priority given", None)
    fake.keys[SENDER] = [{"fingerprint": signed["signing_key_fingerprint"], "public_key": public}]
    fake.deliveries["dlv-1"] = _delivery(signed, priority=None)
    signed_urgent, _ = _sender_signs(["bob-2"], ["dave-4"], "no priority given", "urgent")
    fake.deliveries["dlv-2"] = _delivery(signed_urgent, priority=None)
    bus = _client(fake)
    assert bus.verify("dlv-1")["verdict"] == "valid"
    assert bus.verify("dlv-2")["verdict"] == "invalid"


def test_missing_signed_recipients_cannot_verify_a_message_signed_to_people(world) -> None:
    fake, _ = world
    del fake.deliveries["dlv-1"]["provenance"]["signature"]["verify_yourself"]
    result = _client(fake).verify("dlv-1")
    assert result["verified"] is False
    assert result["verdict"] == "invalid"


def test_an_unknown_delivery_raises_rather_than_returning_a_verdict(world) -> None:
    fake, _ = world
    from agentbus_client.client import AgentBusError

    with pytest.raises(AgentBusError):
        _client(fake).verify("dlv-404")


@pytest.mark.parametrize("bad", [None, 7, "", "absig1"])
def test_a_signed_delivery_with_a_missing_or_broken_signature_is_invalid(world, bad) -> None:
    fake, _ = world
    block = fake.deliveries["dlv-1"]["provenance"]["signature"]
    if bad is None:
        block.pop("signature", None)
    else:
        block["signature"] = bad
    result = _client(fake).verify("dlv-1")
    assert result["verified"] is False
    assert result["verdict"] == "invalid"
