"""The agent's own notebook, sealed on this machine (#341, server-side).

WHY THIS IS ONE MODULE FOR BOTH CLIENTS. `every_send_path_not_one` is a standing
constraint here: this package has paid three times for a cross-cutting rule
implemented on only some of its four surfaces — #155, #189 and both halves of
219. Memory adds two more surfaces, so the rule that matters (open every entry
you can, and never present one you could not open as if you had) lives ONCE in
`_open_entries` and both mixins call it. The only thing the sync and async
halves hold separately is the awaiting.

THE SEALING IS NOT NEW CRYPTO. `_seal_to_self` / `_seal_to_self_async` already
exist for drafts and already ask the server whether the workspace is encrypted
rather than guessing; `unseal_with_any` already walks superseded keys. Memory
reuses all three, which is why there is no age code in this file.

A NOTE ON RESEAL, which is the one operation here that has no analogue in mail.
A message is retained thirty days; a memory entry is meant to outlive the
machine. After a key rotation every existing entry is still sealed to the OLD
key, and the client can only open it while that superseded key file is still on
this host. Move to a new box and the notebook is silently unreadable, with
nothing reporting it until somebody needs the note. `memory_reseal` rewrites
each entry IN PLACE — a PUT, never delete-and-add — so the seq an agent has
cited somewhere still means the same line afterwards.
"""

from __future__ import annotations

from typing import Any

from .. import sealing
from .errors import AgentBusError


def _open_entries(result: dict[str, Any], acting: str | None) -> dict[str, Any]:
    """Unseal what we can, and be explicit about what we could not.

    THREE OUTCOMES, NEVER TWO. An entry is opened, or it was never sealed, or it
    could not be opened — and the third must not be presented as either of the
    others. `verifier_negatives_must_be_earned` is the house rule this obeys:
    handing back ciphertext in the `text` field would read as content, and
    dropping the row would read as an empty notebook. Both are worse than
    saying so.

    `opened` is added to every entry so a caller can filter on a field that is
    always present, rather than inferring from whether `text` happens to look
    like armor.
    """
    entries = []
    for entry in result.get("memory", []) or []:
        item = dict(entry)
        if not item.get("sealed"):
            item["opened"] = True
            entries.append(item)
            continue
        try:
            item["text"] = sealing.unseal_with_any(item["text"], acting)
            item["opened"] = True
        except Exception as exc:
            # The text is left as the ciphertext it is, and flagged. A caller
            # that renders `text` blindly shows armor, which is ugly and
            # honest; a caller that checks `opened` shows the reason.
            item["opened"] = False
            item["open_error"] = (
                f"no key on this machine opens seq {item.get('seq')} ({type(exc).__name__}). "
                f"If you rotated keys on another host, the superseded private key is there, "
                f"not here."
            )
            entries.append(item)
            continue
        entries.append(item)
    out = dict(result)
    out["memory"] = entries
    unopened = [e["seq"] for e in entries if not e.get("opened")]
    if unopened:
        out["unopened_seqs"] = unopened
    return out


def _reseal_plan(opened: dict[str, Any]) -> tuple[list[dict[str, Any]], list[int]]:
    """(entries to rewrite, seqs that cannot be rewritten).

    AN ENTRY THAT WOULD NOT OPEN IS NEVER REWRITTEN. Sealing its ciphertext to a
    new key would produce a doubly-wrapped body nobody can ever read, turning a
    recoverable problem — find the old key file — into a permanent one. This is
    the delete-the-evidence failure, and reseal is exactly where it would
    happen.
    """
    todo = [e for e in opened.get("memory", []) if e.get("sealed") and e.get("opened")]
    unrecoverable = [e["seq"] for e in opened.get("memory", []) if not e.get("opened")]
    return todo, unrecoverable


class SyncMemoryMixin:
    """`AgentBus.memory_*` — the notebook, opened on this machine."""

    def memory_fetch(self, agent: str | None = None) -> dict[str, Any]:
        """Every entry, oldest first, unsealed where this machine holds the key."""
        result = self._request("GET", "/v1/memory", agent=agent)  # type: ignore[attr-defined]
        return _open_entries(result, agent or self.agent)  # type: ignore[attr-defined]

    def memory_add(
        self,
        text: str,
        agent: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Append one entry, sealed to your own key on an encrypted workspace.

        The idempotency key is worth passing on a retry: the same text appended
        twice is two entries with two seqs, so a retried timeout silently
        doubles a note.
        """
        payload = self._seal_to_self({"text": text}, agent)  # type: ignore[attr-defined]
        # `idempotent=True`, NOT a hand-rolled Idempotency-Key header. _request
        # mints the key ONCE, outside the resilience layer's retry loop (REG-7);
        # a header set here would be re-sent correctly but a caller passing none
        # would get a fresh UUID per retry, which is two distinct writes to the
        # server and the exact retry-safety hole that guard closed.
        return self._request(  # type: ignore[attr-defined]
            "POST",
            "/v1/memory",
            json=payload,
            agent=agent,
            idempotent=True,
            idempotency_key=idempotency_key,
        )

    def memory_delete(self, seq: int, agent: str | None = None) -> dict[str, Any]:
        """Remove one entry by its stable seq. Survivors keep their numbers."""
        return self._request("DELETE", f"/v1/memory/{seq}", agent=agent)  # type: ignore[attr-defined]

    def memory_truncate(
        self,
        first: int | None = None,
        seqs: list[int] | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Remove the N OLDEST entries, or a named set. `first` is position."""
        body: dict[str, Any] = {"first": first} if first is not None else {"seqs": seqs}
        return self._request("POST", "/v1/memory/truncate", json=body, agent=agent)  # type: ignore[attr-defined]

    def memory_reseal(self, agent: str | None = None) -> dict[str, Any]:
        """Re-seal every entry to this agent's CURRENT key, keeping every seq."""
        acting = agent or self.agent  # type: ignore[attr-defined]
        opened = self.memory_fetch(agent=agent)
        todo, unrecoverable = _reseal_plan(opened)
        if not todo and not unrecoverable:
            return {"resealed": [], "unrecoverable": [], "detail": "nothing sealed to re-seal"}
        if not acting:
            raise AgentBusError(
                "cannot reseal: a sealing key belongs to ONE agent, so the client "
                "needs agent=... or AGENTBUS_AGENT to know whose key to seal to."
            )
        _private, own_public = sealing.ensure_keypair(acting)
        resealed = []
        for entry in todo:
            self._request(  # type: ignore[attr-defined]
                "PUT",
                f"/v1/memory/{entry['seq']}",
                json={"text": sealing.seal_for(entry["text"], [own_public]), "sealed": True},
                agent=agent,
            )
            resealed.append(entry["seq"])
        return {"resealed": resealed, "unrecoverable": unrecoverable}


class AsyncMemoryMixin:
    """`AsyncAgentBus.memory_*` — the async twin, rule for rule."""

    async def memory_fetch(self, agent: str | None = None) -> dict[str, Any]:
        result = await self._request("GET", "/v1/memory", agent=agent)  # type: ignore[attr-defined]
        return _open_entries(result, agent or self.agent)  # type: ignore[attr-defined]

    async def memory_add(
        self,
        text: str,
        agent: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = await self._seal_to_self_async({"text": text}, agent)  # type: ignore[attr-defined]
        return await self._request(  # type: ignore[attr-defined]
            "POST",
            "/v1/memory",
            json=payload,
            agent=agent,
            idempotent=True,
            idempotency_key=idempotency_key,
        )

    async def memory_delete(self, seq: int, agent: str | None = None) -> dict[str, Any]:
        return await self._request("DELETE", f"/v1/memory/{seq}", agent=agent)  # type: ignore[attr-defined]

    async def memory_truncate(
        self,
        first: int | None = None,
        seqs: list[int] | None = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"first": first} if first is not None else {"seqs": seqs}
        return await self._request("POST", "/v1/memory/truncate", json=body, agent=agent)  # type: ignore[attr-defined]

    async def memory_reseal(self, agent: str | None = None) -> dict[str, Any]:
        acting = agent or self.agent  # type: ignore[attr-defined]
        opened = await self.memory_fetch(agent=agent)
        todo, unrecoverable = _reseal_plan(opened)
        if not todo and not unrecoverable:
            return {"resealed": [], "unrecoverable": [], "detail": "nothing sealed to re-seal"}
        if not acting:
            raise AgentBusError(
                "cannot reseal: a sealing key belongs to ONE agent, so the client "
                "needs agent=... or AGENTBUS_AGENT to know whose key to seal to."
            )
        _private, own_public = sealing.ensure_keypair(acting)
        resealed = []
        for entry in todo:
            await self._request(  # type: ignore[attr-defined]
                "PUT",
                f"/v1/memory/{entry['seq']}",
                json={"text": sealing.seal_for(entry["text"], [own_public]), "sealed": True},
                agent=agent,
            )
            resealed.append(entry["seq"])
        return {"resealed": resealed, "unrecoverable": unrecoverable}
