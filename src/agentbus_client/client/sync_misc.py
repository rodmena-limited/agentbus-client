"""Typed sync and async clients for the AgentBus API."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from .._timefmt import _as_instant, _duration_seconds, _expiry_instant
from .errors import AgentBusError
from .sync_verify import SyncVerifyMixin

if TYPE_CHECKING:  # #48: tell mypy what the assembled client provides
    from ._mixin_base import SyncClientBase as _MixinBase
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


class SyncMiscMixin(SyncVerifyMixin, _MixinBase):
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
        # UNSEAL, EXACTLY AS `thread()` DOES. On an encrypted workspace the
        # server holds ciphertext by design, so without this a caller got raw
        # age armor back from `history --json` while every other read path
        # rendered prose. Silent: the call SUCCEEDS and the body is unusable,
        # which is worse than an error. Reported by
        # bikeroom-freebsd-operato-b124c2.
        for msg in result.get("messages") or []:
            self.unseal_message(msg)
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
            body, _resolved = self._seal_if_needed(
                body,
                agent,
                resolve_body={"to": [target], "subject": subject},
            )
        else:
            body = self._seal_to_self(body, agent)
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
        # #336 — `all=true` WAS NOT A PARAMETER THIS API HAS.
        #
        # The server's query parameter is `state` (scheduled | all). `all=true`
        # was silently ignored, so EVERY call asked for every state and got the
        # newest 50 by created_at, and the CLI then filtered live rows locally —
        # i.e. the truncation happened BEFORE the state filter. An old but live
        # recurring reminder was crowded out by newer FINISHED one-shots and
        # simply disappeared. A customer reported five as vanished; every row was
        # still in the database.
        #
        # Now the filter is server-side, so the limit applies to the rows the
        # caller actually asked for. `limit=200` is the API maximum: the listing
        # has no cursor, so asking for the most it will give is the honest
        # maximum this client can offer, and `reminds_page()` exposes the
        # truncation flags for a caller that needs to know.
        return self.reminds_page(agent=agent, all=all)["reminders"]

    def reminds_page(self, *, agent: str | None = None, all: bool = False) -> dict[str, Any]:
        """The whole listing envelope: reminders, count, total, has_more, limit.

        SEPARATE FROM `reminds()` BECAUSE A LIST CANNOT CARRY A TRUNCATION FLAG,
        and that is the bug (#336): the response looked complete whether it was
        or not. A caller that only wants the rows keeps `reminds()`; a caller
        that must not silently under-report — the CLI — uses this.

        `total` is the count under the SAME state filter, so `has_more` answers
        "is this page all of what I asked for", not "does anything else exist".
        """
        params = {"state": "all" if all else "scheduled", "limit": "200"}
        result: dict[str, Any] = self._request("GET", "/v1/reminders", params=params, agent=agent)
        return result

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

    def sent(
        self, *, limit: int = 50, cursor: str | None = None, agent: str | None = None
    ) -> dict[str, Any]:
        """One page of mail the acting agent SENT, newest first (#51).

        `GET /v1/sent` — the outbox. The server has answered it for some time
        (llms.txt: "did my message actually land, and what did it contain");
        no client surface exposed it, so a platform that had auto-posted ~60
        alerts reconstructed its own outbound history by grepping daemon logs.

        Returns the page unchanged: `{"messages": [ROW...], "count": int,
        "cursor": str | None}`. ROW carries message_id, thread_id, subject,
        sent_at, sealed, sealed_by, signed_recipients (a JSON string, present
        only when the send was signed) and text_body (ciphertext on an
        encrypted workspace — `unseal_message` opens your own). Nothing here
        is unsealed; the CLI does that where it renders bodies.
        """
        params: dict[str, Any] = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", "/v1/sent", params=params, agent=agent)

    def create_webhook(
        self, url: str, events: Sequence[str] | None = None, agent: str | None = None
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/v1/webhooks",
            json={"url": url, "events": list(events) if events else None, "agent": agent},
        )
