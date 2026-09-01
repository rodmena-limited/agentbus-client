"""Typed sync and async clients for the AgentBus API."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

import httpx

from .. import sealing
from ._reply_guard import _refuse_self_reply
from .attachments import _encode_attachments
from .errors import AgentBusError, TransportError, _raise_for
from .models import Delivery, _ack_window_seconds

if TYPE_CHECKING:  # #48: tell mypy what the assembled client provides
    from ._mixin_base import SyncClientBase as _MixinBase
else:  # runtime: no new base, no MRO change, no import cycle
    _MixinBase = object


class SyncMessagingMixin(_MixinBase):
    def send(
        self,
        to: Sequence[str] | str,
        subject: str = "",
        text: str | None = None,
        *,
        # #155: copied, not addressed. Cc recipients get the same delivery; the
        # difference is the intent they can read off it (to = act, cc = informed).
        cc: Sequence[str] | str | None = None,
        # #167: urgent | normal | background. Omitted = normal, so nothing
        # changes for a caller that never sets it.
        priority: str | None = None,
        html: str | None = None,
        thread_id: str | None = None,
        attachments: Sequence[str] | None = None,
        require_responsive: bool = False,
        # #168: refuse rather than queue if the recipient has declared itself
        # busy. `require_responsive` asks "is anyone home"; this asks "is anyone
        # FREE" — a distinction that only matters when you would rather route
        # elsewhere than wait.
        require_available: bool = False,
        # #174: message ids this is DERIVED FROM. A claim the bus records and
        # labels as the sender's, never as its own attestation.
        derived_from: Sequence[str] | None = None,
        # #169: a structured body the room's schema validates BEFORE the message
        # is accepted, so a malformed payload is a refusal to the sender rather
        # than a surprise for every consumer.
        payload: Any = None,
        # #172: "fire_and_forget" trades durability for cost — no store, no ack,
        # no redelivery. The right choice for a heartbeat, the wrong one for
        # anything you would miss.
        guarantee: str | None = None,
        agent: str | None = None,
        # SEV-2-D (#234): a caller wrapping .send() in try/except TransportError:
        # retry MUST pass the same key across attempts so the server dedupes. If
        # omitted, the SDK mints one per _request — safe for a caller that does
        # not retry, unsafe for one that does. Named explicitly so the option is
        # discoverable and the failure mode is documented.
        idempotency_key: str | None = None,
        # Ack-tracking (SPECS/0022): require the recipient to ack, with
        # exponential-backoff reminders until they do or the window elapses.
        #
        #   require_ack: bool  — "this message carries an ask; I want to know
        #                        it was answered, not just delivered"
        #   ack_window: timedelta | int | None — how long to keep reminding
        #                        (default 24h when require_ack is set). The
        #                        server caps at 168h (7 days).
        #
        # FORWARD-COMPATIBLE: a server that predates the delivery_reminders
        # table ignores these fields, so this flag is safe to pass before the
        # backend ships. The reminders light up when the table lands.
        #
        # TO ONLY, NEVER CC: a cc recipient is copied for information and is
        # never obligated to ack (Farshid's decision, locked in the spec).
        require_ack: bool = False,
        ack_window: Any = None,
    ) -> dict[str, Any]:
        recipients = [to] if isinstance(to, str) else list(to)
        copied = [cc] if isinstance(cc, str) else list(cc or [])
        ack_window_seconds = _ack_window_seconds(ack_window, default_when_set=require_ack)
        body = {
            "to": recipients,
            "cc": copied,
            "priority": priority,
            "subject": subject,
            "text": text,
            "html": html,
            "thread_id": thread_id,
            "attachments": _encode_attachments(attachments),
            "require_responsive": require_responsive,
            "require_available": require_available,
            "payload": payload,
            "guarantee": guarantee,
            "derived_from": list(derived_from) if derived_from else None,
        }
        if require_ack:
            body["require_ack"] = True
            if ack_window_seconds is not None:
                body["ack_window_seconds"] = ack_window_seconds
        body, resolved = self._seal_if_needed(body, agent)
        # AFTER SEALING, DELIBERATELY. The canonical form hashes the STORED
        # body, so on an encrypted workspace the signature covers the
        # ciphertext: the platform can still verify (it holds those bytes), and
        # a recipient verifies before decrypting. Signing the plaintext instead
        # would make every signature on an encrypted workspace unverifiable by
        # anyone but a recipient — a state nobody reads after the first week.
        body = self._sign_if_possible(body, agent, resolved)
        return self._request(
            "POST",
            "/v1/messages",
            json=body,
            agent=agent,
            idempotent=True,
            idempotency_key=idempotency_key,
        )

    def reply(
        self,
        message_id: str,
        text: str,
        *,
        # #155: opt-in reply-all, never the default. True addresses the sender
        # plus the parent message's recipients (Cc preserved, you excluded).
        reply_all: bool = False,
        cc: Sequence[str] | str | None = None,
        priority: str | None = None,
        subject: str | None = None,
        attachments: Sequence[str] | None = None,
        require_responsive: bool = False,
        agent: str | None = None,
        # SEV-2-D (#234): caller-supplied stable key for retry safety; see send().
        idempotency_key: str | None = None,
        # #53: a reply whose only recipient is YOU is refused unless you say so.
        allow_self: bool = False,
    ) -> dict[str, Any]:
        # ACCEPT EITHER ID KIND, which is what the skill documents
        # unconditionally and what the CLI and MCP already do. The SDK was the
        # third surface and was missed — MCP's own comment records the previous
        # instance of this exact miss ("fixed in `agentbus reply` and NOT
        # here"), so this is the same defect one surface further along.
        #
        # Resolved HERE rather than server-side on purpose: the reply route is
        # guarded by a `message_participant` rule that runs in a dependency and
        # rejects an unknown message id before any handler code, deliberately,
        # so the write path is no more of an existence oracle than the read path.
        message_id = self._as_message_id(message_id, agent=agent)
        payload = {
            "text": text,
            "reply_all": reply_all,
            "priority": priority,
            "cc": ([cc] if isinstance(cc, str) else list(cc)) if cc else None,
            "subject": subject,
            "attachments": _encode_attachments(attachments),
            "require_responsive": require_responsive,
        }
        # #220: A REPLY MUST SEAL TOO, and it could not, because it does not
        # know its own recipients — they are derived from the PARENT, server
        # side. So `reply` was the one send path that simply did not work on an
        # encrypted workspace: every attempt came back "the body must be sealed
        # by the client" and no client could comply. Two agents on two operating
        # systems reported it independently within minutes.
        #
        # Ask the server who the reply reaches, then seal to exactly those keys.
        # Re-deriving reply/reply-all addressing here would put that rule in two
        # places, which is the mistake #155 already paid for once.
        payload, resolved = self._seal_if_needed(
            payload,
            agent,
            resolve_path="/v1/recipients/resolve-reply",
            resolve_body={
                "message_id": message_id,
                "reply_all": reply_all,
                "to": payload.get("to"),
                "cc": payload.get("cc"),
                "subject": subject,
            },
        )
        # #220, second half: A REPLY WAS NEVER SIGNED, on any workspace. It could
        # not be — the signature covers the recipients and subject the SERVER
        # derives for a reply, so the resolver returns them and we sign over that
        # answer rather than re-deriving "Re: " here.
        _refuse_self_reply(resolved, agent or self.agent, message_id, allow_self=allow_self)
        payload = self._sign_if_possible(payload, agent, resolved)
        return self._request(
            "POST",
            f"/v1/messages/{message_id}/reply",
            json=payload,
            agent=agent,
            idempotent=True,
            idempotency_key=idempotency_key,
        )

    def inbox(
        self,
        cursor: int = 0,
        *,
        limit: int = 50,
        label: str | None = None,
        wait: int = 0,
        unread: bool = False,
        agent: str | None = None,
    ) -> list[Delivery]:
        """One page of deliveries after `cursor`. `wait` long-polls (max 55s).

        `unread=True` asks the SERVER for unread only. Page-and-filter from
        cursor 0 cannot answer "what is unread": cursor 0 is the OLDEST page,
        so the window fills with read mail as history grows and the filter goes
        blind while looking exactly like an empty inbox.
        """
        params: dict[str, Any] = {"cursor": cursor, "limit": limit}
        if label:
            params["label"] = label
        if unread:
            params["unread"] = "true"
        if wait:
            params["wait"] = min(wait, 55)
        result = self._request(
            "GET",
            "/v1/inbox",
            params=params,
            agent=agent,
            timeout=max(self.timeout, wait + 10) if wait else self.timeout,
        )
        return [Delivery.from_api(m) for m in result["messages"]]

    def follow(
        self, cursor: int = 0, *, agent: str | None = None, wait: int = 30
    ) -> Iterator[Delivery]:
        """Yield deliveries forever, advancing the cursor as they are consumed."""
        while True:
            batch = self.inbox(cursor, wait=wait, agent=agent)
            before = cursor
            for delivery in batch:
                cursor = max(cursor, delivery.seq)
                yield delivery
            if batch and cursor <= before:
                # Progress guard (issuedb #29): never re-fetch the same page forever.
                raise AgentBusError(
                    f"inbox page of {len(batch)} did not advance the cursor past {before}"
                )

    def read(self, delivery_id: str, agent: str | None = None, raw: bool = False) -> dict[str, Any]:
        """Read one delivery, unsealing it when this machine holds the key.

        Unsealing happens HERE rather than being an extra step the caller must
        remember: a reader that forgets it gets ciphertext and no explanation,
        and "remember to decrypt" is exactly the kind of instruction that is
        followed for a week.

        `raw=True` returns the body EXACTLY as stored, skipping the unseal.
        #39: without it, this client's own decoder is the only practical
        witness to its own correctness — a recipient wanting to check their
        mail against stock `age` had to hand-build a curl auth header. A
        decoder that can only be verified by itself is a check that cannot
        go red.
        """
        delivery = self._request("GET", f"/v1/deliveries/{delivery_id}", agent=agent)
        return delivery if raw else self.unseal_message(delivery)

    def ack(self, delivery_id: str, agent: str | None = None) -> dict[str, Any]:
        return self._request("POST", f"/v1/deliveries/{delivery_id}/ack", agent=agent)

    def label(
        self,
        delivery_id: str,
        add: Sequence[str] | None = None,
        remove: Sequence[str] | None = None,
        agent: str | None = None,
    ) -> list[str]:
        payload = {"add": list(add or []), "remove": list(remove or [])}
        return self._request(
            "POST", f"/v1/deliveries/{delivery_id}/labels", json=payload, agent=agent
        )["labels"]

    def thread(self, thread_id: str) -> dict[str, Any]:
        """Read a whole conversation, unsealing each message where possible.

        F10 (issuedb #11): on an encrypted workspace the server holds
        ciphertext by design (end-to-end sealing — no private key server-
        side). Before this fix, `agentbus thread --json` returned each
        `text_body` as the raw age envelope, and reading N turns cost N
        `agentbus show` round trips just to unseal them. The fix mirrors
        what `read()` already does per delivery: iterate messages once and
        call `unseal_message` on each, so `sealed_unreadable` appears in
        place of a body the caller cannot open (matching `show`).
        """
        result = self._request("GET", f"/v1/threads/{thread_id}")
        for msg in result.get("messages") or []:
            self.unseal_message(msg)
        return result

    def threads(self, limit: int = 50) -> list[dict[str, Any]]:
        return self._request("GET", "/v1/threads", params={"limit": limit})["threads"]

    def raw(self, message_id: str) -> str:
        """The canonical RFC822 artifact for a message."""
        try:
            response = self._client.get(f"/v1/messages/{message_id}/raw", headers=self._headers())
        except httpx.HTTPError as exc:
            raise TransportError(str(exc)) from exc
        _raise_for(response)
        return response.text

    def status(
        self,
        state: str | None = None,
        *,
        seconds: int | None = None,
        reason: str | None = None,
        hold_below: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Read or declare this agent's availability (#187).

        With no `state` this READS. With one it declares:

            online    the default; releases anything withheld
            busy      occupied until T — delivers normally, tells senders
            away      same, different word for a human reading the roster
            dnd       WITHHOLDS normal and background; urgent still arrives
            offline   WITHHOLDS everything, and nothing tries to wake you

        `dnd` and `offline` are the recipient deciding, which is what #168's
        `busy()` was missing: it declared, and every sender was free to ignore
        it. Withheld mail is stored and delivered — with a wake — when the
        status clears or expires.
        """
        target = agent or self.agent
        if not target:
            raise AgentBusError("which agent? pass agent= or bind the client")
        if state is None:
            result: dict[str, Any] = self._request(
                "GET", f"/v1/agents/{target}/availability", agent=target
            )
            return result
        body: dict[str, Any] = {"state": state}
        if seconds is not None:
            body["seconds"] = seconds
        if reason is not None:
            body["reason"] = reason
        if hold_below is not None:
            body["hold_below"] = hold_below
        declared: dict[str, Any] = self._request(
            "PUT", f"/v1/agents/{target}/availability", json=body, agent=target
        )
        return declared

    def busy(
        self,
        seconds: int,
        *,
        reason: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Declare how long THIS agent cannot take new work (#168); 0 clears it.

        A duration, not a flag: it expires by itself, so a crash cannot leave
        this agent looking permanently unavailable.
        """
        target = agent or self.agent
        if not target:
            raise AgentBusError("which agent? pass agent= or bind the client")
        result: dict[str, Any] = self._request(
            "POST",
            f"/v1/agents/{target}/busy",
            json={"seconds": seconds, "reason": reason},
            agent=target,
        )
        return result

    def _seal_if_needed(
        self,
        payload: dict[str, Any],
        agent: str | None,
        *,
        resolve_path: str = "/v1/recipients/resolve",
        resolve_body: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Seal the body when the workspace is encrypted, or leave it alone.

        THE SERVER IS ASKED, NOT GUESSED. `POST /v1/recipients/resolve` returns
        the concrete agents a send would reach — which only the server can know
        for `room:` and `tag:` targets — along with their public keys and,
        crucially, the names of any recipient that has published none.

        A CLIENT THAT SEALED ONLY TO THE KEYS IT FOUND would silently exclude
        whoever had none: a message that looks delivered and is unreadable by
        half its recipients, with nothing anywhere saying so. So a missing key
        is a refusal, not a warning.

        The sender's own key is always added, so an agent can read its own sent
        mail. Without it, `agentbus sent` shows you ciphertext you wrote.

        RETURNS THE RESOLVER'S ANSWER ALONGSIDE THE PAYLOAD, because the
        signature needs it: on a reply the server derives the recipients and the
        "Re: " subject, and a signature covers what the server STORES. `None`
        means the question was never answered -- no such route, or refused --
        and the caller then signs over its own payload, as it always did.
        """

        # A SERVER THAT REFUSES THE QUESTION MUST NOT COST YOU THE SEND.
        #
        # An older server does not have this route at all, and a server whose
        # scope allowlist has not caught up refuses it — which is exactly what
        # happened: 0.5.0 made resolve unconditional and a send-scope key got
        # 403 here, before it ever reached the send. The client then raised and
        # the message was never posted.
        #
        # Falling through is SAFE, not a bypass, because the server owns the
        # guarantee: `accept_message` refuses an unsealed body on an encrypted
        # workspace. So the worst case here is a clear refusal from the surface
        # that actually knows, instead of a 403 naming an endpoint the user
        # never called.
        try:
            resolved = self._request(
                "POST",
                resolve_path,
                json=resolve_body if resolve_body is not None else payload,
                agent=agent,
            )
        except AgentBusError as exc:
            if getattr(exc, "status", None) in (403, 404, 405):
                return payload, None
            raise
        return self._apply_seal(payload, resolved, agent=agent), resolved

    def _seal_to_self(self, payload: dict[str, Any], agent: str | None) -> dict[str, Any]:
        """Seal `text` to this agent's own key, or leave it alone.

        Asks the server whether the workspace is encrypted rather than guessing,
        the same way `_seal_if_needed` does, and falls through untouched when the
        question cannot be answered — a refusal must never cost you the write.
        """
        try:
            resolved = self._request(
                "POST", "/v1/recipients/resolve", json={"to": [agent or self.agent]}, agent=agent
            )
        except AgentBusError as exc:
            if getattr(exc, "status", None) in (403, 404, 405):
                return payload
            raise
        if not resolved.get("encrypted"):
            return payload
        # B2 (reliability audit follow-up): same unbound-client guard as
        # _apply_seal — an untyped ValueError from ensure_keypair would
        # escape an SDK caller that catches AgentBusError.
        acting = agent or self.agent
        if not acting:
            raise AgentBusError(
                "cannot seal: this workspace is encrypted but no acting agent "
                "is set. A sealing key belongs to ONE agent, so the client "
                "needs agent=... or AGENTBUS_AGENT to know whose key to seal to."
            )
        _private, own_public = sealing.ensure_keypair(acting)
        sealed = dict(payload)
        sealed["text"] = sealing.seal_for(payload["text"], [own_public])
        sealed["sealed"] = True
        return sealed

    def _as_message_id(self, ident: str, *, agent: str | None = None) -> str:
        """A delivery id resolved to its message id, or the input unchanged.

        Every surface an agent reads prints `agentbus show <DELIVERY_ID>`, so
        pasting that into `reply` is the natural move — and it used to fail with
        a bare `not_found` that gave no hint the id was the wrong KIND.
        """
        try:
            delivery = self.read(ident, agent=agent)
        except AgentBusError:
            return ident  # not a delivery of ours; let the server speak
        return str(delivery.get("message_id") or ident)
