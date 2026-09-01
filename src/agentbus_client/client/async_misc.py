"""Typed sync and async clients for the AgentBus API."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

import httpx

from .._timefmt import _as_instant, _duration_seconds, _expiry_instant
from .errors import AgentBusError, TransportError, _raise_for
from .models import _max_attachment_bytes

if TYPE_CHECKING:  # #48: tell mypy what the assembled client provides
    from ._mixin_base import AsyncClientBase as _MixinBase
else:  # runtime: no new base, no MRO change, no import cycle
    _MixinBase = object


# The SERVED RemindRequest field set (verified against the deployed OpenAPI).
# The route forbids extra inputs, so anything outside this fails the whole
# create — not just the offending field.
_REMIND_FIELDS = frozenset(
    {
        "target",
        "subject",
        "text",
        "sealed",
        "delay_seconds",
        "due_at",
        "expires_at",
        "repeat",
        "timezone",
    }
)


class AsyncMiscMixin(_MixinBase):
    async def room_history(
        self,
        room: str,
        *,
        limit: int | None = None,
        since: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Async mirror of AgentBus.room_history() (#170)."""
        target = agent or self.agent
        if not target:
            raise AgentBusError("which agent? pass agent= or bind the client")
        params: dict[str, Any] = {}
        if limit is not None:
            params["limit"] = limit
        if since is not None:
            params["since"] = since
        result: dict[str, Any] = await self._request(
            "GET", f"/v1/rooms/{room}/history", params=params, agent=target
        )
        # Async twin — MIRRORS the sync unsealing deliberately. This pair has
        # drifted on exactly this before: async `read` once skipped unsealing
        # entirely and a caller switching to async silently got ciphertext.
        for msg in result.get("messages") or []:
            self.unseal_message(msg)
        return result

    async def room_schema(self, room: str, *, agent: str | None = None) -> dict[str, Any]:
        """Async mirror of AgentBus.room_schema() (#169)."""
        result: dict[str, Any] = await self._request("GET", f"/v1/rooms/{room}/schema", agent=agent)
        return result

    async def set_room_schema(
        self, room: str, schema: dict[str, Any] | None, *, agent: str | None = None
    ) -> dict[str, Any]:
        """Async mirror of AgentBus.set_room_schema() (#169)."""
        target = agent or self.agent
        if not target:
            raise AgentBusError("which agent? pass agent= or bind the client")
        result: dict[str, Any] = await self._request(
            "PUT", f"/v1/rooms/{room}/schema", json={"schema": schema}, agent=target
        )
        return result

    async def join_room(self, room: str, *, agent: str | None = None) -> dict[str, Any]:
        """Async mirror of AgentBus.join_room()."""
        target = agent or self.agent
        if not target:
            raise AgentBusError("which agent? pass agent= or bind the client")
        result: dict[str, Any] = await self._request("POST", f"/v1/rooms/{room}/join", agent=target)
        return result

    async def verify(self, delivery_id: str, *, agent: str | None = None) -> dict[str, Any]:
        """Verify a message's signature yourself — async twin of AgentBus.verify.

        SEV-3 (#234): reads the sender from `sender_agent_name` when the server
        publishes it, falling back to the historical " via AgentBus" strip. The
        strip is fragile — a display-suffix change breaks verification — but
        removing it outright would break older servers, so it stays as a fallback.
        """
        from .. import _signing  # package root, NOT client/ — see below

        message = await self.read(delivery_id, agent=agent)
        provenance = message.get("provenance") or {}
        block = provenance.get("signature") or {}
        if not block.get("signed"):
            return {
                "verified": False,
                "verdict": "unsigned",
                "reason": "unsigned",
                "platform_said": None,
            }
        sender = message.get("sender_agent_name") or (
            (message.get("sender_display") or "").replace(" via AgentBus", "")
        )
        keys = await self._request(
            "GET",
            f"/v1/agents/{sender}/pubkey",
            params={"algorithm": "ed25519"},
            agent=agent,
        )
        fingerprint = block.get("key_fingerprint")
        match = next(
            (k for k in keys.get("keys") or [] if k.get("fingerprint") == fingerprint), None
        )
        if match is None:
            return {
                "verified": False,
                "verdict": "unverifiable",
                "reason": "signing key not published for this fingerprint",
                "platform_said": block.get("state"),
            }
        signed_for = (block.get("verify_yourself") or {}).get("signed_recipients") or {}
        if isinstance(signed_for, str):
            signed_for = json.loads(signed_for)
        if not message.get("body_sha256"):
            return {
                "verified": False,
                "verdict": "unverifiable",
                "reason": (
                    "cannot check: this server did not return `body_sha256` for "
                    "this copy of the message. This is NOT a failed signature."
                ),
                "platform_said": block.get("state"),
            }
        payload = _signing.canonical_bytes(
            sender=sender,
            to=list(signed_for.get("to") or []),
            cc=list(signed_for.get("cc") or []),
            subject=message.get("subject"),
            priority=message.get("priority") or "normal",
            body_sha256=message.get("body_sha256"),
        )
        try:
            _signing.verify(match["public_key"], payload, block["signature"])
        except _signing.BadSignature as exc:
            return {
                "verified": False,
                "verdict": "invalid",
                "reason": f"signature does not verify: {exc}",
                "platform_said": block.get("state"),
            }
        return {
            "verified": True,
            "verdict": "valid",
            "reason": "signature verifies against the sender's published key",
            "platform_said": block.get("state"),
        }

    async def get_claim(self, message_id: str, agent: str | None = None) -> dict[str, Any]:
        """Read the claim carried by a message, with verdicts — async twin."""
        return await self._request("GET", f"/v1/messages/{message_id}/claim", agent=agent)

    async def record_verdict(
        self,
        message_id: str,
        *,
        result: str,
        observed_exit: int | None = None,
        observed_output: str | None = None,
        client_version: str | None = None,
        env_note: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Record THIS agent's verdict on a claim it ran itself — async twin."""
        payload: dict[str, Any] = {"result": result}
        if observed_exit is not None:
            payload["observed_exit"] = observed_exit
        if observed_output is not None:
            payload["observed_output"] = observed_output
        if client_version is not None:
            payload["client_version"] = client_version
        if env_note is not None:
            payload["env_note"] = env_note
        data = await self._request(
            "POST", f"/v1/messages/{message_id}/claim/verdict", json=payload, agent=agent
        )
        return data if isinstance(data, dict) else {"verdict_id": "", "result": result}

    async def request_approval(
        self,
        title: str,
        *,
        kind: str = "generic",
        summary: str | None = None,
        proposed_action: dict[str, Any] | None = None,
        context: dict[str, Any] | None = None,
        # SEV-2-H (#234): missing on the async surface — drift.
        thread_id: str | None = None,
        expires_in_minutes: int | None = None,
        agent: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "title": title,
            "kind": kind,
            "summary": summary,
            "proposed_action": proposed_action or {},
            "context": context or {},
            "thread_id": thread_id,
            "expires_in_minutes": expires_in_minutes,
        }
        return await self._request(
            "POST",
            "/v1/approvals",
            json=payload,
            agent=agent,
            idempotent=True,
            idempotency_key=idempotency_key,
        )

    async def approval(self, approval_id: str, wait: int = 0) -> dict[str, Any]:
        params = {"wait": min(wait, 55)} if wait else {}
        return await self._request(
            "GET",
            f"/v1/approvals/{approval_id}",
            params=params,
            timeout=max(self.timeout, wait + 10) if wait else self.timeout,
        )

    async def remind(
        self,
        text: str,
        *,
        target: str | None = None,
        subject: str = "",
        delay: Any = None,
        at: Any = None,
        expire: Any = None,
        repeat: str | None = None,
        repeat_until: Any = None,
        timezone: str | None = None,
        agent: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Async twin of AgentBus.remind — MIRRORS IT DELIBERATELY.

        Same signature, same order, same sealing rule. This pair has drifted
        before (`phonebook(label=)` on one and not the other; async `read` once
        skipped unsealing entirely), and a caller who switches to async must not
        silently lose the seal — which on this surface would mean a plaintext
        body sitting at rest until the reminder is due.
        """
        # ONE OF delay OR at, NEVER BOTH — the server 422s the pair, and
        # catching it here names the conflict instead of relaying a status
        # code. They are the same statement in two forms.
        # repeat_until IS NOT ON THE WIRE. The served RemindRequest forbids
        # extra inputs (verified: 422 extra_forbidden), so sending it fails
        # the whole create. Refused here with the reason rather than passed
        # through to become a confusing server error.
        if repeat_until is not None:
            raise ValueError(
                "repeat_until is not accepted by the server yet — a recurring "
                "reminder currently has no end date. Track it and cancel with "
                "cancel_remind(), or omit it."
            )
        if delay is not None and at is not None:
            raise ValueError("pass delay OR at, not both — they say the same thing two ways")
        body: dict[str, Any] = {"subject": subject, "text": text}
        if target:
            body["target"] = target
            body, _resolved = await self._seal_if_needed(
                body,
                agent,
                resolve_body={"to": [target], "subject": subject},
            )
        else:
            body = await self._seal_to_self_async(body, agent)
        # STRIP WHAT THE SEALER ADDS FOR THE SEND ROUTE BUT REMINDERS FORBID.
        # `_apply_seal` sets html=None and (for attachments) other keys,
        # because it was written for POST /v1/messages where those fields
        # exist. The reminders route forbids extra inputs, so a TARGETED
        # reminder — the only path that goes through _seal_if_needed — died
        # with "html: Extra inputs are not permitted" while a self-note
        # worked. Reported by macbook-admin-bd8e86 and reproduced here.
        #
        # Filtered rather than fixed in _apply_seal: that helper is shared
        # with send/reply/forward, where html IS a legal field, and
        # narrowing it there to suit one caller would break the others.
        body = {k: v for k, v in body.items() if k in _REMIND_FIELDS}
        for key, value in (
            ("delay_seconds", _duration_seconds(delay)),
            ("due_at", _as_instant(at)),
            ("expires_at", _expiry_instant(expire, delay, at)),
            ("repeat", repeat),
            ("timezone", timezone),
        ):
            if value is not None:
                body[key] = value
        return await self._request(
            "POST",
            "/v1/reminders",
            json=body,
            agent=agent,
            idempotent=True,
            idempotency_key=idempotency_key,
        )

    async def reminds(self, *, agent: str | None = None, all: bool = False) -> list[dict[str, Any]]:
        """Scheduled reminders — async twin of AgentBus.reminds."""
        # #336 — see the sync twin: `all=true` was not a parameter this API has,
        # so the state filter never reached the server and truncation happened
        # BEFORE it. Live recurrences were crowded out by finished one-shots.
        page = await self.reminds_page(agent=agent, all=all)
        return list(page["reminders"])

    async def reminds_page(self, *, agent: str | None = None, all: bool = False) -> dict[str, Any]:
        """Async twin of AgentBus.reminds_page — the full envelope (#336)."""
        params = {"state": "all" if all else "scheduled", "limit": "200"}
        result: dict[str, Any] = await self._request(
            "GET", "/v1/reminders", params=params, agent=agent
        )
        return result

    async def cancel_remind(self, reminder_id: str, agent: str | None = None) -> dict[str, Any]:
        """Cancel a scheduled reminder — async twin."""
        return await self._request("DELETE", f"/v1/reminders/{reminder_id}", agent=agent)

    async def drafts(self, agent: str | None = None) -> list[dict[str, Any]]:
        """List this agent's drafts — async twin."""
        result: dict[str, Any] = await self._request("GET", "/v1/drafts", agent=agent)
        return result["drafts"]

    async def create_draft(
        self,
        to: Sequence[str],
        subject: str = "",
        text: str | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Store a draft, sealed to YOUR OWN key on an encrypted workspace — async twin.

        Calls the same _seal_to_self path as the sync twin; sealing is a local
        operation so no async-specific machinery is needed inside it.
        """
        body: dict[str, Any] = {"to": list(to), "subject": subject, "text": text}
        if text:
            body = await self._seal_to_self_async(body, agent)
        return await self._request("POST", "/v1/drafts", json=body, agent=agent)

    async def send_draft(self, draft_id: str, agent: str | None = None) -> dict[str, Any]:
        """Send a stored draft, sealing it first on an encrypted workspace — async twin."""
        draft = await self._request("GET", f"/v1/drafts/{draft_id}", agent=agent)
        recipients = draft.get("recipients") or draft.get("to") or []
        if isinstance(recipients, str):
            recipients = json.loads(recipients)
        opened = self.unseal_message({"text_body": draft.get("text_body") or draft.get("text")})
        if opened.get("sealed_unreadable"):
            raise AgentBusError(
                "cannot send this draft: its stored body is sealed to a key this "
                f"machine does not hold ({opened['sealed_unreadable']})"
            )
        payload: dict[str, Any] = {
            "text": opened.get("text_body"),
            "subject": draft.get("subject"),
            "to": list(recipients),
        }
        payload, resolved = await self._seal_if_needed(payload, agent)
        if not payload.get("sealed"):
            plain: dict[str, Any] = await self._request(
                "POST", f"/v1/drafts/{draft_id}/send", agent=agent
            )
            return plain
        payload = self._sign_if_possible(payload, agent, resolved)
        body = {
            "text": payload["text"],
            "sealed": True,
            "signature": payload.get("signature"),
            "signing_key_fingerprint": payload.get("signing_key_fingerprint"),
        }
        sent: dict[str, Any] = await self._request(
            "POST",
            f"/v1/drafts/{draft_id}/send",
            json=body,
            agent=agent,
            idempotent=True,
        )
        return sent

    async def usage(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/usage")

    async def reminders_owing(self) -> list[dict[str, Any]]:
        """Async parity — see AgentBus.reminders_owing."""
        data = await self._request("GET", "/v1/reminders/owing")
        return list(data.get("owing") or [])

    async def reminders_owed(self) -> list[dict[str, Any]]:
        """Async parity — see AgentBus.reminders_owed."""
        data = await self._request("GET", "/v1/reminders/owed")
        return list(data.get("owed") or [])

    async def sent(
        self, *, limit: int = 50, cursor: str | None = None, agent: str | None = None
    ) -> dict[str, Any]:
        """Async parity — see AgentBus.sent (#51)."""
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return await self._request("GET", "/v1/sent", params=params, agent=agent)

    async def create_webhook(
        self,
        url: str,
        events: Sequence[str] | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Create a webhook subscription — async twin."""
        result: dict[str, Any] = await self._request(
            "POST",
            "/v1/webhooks",
            json={"url": url, "events": list(events) if events else None, "agent": agent},
        )
        return result

    async def attachment(
        self, delivery_id: str, index: int = 0, *, agent: str | None = None
    ) -> bytes:
        """The RAW BYTES of one attachment on a delivery — async twin of AgentBus.attachment.

        Unseals on the way out with any private key this machine holds, so
        `sealed_by=sender` armor never leaks to the caller as-is.

        REG-9 (round-3 re-audit): stream + boundary cap, same rule as the sync
        twin. See AgentBus.attachment for the reasoning.
        """
        from .. import sealing

        cap = int(1.5 * _max_attachment_bytes())
        content: bytes
        try:
            async with self._client.stream(
                "GET",
                f"/v1/deliveries/{delivery_id}/attachments/{index}",
                headers=self._headers(agent, False),
            ) as response:
                _raise_for(response)
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                    total += len(chunk)
                    if total > cap:
                        raise AgentBusError(
                            f"attachment is over the {cap:,} byte cap "
                            f"({total:,} bytes received so far and still coming). "
                            "Raise AGENTBUS_MAX_ATTACHMENT_BYTES if this machine "
                            "has the RAM budget; the client holds the ciphertext "
                            "and the plaintext at the same time during unseal."
                        )
                    chunks.append(chunk)
                content = b"".join(chunks)
        except httpx.HTTPError as exc:
            raise TransportError(str(exc)) from exc

        if content[:64].lstrip().startswith(b"-----BEGIN AGE ENCRYPTED FILE-----"):
            if not sealing.load_private_keys(self.agent):
                raise AgentBusError(
                    "this attachment is sealed and this machine holds no sealing key"
                )
            try:
                return sealing.unseal_bytes_with_any(content, self.agent)
            except sealing.MalformedSealed as exc:
                raise AgentBusError(f"this attachment is damaged: {exc}") from exc
            except sealing.CannotDecrypt as exc:
                raise AgentBusError(
                    "this attachment was sealed to keys this machine does not hold"
                ) from exc
        return content
