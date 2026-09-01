"""Typed sync and async clients for the AgentBus API."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import httpx

from ._reply_guard import _refuse_self_reply
from .attachments import _encode_attachments
from .errors import AgentBusError, TransportError, _raise_for
from .models import Delivery, _ack_window_seconds

if TYPE_CHECKING:  # #48: tell mypy what the assembled client provides
    from ._mixin_base import AsyncClientBase as _MixinBase
else:  # runtime: no new base, no MRO change, no import cycle
    _MixinBase = object


class AsyncMessagingMixin(_MixinBase):
    async def send(
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
        # The async class MIRRORS the sync one deliberately. It has drifted
        # before — `phonebook(label=)` landed on one and not the other — and a
        # caller who switches to async should not silently lose options.
        require_available: bool = False,
        # SEV-2-H (#234): missing `derived_from` on the async surface — drift.
        # Placed at the same position as AgentBus.send for the parity test to pass.
        derived_from: Sequence[str] | None = None,
        payload: Any = None,
        guarantee: str | None = None,
        agent: str | None = None,
        # SEV-2-D (#234): stable key for retry safety; see AgentBus.send().
        idempotency_key: str | None = None,
        # Ack-tracking (SPECS/0022): PARITY with AgentBus.send. Same rules:
        # require_ack binds TO only, never CC; forward-compatible with
        # servers that predate the delivery_reminders table.
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
        body, resolved = await self._seal_if_needed(body, agent)
        # #220: SIGNED TOO. This surface signed nothing at all — `send` on the
        # sync client was the only one of the four that ever did. Same ordering
        # as there: seal first, so the signature covers the stored bytes.
        body = self._sign_if_possible(body, agent, resolved)
        return await self._request(
            "POST",
            "/v1/messages",
            json=body,
            agent=agent,
            idempotent=True,
            idempotency_key=idempotency_key,
        )

    async def reply(
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
        idempotency_key: str | None = None,
        # #53: parity with the sync guard — see _refuse_self_reply.
        allow_self: bool = False,
    ) -> dict[str, Any]:
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
        payload, resolved = await self._seal_if_needed(
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
        # #220, second half: signed over the server's own answer, for the same
        # reason as the sync reply. This surface signed nothing before.
        _refuse_self_reply(resolved, agent or self.agent, message_id, allow_self=allow_self)
        payload = self._sign_if_possible(payload, agent, resolved)
        return await self._request(
            "POST",
            f"/v1/messages/{message_id}/reply",
            json=payload,
            agent=agent,
            idempotent=True,
            idempotency_key=idempotency_key,
        )

    async def inbox(
        self,
        cursor: int = 0,
        *,
        limit: int = 50,
        label: str | None = None,
        wait: int = 0,
        unread: bool = False,
        agent: str | None = None,
    ) -> list[Delivery]:
        params: dict[str, Any] = {"cursor": cursor, "limit": limit}
        if label:
            params["label"] = label
        if unread:
            params["unread"] = "true"
        if wait:
            params["wait"] = min(wait, 55)
        result = await self._request(
            "GET",
            "/v1/inbox",
            params=params,
            agent=agent,
            timeout=max(self.timeout, wait + 10) if wait else self.timeout,
        )
        return [Delivery.from_api(m) for m in result["messages"]]

    async def follow(self, cursor: int = 0, *, agent: str | None = None, wait: int = 30) -> Any:
        """Async generator yielding deliveries as they arrive — async twin.

        Return type is Any (not AsyncIterator[Delivery]) because it is an async
        GENERATOR, which typing expresses as AsyncGenerator[Delivery, None]. The
        loose annotation keeps the parity test's param-name check simple; the
        docstring is authoritative.
        """
        while True:
            batch = await self.inbox(cursor, wait=wait, agent=agent)
            for delivery in batch:
                cursor = max(cursor, delivery.seq)
                yield delivery

    async def read(
        self, delivery_id: str, agent: str | None = None, raw: bool = False
    ) -> dict[str, Any]:
        """Read one delivery, unsealing it when this machine holds the key.

        SEV-4 (#234): the async twin USED to skip unsealing — a bug the sync one
        fixed and this one inherited from the drift the parity test now catches.
        A caller who switched sync -> async silently got ciphertext, and
        "remember to decrypt" is exactly the instruction that gets followed for
        about a week.

        `raw=True` returns the body EXACTLY as stored, skipping the unseal
        (#39) — kept in step with the sync twin, which is the pair the parity
        test exists to police.
        """
        delivery = await self._request("GET", f"/v1/deliveries/{delivery_id}", agent=agent)
        return delivery if raw else self.unseal_message(delivery)

    async def ack(self, delivery_id: str, agent: str | None = None) -> dict[str, Any]:
        return await self._request("POST", f"/v1/deliveries/{delivery_id}/ack", agent=agent)

    async def label(
        self,
        delivery_id: str,
        add: Sequence[str] | None = None,
        remove: Sequence[str] | None = None,
        agent: str | None = None,
    ) -> list[str]:
        """Add/remove labels on a delivery — async twin."""
        payload = {"add": list(add or []), "remove": list(remove or [])}
        result: dict[str, Any] = await self._request(
            "POST", f"/v1/deliveries/{delivery_id}/labels", json=payload, agent=agent
        )
        return result["labels"]

    async def thread(self, thread_id: str) -> dict[str, Any]:
        """F10 (issuedb #11): match the sync client — unseal each message."""
        result = await self._request("GET", f"/v1/threads/{thread_id}")
        for msg in result.get("messages") or []:
            self.unseal_message(msg)
        return result

    async def threads(self, limit: int = 50) -> list[dict[str, Any]]:
        """List threads the acting agent participates in — async twin."""
        result: dict[str, Any] = await self._request("GET", "/v1/threads", params={"limit": limit})
        return result["threads"]

    async def raw(self, message_id: str) -> str:
        """The canonical RFC822 artifact for a message — async twin of AgentBus.raw."""
        try:
            response = await self._client.get(
                f"/v1/messages/{message_id}/raw", headers=self._headers()
            )
        except httpx.HTTPError as exc:
            raise TransportError(str(exc)) from exc
        _raise_for(response)
        return response.text

    async def status(
        self,
        state: str | None = None,
        *,
        seconds: int | None = None,
        reason: str | None = None,
        hold_below: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Async mirror of AgentBus.status() (#187).

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
            result: dict[str, Any] = await self._request(
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
        declared: dict[str, Any] = await self._request(
            "PUT", f"/v1/agents/{target}/availability", json=body, agent=target
        )
        return declared

    async def busy(
        self, seconds: int, *, reason: str | None = None, agent: str | None = None
    ) -> dict[str, Any]:
        """Declare this agent busy for a duration — async twin of AgentBus.busy."""
        return await self.status("busy", seconds=seconds, reason=reason, agent=agent)

    async def _seal_if_needed(
        self,
        payload: dict[str, Any],
        agent: str | None,
        *,
        resolve_path: str = "/v1/recipients/resolve",
        resolve_body: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """The async twin of the sync client's sealer. #220.

        This class sealed NOTHING before: every async send on an encrypted
        workspace was refused by the server with "the body must be sealed by the
        client", and the async surface had no way to comply. The sealing rule
        lived on the sync class only — one surface fixed, the other left, which
        is the exact shape this codebase keeps paying for.

        Only the request differs, so only the request is duplicated; the
        decision itself is `_apply_seal` on the shared base.

        RETURNS THE RESOLVER'S ANSWER ALONGSIDE THE PAYLOAD, because the
        signature needs it: on a reply the server derives the recipients and the
        "Re: " subject, and a signature covers what the server STORES. `None`
        means the question was never answered -- no such route, or refused --
        and the caller then signs over its own payload, as it always did.
        """
        try:
            resolved = await self._request(
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

    async def _seal_to_self_async(
        self, payload: dict[str, Any], agent: str | None
    ) -> dict[str, Any]:
        """Seal `text` to this agent's own key on an encrypted workspace —
        async twin of the sync `_seal_to_self`.

        C (reliability audit follow-up): this method did NOT exist even
        though async `secure_draft` called it — a guaranteed AttributeError
        on the async draft path. It mirrors the sync implementation (the
        resolve round-trip is the only I/O, hence `async def`), including
        the B2 unbound-client guard.
        """
        from .. import sealing

        try:
            resolved = await self._request(
                "POST",
                "/v1/recipients/resolve",
                json={"to": [agent or self.agent]},
                agent=agent,
            )
        except AgentBusError as exc:
            if getattr(exc, "status", None) in (403, 404, 405):
                return payload
            raise
        if not resolved.get("encrypted"):
            return payload
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
