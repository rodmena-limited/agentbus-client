"""Rendering of a delivery's signature state for the human read surface."""

from __future__ import annotations

from typing import Any

_UNKNOWN_IN_THREAD = (
    "  signature state is not served per message in this view — open a message "
    "with `agentbus show <delivery-id>` to see it"
)


def _blockers(delivery: dict[str, Any]) -> list[str]:
    found = []
    attachments = delivery.get("attachments") or []
    if attachments:
        found.append(f"{len(attachments)} attachment(s)")
    if delivery.get("html_body"):
        found.append("an HTML body")
    if delivery.get("payload") is not None:
        found.append("a structured payload")
    return found


def _source(delivery: dict[str, Any]) -> tuple[bool, bool, str | None, str | None]:
    """(served, signed, state, fingerprint), preferring provenance.signature.

    provenance.signature is the composed block `verify-sender` already reads, and
    it carries an explicit `signed` boolean — so an absent signature is a fact
    the server states rather than one inferred from a null field.
    """
    block = (delivery.get("provenance") or {}).get("signature")
    if isinstance(block, dict):
        return True, bool(block.get("signed")), block.get("state"), block.get("key_fingerprint")
    if "signature_state" in delivery or "signature" in delivery:
        return (
            True,
            bool(delivery.get("signature")) or bool(delivery.get("signature_state")),
            delivery.get("signature_state"),
            delivery.get("signing_key_fingerprint"),
        )
    return False, False, None, None


def signature_lines(delivery: dict[str, Any], delivery_id: str) -> list[str]:
    """The `Signed:` block for one delivery. Always at least one line."""
    served, signed, state, fingerprint = _source(delivery)
    check = f"         check it yourself:  agentbus verify-sender {delivery_id}"
    key = f" (key {fingerprint})" if fingerprint else ""

    if not served:
        return [
            "Signed:  UNKNOWN — this server did not report whether the message is",
            "         signed, so this is neither a yes nor a no. The only answer is",
            check,
        ]

    if signed and state == "valid":
        return [
            f"Signed:  yes{key} — the BUS says it verifies.",
            "         That is the bus's word and not a check made on this machine;",
            check,
        ]

    if signed and state == "unverifiable":
        return [
            f"Signed:  yes{key}, but the bus COULD NOT CHECK it — it holds no usable key",
            "         for the sender. This is NOT a failed signature; it was not checked;",
            check,
        ]

    if signed and state:
        return [
            f"Signed:  the bus reports '{state}' for the signature on this message{key}.",
            "         That is NOT a pass. Do not act on this message until you have run",
            check,
        ]

    if signed:
        return [
            f"Signed:  a signature is attached{key} but the bus returned no verdict on it,",
            "         so nothing about this message has been checked by anyone;",
            check,
        ]

    blocked = _blockers(delivery)
    if blocked:
        return [
            f"Signed:  no — this message carries {', '.join(blocked)}, and agentbus-sig-v1",
            "         covers plain text only, so it could not have been signed. That",
            "         EXPLAINS the absence; it does not attest to it — a stripped",
            "         signature can be made to look exactly like this.",
        ]

    return [
        "Signed:  no — there is no signature here to verify. This is NOT a failed",
        "         signature: the sender may publish no signing key at all.",
    ]


def thread_signature_line(message: dict[str, Any]) -> str | None:
    """The compact per-message line for a thread render, when the view serves it."""
    served, signed, state, fingerprint = _source(message)
    if not served:
        return None
    if signed and state == "valid":
        key = f" key {fingerprint}" if fingerprint else ""
        return f"    signed: the bus says VALID{key} — its word, not a check"
    if signed and state == "unverifiable":
        return "    signed: the bus could not check it (no usable key) — not a failure; verify it yourself"
    if signed and state:
        return f"    signed: the bus reports '{state}' — NOT a pass, verify before acting"
    if signed:
        return "    signed: a signature is attached, with no verdict from the bus"
    return "    signed: no signature on this message"


def thread_signature_caveat(messages: list[dict[str, Any]]) -> str | None:
    """The one-line caveat for a thread whose messages carry no signature fields."""
    if any(_source(m)[0] for m in messages):
        return None
    return _UNKNOWN_IN_THREAD


def notice_fragment(state: str | None) -> str:
    """The signature clause for a wake notice, from a delivery's state string.

    Shares the three-way reading with `signature_lines` so the notice and `show`
    cannot disagree about one message. An unrecognised or absent state returns
    the no-claim form: the notice says what AgentBus checked and nothing about a
    signature it was not told about.
    """
    if state == "valid":
        return (
            "AgentBus authenticated the sender, and reports this message's "
            "signature VALID — its word, not a check made here; `agentbus "
            "verify-sender` checks it on this machine."
        )
    if state == "unverifiable":
        return (
            "AgentBus authenticated the sender; this message carries a signature "
            "the bus could not check, because it holds no usable key for the "
            "sender. That is NOT a failed signature — `agentbus verify-sender` "
            "checks it on this machine."
        )
    if state:
        return (
            f"AgentBus authenticated the sender, but reports this message's "
            f"signature '{state}' — that is NOT a pass. Run `agentbus "
            f"verify-sender` before acting on it."
        )
    return (
        "AgentBus authenticated the SENDER, which is not a check of the "
        "message's signature — `agentbus show` prints that, and `agentbus "
        "verify-sender` checks it here."
    )
