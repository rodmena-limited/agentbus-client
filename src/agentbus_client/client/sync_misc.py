"""Typed sync and async clients for the AgentBus API."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from .._timefmt import _as_instant, _duration_seconds
from .errors import AgentBusError
from .sync_verify import SyncVerifyMixin


class SyncMiscMixin(SyncVerifyMixin):
        def room_history(
            self,
            room: str,
            *,
            limit: int | None = None,
            since: str | None = None,
            agent: str | None = None,
        ) -> dict[str, Any]:
            """What was said in a room BEFORE this agent joined (#170).

            A room is a conversation, and an agent that joins one mid-flight
            otherwise starts blind — it can see every future message and none of the
            context that makes them mean anything. Membership is the authorization,
            so this needs an acting agent: a workspace key with no agent is not
            "everyone" here, it is nobody.
            """
            target = agent or self.agent
            if not target:
                raise AgentBusError("which agent? pass agent= or bind the client")
            params: dict[str, Any] = {}
            if limit is not None:
                params["limit"] = limit
            if since is not None:
                params["since"] = since
            result: dict[str, Any] = self._request(
                "GET", f"/v1/rooms/{room}/history", params=params, agent=target
            )
            return result

        def room_schema(self, room: str, *, agent: str | None = None) -> dict[str, Any]:
            """The shape a room expects (#169).

            Readable by any member on purpose: a producer must be able to see what
            it is expected to send BEFORE being refused for getting it wrong. A
            contract you can only discover by violating it is not a contract.
            """
            result: dict[str, Any] = self._request("GET", f"/v1/rooms/{room}/schema", agent=agent)
            return result

        def set_room_schema(
            self, room: str, schema: dict[str, Any] | None, *, agent: str | None = None
        ) -> dict[str, Any]:
            """Declare or clear a room's payload contract (#169). `None` clears it.

            Membership is the rule — a room's schema is its contract, and a key that
            is not in the room must not reshape what everybody else has to send.
            Refused on encrypted workspaces: a server that cannot read a body cannot
            validate it.
            """
            target = agent or self.agent
            if not target:
                raise AgentBusError("which agent? pass agent= or bind the client")
            result: dict[str, Any] = self._request(
                "PUT", f"/v1/rooms/{room}/schema", json={"schema": schema}, agent=target
            )
            return result

        def join_room(self, room: str, *, agent: str | None = None) -> dict[str, Any]:
            """Join a room, so its broadcasts reach this agent."""
            target = agent or self.agent
            if not target:
                raise AgentBusError("which agent? pass agent= or bind the client")
            result: dict[str, Any] = self._request("POST", f"/v1/rooms/{room}/join", agent=target)
            return result

        def request_approval(
            self,
            title: str,
            *,
            kind: str = "generic",
            summary: str | None = None,
            proposed_action: dict[str, Any] | None = None,
            context: dict[str, Any] | None = None,
            thread_id: str | None = None,
            expires_in_minutes: int | None = None,
            agent: str | None = None,
            # SEV-2-D (#234): caller-supplied stable key for retry safety. Especially
            # important for approvals — a retried request_approval without a stable
            # key mints TWO approval rows to the human, who then wonders which is real.
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
            return self._request(
                "POST",
                "/v1/approvals",
                json=payload,
                agent=agent,
                idempotent=True,
                idempotency_key=idempotency_key,
            )

        def approval(self, approval_id: str, wait: int = 0) -> dict[str, Any]:
            params = {"wait": min(wait, 55)} if wait else {}
            return self._request(
                "GET",
                f"/v1/approvals/{approval_id}",
                params=params,
                timeout=max(self.timeout, wait + 10) if wait else self.timeout,
            )

        def remind(
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
            """Schedule a reminder into an agent's inbox. `target=None` reminds SELF.

            THE BODY IS SEALED HERE, BEFORE IT LEAVES THIS MACHINE, on an encrypted
            workspace — the same rule `create_draft` follows (#222). The server stores
            ciphertext it cannot read, and the scheduler behind it (RunFlow) only ever
            carries an opaque reminder id, never the text.

            That matters more for a reminder than for a message, because a reminder sits
            at rest until it is due. A plaintext body scheduled a week out is a week of
            exposure on a workspace whose whole purpose is that there is none — which is
            the defect class closed for MCP drafts on 2026-08-21.

            SEALED TO THE RECIPIENT, NOT TO SELF, when a target is named: the reminder is
            delivered to THEM and must be readable by THEM. `_seal_to_self` is only right
            for a self-note, where author and recipient are the same agent.

            The client knows nothing about the scheduler. It posts to its own backend and
            that is the whole of its world (operator ruling, 2026-08-21): one platform
            credential, held server-side, never on a user's machine.
            """
            body: dict[str, Any] = {"subject": subject, "text": text}
            if target:
                body["target"] = target
                body, _resolved = self._seal_if_needed(
                    body,
                    agent,
                    resolve_body={"to": [target], "subject": subject},
                )
            else:
                body = self._seal_to_self(body, agent)
            for key, value in (
                ("delay_seconds", _duration_seconds(delay)),
                ("due_at", _as_instant(at)),
                ("expire_seconds", _duration_seconds(expire)),
                ("repeat", repeat),
                ("repeat_until", _as_instant(repeat_until)),
                ("timezone", timezone),
            ):
                if value is not None:
                    body[key] = value
            return self._request(
                "POST",
                "/v1/reminders",
                json=body,
                agent=agent,
                idempotent=True,
                idempotency_key=idempotency_key,
            )

        def reminds(self, *, agent: str | None = None, all: bool = False) -> list[dict[str, Any]]:
            """Scheduled reminders — mine by default, everything I can see with all=True.

            NOT `reminders()`, which is the ack-tracking surface (#265) and answers a
            different question: that one chases messages already delivered, this one
            lists messages not yet sent. Two features, similar words, and conflating
            them would make both harder to reason about.
            """
            params = {"all": "true"} if all else None
            result: dict[str, Any] = self._request(
                "GET", "/v1/reminders", params=params, agent=agent
            )
            return result["reminders"]

        def cancel_remind(self, reminder_id: str, agent: str | None = None) -> dict[str, Any]:
            """Cancel a scheduled reminder before it fires."""
            return self._request("DELETE", f"/v1/reminders/{reminder_id}", agent=agent)

        def drafts(self, agent: str | None = None) -> list[dict[str, Any]]:
            return self._request("GET", "/v1/drafts", agent=agent)["drafts"]

        def create_draft(
            self,
            to: Sequence[str],
            subject: str = "",
            text: str | None = None,
            agent: str | None = None,
        ) -> dict[str, Any]:
            """Store a draft, sealed to YOUR OWN key on an encrypted workspace.

            #222, operator ruling 2026-08-16: an unsent draft used to sit in the
            drafts table as plaintext. Sealing it to the author's own key removes
            that without changing who can read it — the author is the only party who
            ever could, and is the only one who needs to.

            SEALED TO SELF HERE, TO RECIPIENTS AT SEND TIME. These are two different
            seals on purpose. A draft's recipients can be edited after it is written,
            so sealing to them now could seal to a set that is wrong by the time it
            goes out — delivered and unreadable by the agents it reached. Self here,
            recipients there.

            THE SERVER STILL ACCEPTS PLAINTEXT, deliberately. Refusing it would break
            every un-upgraded client the moment this shipped, which is exactly the
            failure #221 was: a rule enforced before any client could satisfy it.
            Publish first, tighten later - the same ordering `keys rotate` uses.
            """
            body = {"to": list(to), "subject": subject, "text": text}
            if text:
                body = self._seal_to_self(body, agent)
            return self._request("POST", "/v1/drafts", json=body, agent=agent)

        def send_draft(self, draft_id: str, agent: str | None = None) -> dict[str, Any]:
            """Send a stored draft, sealing it first when the workspace is encrypted.

            #221: THIS USED TO POST NOTHING, and on an encrypted workspace that meant
            a draft could never be sent at all — the stored body is plaintext, the
            server refuses plaintext, and there was nowhere to put a sealed one.
            Encryption is the default for new workspaces, so the whole draft feature
            was unusable by default.

            SEALED HERE, AT SEND TIME, rather than when the draft was written. A
            draft's recipients can be edited after creation, so a body sealed at
            creation could be sealed to the wrong set by the time it goes out —
            delivered and unreadable by the agents it reached, which is worse than a
            refusal. Sealing now means sealing to whoever it will ACTUALLY reach.

            The draft is fetched to learn its recipients and body. On an unencrypted
            workspace `_seal_if_needed` returns the payload untouched and this posts
            the same empty-bodied request it always did.
            """
            draft = self._request("GET", f"/v1/drafts/{draft_id}", agent=agent)
            recipients = draft.get("recipients") or draft.get("to") or []
            if isinstance(recipients, str):
                recipients = json.loads(recipients)

            # #222: THE STORED DRAFT MAY BE SEALED TO US. create_draft seals to the
            # author's own key on an encrypted workspace, so open it before sealing
            # it again to the recipients. A draft written by an older client is
            # plaintext and passes through untouched — `unseal_message` is a no-op on
            # a body that does not carry an age header.
            opened = self.unseal_message({"text_body": draft.get("text_body") or draft.get("text")})
            if opened.get("sealed_unreadable"):
                raise AgentBusError(
                    "cannot send this draft: its stored body is sealed to a key this "
                    f"machine does not hold ({opened['sealed_unreadable']}). Send it "
                    "from the machine that wrote it."
                )

            payload: dict[str, Any] = {
                "text": opened.get("text_body"),
                "subject": draft.get("subject"),
                "to": list(recipients),
            }
            payload, resolved = self._seal_if_needed(payload, agent)
            if not payload.get("sealed"):
                # Nothing to add: an unencrypted workspace (or a server that refused
                # the resolve) gets the request it has always received, and the
                # server uses its own stored body.
                plain: dict[str, Any] = self._request(
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
            sent: dict[str, Any] = self._request(
                "POST", f"/v1/drafts/{draft_id}/send", json=body, agent=agent, idempotent=True
            )
            return sent

        def usage(self) -> dict[str, Any]:
            return self._request("GET", "/v1/usage")

        def reminders_owing(self) -> list[dict[str, Any]]:
            """Messages I sent that are still awaiting ack (ack-tracking, SPECS/0022).

            The sender's view: what I'm waiting on. Backend endpoint shape
            (thread 01M097AQA9KVBTHFJZGSM1PN88):

              GET /v1/reminders/owing  -> {"owing": [ROW...], "count": int}
              ROW: {delivery_id, subject, required_by, attempts_so_far,
                    last_attempt_at, next_attempt_at, thread_id, recipient_name}

            Only UNRESOLVED rows are returned (acked/replied/expired drop off).
            Scoped to the caller's own agent; reads only.
            """
            data = self._request("GET", "/v1/reminders/owing")
            return list(data.get("owing") or [])

        def reminders_owed(self) -> list[dict[str, Any]]:
            """Messages TO me that I owe an ack on (SPECS/0022).

            The recipient's view: what I'm being reminded about. Endpoint:
              GET /v1/reminders/owed  -> {"owed": [ROW...], "count": int}
              ROW: {delivery_id, subject, required_by, attempts_so_far,
                    last_attempt_at, next_attempt_at, thread_id, sender_name}
            """
            data = self._request("GET", "/v1/reminders/owed")
            return list(data.get("owed") or [])

        def create_webhook(
            self, url: str, events: Sequence[str] | None = None, agent: str | None = None
        ) -> dict[str, Any]:
            return self._request(
                "POST",
                "/v1/webhooks",
                json={"url": url, "events": list(events) if events else None, "agent": agent},
            )

