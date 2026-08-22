"""Typed sync and async clients for the AgentBus API."""

from __future__ import annotations

import os
import sys
import threading as _threading
import uuid
from typing import Any

from .. import sealing
from .errors import AgentBusError, AuthError
from .models import _SEAL_INFLATION_FACTOR, _server_max_attachment_bytes

DEFAULT_BASE_URL = "https://agentbus.rodmena.co.uk"


# ------------------------------------------------------------------ clients


class _Base:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        agent: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        # `agent` is read before base_url/self.agent below because the on-disk
        # lookup is agent-scoped: the bound key lives at keys/<agent>.env.
        #
        # WHEN AGENT IS EXPLICITLY NAMED, PREFER THE DISK-BOUND KEY OVER $ENV.
        # Root-caused by agentbus-8dc08d (thread 01M08QS3M10M49WKT8WVX3P2P7):
        # a dependency (`resilient_circuit/storage.py`) calls `load_dotenv()`
        # at import time; find_dotenv walks UP from that module's file inside
        # .venv/lib/python3.13/site-packages/, so any parent directory's
        # `.env` containing `AGENTBUS_API_KEY=<some other key>` STOMPS
        # os.environ. If that stomped key is bound to a deleted workspace,
        # every downstream call sees WorkspaceDeleted — even though the
        # correct freshly-minted bound key is sitting on disk at
        # ~/.config/agentbus/keys/<agent>.env.
        #
        # When the caller NAMED an agent, they want THAT agent's credential.
        # Disk wins for that case. Env still wins in the unnamed-agent path
        # (the operator CLI: `agentbus signin`, `agentbus register`, etc.).
        # Every legitimate agent-named use case ships the bound key on disk.
        disk_key_for_named_agent = _key_from_disk(agent) if agent else ""
        self.api_key = (
            api_key
            or disk_key_for_named_agent
            or os.environ.get("AGENTBUS_API_KEY", "")
            or _key_from_disk(agent or os.environ.get("AGENTBUS_AGENT"))
        )
        if not self.api_key:
            # NAME THE KEY, WHERE TO GET IT, AND WHERE TO PUT IT.
            #
            # "set AGENTBUS_API_KEY" told an operator nothing he did not already
            # know. He had a key from the dashboard, it was the wrong SHAPE, and
            # nothing here or in the UI connected the two. The dashboard offers
            # read/send/full/admin and has never had an "operator" option, because
            # `operator` is not a key type at all — it is the FILENAME this client
            # reads. Nobody could have deduced that.
            resolved_agent = agent or os.environ.get("AGENTBUS_AGENT")
            if resolved_agent:
                # SEV-1-B: an agent-scoped construction NEVER borrows operator.env;
                # a missing bound key is a real "no credential for this identity"
                # error, not a reason to escalate to the workspace-wide key.
                raise AuthError(
                    f"no API key for agent '{resolved_agent}'. Looked for one in: the "
                    f"call itself, $AGENTBUS_API_KEY, and ~/.config/agentbus/keys/"
                    f"{resolved_agent}.env — all empty. This client REFUSES to fall "
                    "back to ~/.config/agentbus/operator.env when an agent is named, "
                    "because that would let a caller act as an arbitrary peer with "
                    "operator authority. Mint a bound key for this agent (dashboard: "
                    "Keys -> scope 'send', agents=[<name>]) and drop it in that "
                    "keys/ path, or construct AgentBus() without `agent=` to use the "
                    "operator credential explicitly."
                )
            raise AuthError(
                "no API key. Looked for one in: the call itself, $AGENTBUS_API_KEY, "
                "and ~/.config/agentbus/operator.env — all empty. Run `agentbus signin "
                "<api-key>` to create the operator credential. Registering a NEW "
                "agent needs a key with scope 'full' and NO agent binding — create "
                "one in the dashboard (Keys -> scope 'full', leave agents empty). "
                "NOTE: an agent-BOUND key cannot register a new agent, however high "
                "its scope — binding, not scope, is what blocks it."
            )
        self.base_url = (
            base_url or os.environ.get("AGENTBUS_BASE_URL") or DEFAULT_BASE_URL
        ).rstrip("/")
        self.agent = agent or os.environ.get("AGENTBUS_AGENT")
        self.timeout = timeout
        self._pending_challenge: str | None = None
        # SEV-2-B (#234): any caller that shares one AgentBus across a worker
        # pool (thread pool, or asyncio.to_thread from an AsyncAgentBus) races on
        # _pending_challenge — request A captures, request B echoes and clears
        # before A's next call, presence reads as reachable-not-responsive for
        # windows when it was in fact answering. threading.Lock protects the
        # read-then-clear on _headers and the write on _capture_challenge; the
        # critical section is a single dict update and holds the lock for microseconds.
        self._challenge_lock = _threading.Lock()

    def _headers(
        self,
        agent: str | None = None,
        idempotent: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        acting = agent or self.agent
        if acting:
            headers["X-AgentBus-Agent"] = acting
        if idempotent or idempotency_key:
            # SEV-2-D (#234): the caller may supply a STABLE key so a retry after
            # TransportError deduplicates on the server side. If omitted we mint
            # one per _request call — safe when the caller does not retry, and
            # documented so a caller that DOES retry knows to pass its own.
            headers["Idempotency-Key"] = idempotency_key or str(uuid.uuid4())
        # Answer any outstanding liveness challenge on the next call the caller
        # makes anyway. Handled here so an agent never has to implement the
        # protocol: being 'responsive' should be the default for anyone using
        # the SDK, not a feature you opt into and forget.
        # SEV-2-B (#234): read-and-clear under the lock so two threads sharing one
        # AgentBus can't both echo the same challenge (or lose it).
        with self._challenge_lock:
            if self._pending_challenge:
                headers["X-AgentBus-Pong"] = self._pending_challenge
                self._pending_challenge = None
        return headers

    def _capture_challenge(self, payload: Any) -> None:
        if isinstance(payload, dict):
            challenge = payload.get("liveness_challenge")
            if challenge:
                # SEV-2-B: overwrite under the lock. A more recent challenge
                # supersedes an older one, but the swap must be atomic w.r.t.
                # _headers's read-and-clear.
                with self._challenge_lock:
                    self._pending_challenge = challenge

    def _sign_if_possible(
        self,
        payload: dict[str, Any],
        agent: str | None,
        resolved: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Sign when this machine holds a signing key, otherwise send unsigned.

        SILENT WHEN THERE IS NO KEY, and that is the spec: signing is opt-in and
        unsigned mail stays first-class. An error here would make `agentbus
        keys sign` a prerequisite for sending, which is precisely the
        mandatory-signing bus #173 rejects.

        #220: ON `_Base`, so all four surfaces share it. It used to live on the
        sync client, and three of the four send paths — AgentBus.reply,
        AsyncAgentBus.send, AsyncAgentBus.reply — never signed at all. The
        FreeBSD agent found it by reading the installed client and diffing the
        two methods line by line.

        `resolved` IS THE SERVER'S ANSWER ABOUT WHAT IT WILL STORE, and it wins
        where it is present. A reply's recipients and "Re: " subject are derived
        server-side; the signature covers the stored values, so signing over the
        request payload would produce a signature that verifies as INVALID —
        which every read surface labels as tampering. An unsigned reply is
        merely unattested; a wrongly-signed one accuses its own sender.
        """
        from .. import _signing, sealing

        # #220: THIS AGENT's key, not the machine's. A shared signing key made
        # two agents on one box indistinguishable to the bus.
        private = sealing.load_signing_key(agent or self.agent)
        if private is None:
            return payload

        # agentbus-sig-v1 only covers the text body. If a message has HTML, attachments,
        # or a structured payload, signing it would create a signature that doesn't cover
        # the entire message contents (a cryptographic bypass risk).
        # We degrade to an unsigned message rather than failing.
        if payload.get("html") or payload.get("attachments") or payload.get("payload") is not None:
            # F9 (issuedb #3): write directly to STDERR, not via logging. The
            # SDK does not know whether the CLI caller is emitting --json to
            # stdout; the ambient logging config could route lastResort to
            # stdout and poison a machine-readable pipe. Explicit stderr is
            # the only source of truth that survives any caller setup.
            print(
                "agentbus: message downgraded to unsigned. agentbus-sig-v1 only "
                "covers plain text, but html, attachments, or a payload was present.",
                file=sys.stderr,
            )
            return payload

        signed = dict(payload)
        over = resolved or {}
        try:
            signed["signature"] = _signing.sign(
                private,
                _signing.canonical_bytes(
                    sender=agent or self.agent or "",
                    to=list(over.get("to") or payload.get("to") or []),
                    cc=list(over.get("cc") or payload.get("cc") or []),
                    subject=over.get("subject") or payload.get("subject"),
                    priority=payload.get("priority") or "normal",
                    body=payload.get("text"),
                ),
            )
            signed["signing_key_fingerprint"] = _signing.fingerprint(
                _signing.public_from_private(private)
            )
        except Exception:
            return payload
        return signed

    def _apply_seal(
        self, payload: dict[str, Any], resolved: dict[str, Any], agent: str | None = None
    ) -> dict[str, Any]:
        """Turn a resolve answer into a sealed payload. NO I/O, so both the sync
        and async clients share it.

        #220: this used to live inside the sync client's `_seal_if_needed`, which
        is why AsyncAgentBus sealed NOTHING — every async send on an encrypted
        workspace was refused by the server, and the async surface had no way to
        comply. One rule, two surfaces, implemented once: the same shape #155 was
        written about.
        """
        from .. import sealing

        if not resolved.get("encrypted"):
            return payload

        # EXTERNAL RECIPIENTS: hand it to the server unsealed and let it rule.
        #
        # #219 (operator, 2026-08-16): an encrypted workspace's bus carries no
        # mail to external addresses AT ALL, in either direction — a recipient
        # with no key would receive it in the clear, and a guarantee that ends
        # at its own boundary is not a guarantee. An earlier same-day decision
        # allowed plaintext egress; it was reversed, and this comment used to
        # still describe it.
        #
        # The client does not pre-refuse, because the rule has ONE owner. Two
        # copies of it drift, and the copy that drifts is the one nobody reads.
        if resolved.get("external"):
            return payload
        missing = resolved.get("missing_keys") or []
        if missing:
            raise AgentBusError(
                "cannot seal: these recipients have published no public key, so "
                "they could never read this message — "
                + ", ".join(missing)
                + ". They each need to run `agentbus signin` on their machine."
            )

        # AN UNBOUND CLIENT CANNOT SEAL AN ENCRYPTED WORKSPACE.
        #
        # B1 (reliability audit follow-up): this used to call
        # ensure_keypair(agent or self.agent) directly, which raised a raw,
        # untyped ValueError("no acting agent") when a client was constructed
        # with neither agent= nor AGENTBUS_AGENT. The sibling _sign_if_possible
        # degrades to unsigned on the same condition; the seal path cannot
        # degrade (sealing to nobody's key is a guarantee, not a choice), so it
        # must fail TYPED so an SDK caller catching AgentBusError sees it.
        acting = agent or self.agent
        if not acting:
            raise AgentBusError(
                "cannot seal: this workspace is encrypted but no acting agent is "
                "set. A sealing key belongs to ONE agent, so the client needs "
                "agent=... or AGENTBUS_AGENT to know whose key to seal to."
            )

        # EVERY key of every recipient, not one each. An agent may be on
        # several machines (AGENTBUS_DEVICE_ID is the documented way a
        # container fleet shares one identity), and sealing to only the first
        # would deliver a message half that agent's machines cannot read —
        # varying by which container happened to register last.
        keys: list[str] = []
        for entries in (resolved.get("keys") or {}).values():
            keys.extend(entry["public_key"] for entry in entries)
        _private, own_public = sealing.ensure_keypair(acting)
        if own_public not in keys:
            keys.append(own_public)
        if not keys:
            raise AgentBusError(
                "cannot seal: no public keys resolved for this send, so the "
                "message would be readable by nobody"
            )
        body = payload.get("text") or payload.get("html") or ""
        sealed = dict(payload)
        sealed["text"] = sealing.seal_for(body, keys)
        sealed["html"] = None
        sealed["sealed"] = True

        # ATTACHMENTS TOO. A sealed body beside a readable attachment is the
        # worst of both: it LOOKS encrypted, and the file — usually the part
        # actually worth protecting — is sitting in the clear. The spec said
        # body AND attachments from the start; only the body shipped first.
        #
        # Sealed as bytes, then re-base64'd, so the wire shape is unchanged and
        # the server stores an opaque blob exactly as it stores any other.
        # The filename is NOT sealed — it is metadata, like the subject, and
        # the same warning applies: do not put secrets in filenames.
        import base64 as _b64

        resealed = []
        for item in sealed.get("attachments") or []:
            raw = _b64.b64decode(item["content_base64"])
            # FAST PRE-SEAL REJECT (follow-up A). On an encrypted workspace
            # the wire bytes = base64(age_armor(raw)) which inflates at a
            # DETERMINISTIC ~1.806x (age armor base64, then base64-again for
            # JSON transport — double base64, 1.333^2). Sealing is CPU-heavy
            # (~1-2 MB/s), so the post-seal exact check can take 7-50s before
            # rejecting. Skip the seal entirely when the raw is CLEARLY over:
            # if raw * 1.806 is already past the cap, the sealed wire cannot
            # fit, so reject instantly. The post-seal exact check below stays
            # as the backstop for the borderline band.
            if int(len(raw) * _SEAL_INFLATION_FACTOR) > _server_max_attachment_bytes():
                raise AgentBusError(
                    f"attachment '{item.get('filename', '?')}' is {len(raw):,} bytes raw; "
                    f"on this ENCRYPTED workspace that inflates to roughly "
                    f"{int(len(raw) * _SEAL_INFLATION_FACTOR):,} wire bytes "
                    f"(age seal x{_SEAL_INFLATION_FACTOR:.3f}), already over the "
                    f"server's {_server_max_attachment_bytes():,}-byte cap. Split the "
                    "file or use a smaller attachment — the effective encrypted "
                    "limit is ~5.5 MiB raw, well under the documented 10 MiB."
                )
            armored = sealing.seal_for_bytes(raw, keys)
            wire = _b64.b64encode(armored).decode()
            # POST-SEAL EXACT CHECK (F7 follow-up). The server sees exactly
            # `wire`; verify it fits. This is the authoritative backstop — the
            # pre-seal estimate above is the fast reject, this is the exact one.
            if len(wire) > _server_max_attachment_bytes():
                raise AgentBusError(
                    f"attachment '{item.get('filename', '?')}' is {len(raw):,} bytes raw, "
                    f"but on this ENCRYPTED workspace sealing inflates it to "
                    f"{len(wire):,} wire bytes, which exceeds the server's "
                    f"{_server_max_attachment_bytes():,}-byte cap. Split the file or "
                    "use a smaller attachment — the effective encrypted limit is "
                    "well under the documented 10 MiB."
                )
            resealed.append(
                {
                    **item,
                    "content_base64": wire,
                    # The stored type is what it now IS, not what it was. A
                    # sniffing server that saw 'image/png' on age ciphertext
                    # would reject the mismatch at egress.
                    "content_type": "application/age",
                }
            )
        if resealed:
            sealed["attachments"] = resealed
        return sealed

    def unseal_message(self, message: dict[str, Any]) -> dict[str, Any]:
        """Decrypt a message body in place, or mark plainly why it could not be.

        A READER THAT CANNOT DECRYPT MUST SAY SO. Returning ciphertext as if it
        were content is how an agent concludes a peer sent gibberish; returning
        empty is how it concludes the peer sent nothing.

        ON `_Base` (review #23, issuedb #34): it does no I/O, and the async copy
        delegated to `AgentBus.unseal_message` — a name its module never imported —
        so every AsyncAgentBus.read()/thread() raised NameError.
        """
        from .. import sealing

        body = message.get("text_body") or message.get("text") or ""
        if not sealing.is_sealed(body):
            return message
        if not sealing.load_private_keys(self.agent):
            message["sealed_unreadable"] = "no sealing key on this machine"
            return message
        try:
            message["text_body"] = sealing.unseal_with_any(body, self.agent)
            message["sealed_opened"] = True
        except sealing.MalformedSealed as exc:
            message["sealed_unreadable"] = f"the sealed body is damaged: {exc}"
        except sealing.CannotDecrypt:
            message["sealed_unreadable"] = (
                "sealed to keys this machine does not hold — it was sent before "
                "this agent published a key, or to other recipients"
            )
        return message


def _key_from_disk(agent: str | None) -> str:
    """The credential this client already wrote, read back from where it put it.

    ORDER IS LEAST-PRIVILEGE FIRST: a NAMED agent gets ONLY its own bound key
    (keys/<agent>.env) and never falls through to the workspace-wide operator key
    (SEV-1-B, #234) — that fall-through let a script act as any peer with operator
    authority. An unnamed agent uses operator.env, the pre-binding acting mode.
    Filenames go through sealing.bound_env_filename (REG-8/8b) so a hostile name
    cannot traverse out of keys/.
    """
    config = os.path.join(os.path.expanduser("~"), ".config", "agentbus")

    def _read(path: str) -> str:
        try:
            with open(path, encoding="utf-8") as handle:
                for raw in handle:
                    entry = raw.strip()
                    if not entry or entry.startswith("#"):
                        continue
                    entry = entry.removeprefix("export ").strip()
                    name, sep, value = entry.partition("=")
                    if sep and name.strip() == "AGENTBUS_API_KEY":
                        value = value.strip().strip("'\"")
                        if value:
                            return value
        except OSError:
            return ""
        return ""

    if agent:
        return _read(os.path.join(config, "keys", sealing.bound_env_filename(agent)))
    return _read(os.path.join(config, "operator.env"))
