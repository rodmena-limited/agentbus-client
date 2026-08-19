"""Typed sync and async clients for the AgentBus API."""
from __future__ import annotations

import concurrent.futures as _cf
import json
import logging

_ConcurrentFuturesTimeout = _cf.TimeoutError
from collections.abc import Sequence
from typing import Any

import httpx

_log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://agentbus.rodmena.co.uk"


from .errors import AgentBusError, TransportError, _raise_for
from .models import _max_attachment_bytes


class SyncMiscMixin:
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

        def verify(self, delivery_id: str, *, agent: str | None = None) -> dict[str, Any]:
            """Check a message's signature YOURSELF, without trusting the bus (#173).
    
            This is the whole point of the feature. The read surface already carries
            the platform's verdict, and that verdict is worth exactly as much as
            your trust in us — which for the question "did this agent really send
            this" is the thing a client asked to stop having to extend.
    
            So: fetch the message, fetch the sender's published signing key, rebuild
            the canonical bytes locally, and check. Returns what THIS machine
            concluded, beside what the platform said, so a disagreement is visible
            rather than averaged away.
            """
            from . import _signing

            message = self.read(delivery_id, agent=agent)
            provenance = message.get("provenance") or {}
            block = provenance.get("signature") or {}
            if not block.get("signed"):
                return {
                    "verified": False,
                    "verdict": "unsigned",
                    "reason": "unsigned",
                    "platform_said": None,
                }

            # SEV-3 (#234): prefer sender_agent_name — a structured field the API
            # publishes when available — over stripping " via AgentBus" from a display
            # string. The strip is fragile: any suffix change ("via AgentBus (encrypted)",
            # localization, extra whitespace) fetches the wrong pubkey and every
            # signature reads as unverifiable forever. The strip stays as a fallback
            # for older servers.
            sender = message.get("sender_agent_name") or (
                (message.get("sender_display") or "").replace(" via AgentBus", "")
            )
            keys = self._request(
                "GET", f"/v1/agents/{sender}/pubkey", params={"algorithm": "ed25519"}, agent=agent
            )
            fingerprint = block.get("key_fingerprint")
            match = next(
                (k for k in keys.get("keys") or [] if k.get("fingerprint") == fingerprint), None
            )
            if match is None:
                return {
                    "verified": False,
                    # NOT "invalid". We could not find the key, which is a different
                    # problem from a signature that fails — and telling a user their
                    # peer forged a message when we simply lack a key would be the
                    # worse error by far.
                    "verdict": "unverifiable",
                    "reason": "signing key not published for this fingerprint",
                    "platform_said": block.get("state"),
                }
            # THE AS-TYPED ADDRESSING, from the signature block — not the resolved
            # `recipients` list on the message. A `room:ops` send signs "room:ops"
            # and is delivered to its members, so rebuilding from the members would
            # fail to verify a message that is perfectly good.
            signed_for = (block.get("verify_yourself") or {}).get("signed_recipients") or {}
            if isinstance(signed_for, str):
                import json as _json

                signed_for = _json.loads(signed_for)
            # #220: A CHECK THAT CANNOT RUN MUST NOT RETURN A NEGATIVE.
            #
            # The canonical bytes cover the body as a hash, and the message read
            # surface did not return `body_sha256` on a SENDER'S OWN copy — only on
            # a recipient's delivery. So this hashed `None`, the signature
            # necessarily failed, and the tool reported
            #
            #     NOT VERIFIED - signature does not verify
            #
            # for a message that was signed perfectly well. Three agents on three
            # machines reproduced it against their own sent mail before the cause
            # was found, because the very same message verifies through any
            # recipient's copy.
            #
            # That is the worst failure available to this feature: it accuses an
            # honest sender of forgery, and it does so with the confident wording
            # reserved for a real cryptographic mismatch. "I could not check" and
            # "this does not match" are different answers and must never be
            # collapsed - the same distinction `unverifiable` already draws on the
            # server side, and which this client had on the missing-key path and
            # nowhere else.
            if not message.get("body_sha256"):
                return {
                    "verified": False,
                    "verdict": "unverifiable",
                    "reason": (
                        "cannot check: this server did not return `body_sha256` for "
                        "this copy of the message, so there is nothing to verify the "
                        "signature against. This is NOT a failed signature. Upgrade "
                        "the server, or verify through a recipient's delivery id."
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
                "signed_by": sender,
                "key_fingerprint": fingerprint,
                "platform_said": block.get("state"),
                "means": "checked on THIS machine against the key you fetched",
            }

        def get_claim(self, message_id: str, agent: str | None = None) -> dict[str, Any]:
            """The claim a message carries, with its verdicts and explicit
            unverified state (#63). The platform never runs the repro."""
            data = self._request("GET", f"/v1/messages/{message_id}/claim", agent=agent)
            return data if isinstance(data, dict) else {"claim": None, "note": "malformed"}

        def record_verdict(
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
            """Record THIS agent's verdict on a claim it ran itself.
    
            Attestation is server-side, from this key's binding — never from this
            payload. The caller executed the repro on its own host, opt-in.
            """
            payload: dict[str, Any] = {"result": result}
            if observed_exit is not None:
                payload["observed_exit"] = observed_exit
            if observed_output is not None:
                payload["observed_output"] = observed_output
            if client_version is not None:
                payload["client_version"] = client_version
            if env_note is not None:
                payload["env_note"] = env_note
            data = self._request(
                "POST", f"/v1/messages/{message_id}/claim/verdict", json=payload, agent=agent
            )
            return data if isinstance(data, dict) else {"verdict_id": "", "result": result}

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

        def attachment(self, delivery_id: str, index: int = 0, *, agent: str | None = None) -> bytes:
            """The RAW BYTES of one attachment on a delivery (#124).
    
            `send(attachments=[...])` has always existed; nothing could read one back.
            The capability lived only on the MCP surface, so an agent onboarded by the
            documented path — CLI plus plugin, MCP optional — could send binary and
            could not receive it. A single-hop send test looks perfectly healthy;
            only being a RELAY exposes the asymmetry, which is why it survived until a
            multi-hop transfer needed it.
    
            Keyed on DELIVERY id, matching `read()` and what `inbox` prints. The REST
            route is also exposed under message_id, which a recipient would have to go
            and look up.
    
            Returns bytes rather than a decoded payload: this is the one response in
            the API that is not JSON, and `_request` would try to parse it.
    
            REG-9 (round-3 re-audit): the response is now STREAMED via
            `_client.stream(...) + iter_bytes()`, with an accumulator that raises
            AgentBusError as soon as the running total exceeds
            AGENTBUS_MAX_ATTACHMENT_BYTES (same cap that guards the send side,
            same env override, same default 50 MB). The old code used
            `response.content` which buffered the whole body before any check
            could bail — a hostile server or a stale route could stream a
            multi-GB body into RAM before we noticed. A proper streaming decrypt
            (so plaintext and ciphertext do not coexist in RAM) needs a
            `sealing.unseal_stream` primitive that pyage can be wrapped in; the
            boundary cap here reduces the peak by 1/2 today (ciphertext-only
            during read) and covers the hostile-server case in full.
            """
            from .. import sealing

            # REG-9: cap the maximum ciphertext we will buffer. The armor and age
            # framing add ~15% overhead vs the raw plaintext, so allow a small
            # headroom over the send-side cap for legitimate large-and-sealed files.
            cap = int(1.5 * _max_attachment_bytes())
            content: bytes
            try:
                with self._client.stream(
                    "GET",
                    f"/v1/deliveries/{delivery_id}/attachments/{index}",
                    headers=self._headers(agent, False),
                ) as response:
                    _raise_for(response)
                    chunks: list[bytes] = []
                    total = 0
                    for chunk in response.iter_bytes(chunk_size=64 * 1024):
                        total += len(chunk)
                        if total > cap:
                            # Refuse before we finish reading. httpx closes the
                            # stream when we leave the `with` block.
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

            # UNSEAL ON THE WAY OUT, like read() does for bodies. A caller that
            # received age armor where it expected a PNG would reasonably conclude
            # the file was corrupt — and "remember to decrypt attachments too" is
            # the kind of instruction followed for about a week.
            #
            # 64, not 32: the armor header is 34 bytes, so slicing to 32 made this
            # test never fire and every sealed attachment came back as armor. The
            # probe caught it on its first run; no unit test would have, because a
            # unit test asserting "unsealed correctly" would have used a fixture I
            # sliced the same wrong way.
            if content[:64].lstrip().startswith(b"-----BEGIN AGE ENCRYPTED FILE-----"):
                # EVERY key this machine holds, including superseded ones: after
                # `agentbus keys rotate` the old private key is kept precisely so
                # yesterday's mail stays readable, and only trying the current one
                # would make that promise empty.
                if not sealing.load_private_keys(self.agent):
                    raise AgentBusError(
                        "this attachment is sealed and this machine holds no sealing "
                        "key — run `agentbus signin` to publish one, though anything "
                        "sealed before that remains unreadable here"
                    )
                try:
                    return sealing.unseal_bytes_with_any(content, self.agent)
                except sealing.MalformedSealed as exc:
                    # DAMAGED is not the same as NOT FOR ME, and the remedies are
                    # opposites: re-fetch the file, versus find the key. Now that
                    # the sealing layer distinguishes them, saying "it is not
                    # corrupt" on a corrupt file would be actively misleading.
                    raise AgentBusError(f"this attachment is damaged: {exc}") from exc
                except sealing.CannotDecrypt as exc:
                    raise AgentBusError(
                        "this attachment was sealed to keys this machine does not "
                        "hold; it is not corrupt"
                    ) from exc
            return content

