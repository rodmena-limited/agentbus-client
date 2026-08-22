"""Typed sync and async clients for the AgentBus API."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # #48: tell mypy what the assembled client provides
    from ._mixin_base import AsyncClientBase as _MixinBase
else:  # runtime: no new base, no MRO change, no import cycle
    _MixinBase = object


class AsyncDirectoryMixin(_MixinBase):
    async def register(
        self,
        name: str | None = None,
        *,
        role: str | None = None,
        repo_remote: str | None = None,
        workdir: str | None = None,
        capabilities: Sequence[str] | None = None,
        labels: dict[str, str] | None = None,
        unlisted: bool = False,
        ephemeral: bool | None = None,
        persona: str | None = None,
    ) -> dict[str, Any]:
        """Async mirror of AgentBus.register. Prefer `role` over `name`."""
        from .. import identity

        payload: dict[str, Any] = {
            "name": name,
            "capabilities": list(capabilities or []),
            # NOT `labels or {}`. Coercing None to {} erases the distinction
            # the SERVER relies on: it uses model_fields_set to tell "the
            # caller sent {}" (asked to clear -> tell them labels merge)
            # from "the caller never mentioned labels" (asked for nothing ->
            # stay silent). Sending {} unconditionally made every register
            # look like a clear request, so the backend's F11 advisory fired
            # on plain re-registers — noise on the commonest call, which is
            # how an advisory stops being read before it reaches the one
            # caller who needed it. Caught by a negative control, not review.
            **({"labels": labels} if labels is not None else {}),
            "unlisted": unlisted,
        }
        if persona:
            payload["persona"] = persona
        if role:
            env = identity.describe(workdir)
            payload.update(
                role=role,
                device_id=env["device_id"],
                workdir=env["workdir"],
                repo_remote=repo_remote or env["repo_remote"],
                ephemeral=env["ephemeral"] if ephemeral is None else ephemeral,
            )
        else:
            payload.update(repo_remote=repo_remote, ephemeral=bool(ephemeral))
        result = await self._request("POST", "/v1/agents/register", json=payload)
        self.agent = result["agent"]["name"]
        return result

    async def whoami(self, agent: str | None = None) -> dict[str, Any]:
        return await self._request("GET", "/v1/whoami", agent=agent)

    async def health(self, target_agent: str, *, agent: str | None = None) -> dict[str, Any]:
        """Async parity — see AgentBus.health docstring."""
        return await self._request("GET", f"/v1/agents/{target_agent}/health", agent=agent)

    async def mint_key(
        self,
        *,
        scope: str = "send",
        agents: Sequence[str] | None = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        """Mint a key no stronger than this one — async twin."""
        result: dict[str, Any] = await self._request(
            "POST",
            "/v1/keys",
            json={
                "scope": scope,
                "agents": list(agents) if agents else None,
                "label": label,
            },
        )
        return result

    async def create_invite(
        self, *, role: str | None = None, ttl_seconds: int = 3600
    ) -> dict[str, Any]:
        """Mint a one-time join token so a NEW agent can register itself — async twin."""
        result: dict[str, Any] = await self._request(
            "POST",
            "/v1/invites",
            json={"role": role, "ttl_seconds": ttl_seconds},
        )
        return result

    async def phonebook(
        self,
        query: str | None = None,
        *,
        capability: str | None = None,
        repo_fingerprint: str | None = None,
        label: str | Sequence[str] | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            k: v
            for k, v in {
                "q": query,
                "capability": capability,
                "repo_fingerprint": repo_fingerprint,
            }.items()
            if v
        }
        if label:
            # #149: keep the async copy at parity — this file has drifted before.
            params["label"] = [label] if isinstance(label, str) else list(label)
        result = await self._request("GET", "/v1/agents", params=params)
        return result["agents"]

    async def tag(
        self,
        set: dict[str, str] | None = None,
        remove: Sequence[str] | None = None,
        *,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Async mirror of AgentBus.tag() (#149)."""
        target = agent or self.agent
        if not target:
            raise ValueError("tag() needs an acting agent: pass agent= or construct with one")
        result: dict[str, Any] = await self._request(
            "PATCH",
            f"/v1/agents/{target}/labels",
            json={"set": set or {}, "remove": list(remove or [])},
            agent=target,
        )
        return result

    async def heartbeat(self, agent: str | None = None) -> None:
        """Refresh presence for the acting agent — async twin.

        REG-4 (round-3 audit): posts to /v1/agents/<agent>/heartbeat, matching
        the sync twin at AgentBus.heartbeat (client.py). This method previously
        posted to /v1/heartbeat, which the server 404s — presence went silently
        stale for anyone using the async client. The parity test now compares
        endpoint strings so this drift class fails CI.
        """
        await self._request("POST", f"/v1/agents/{agent or self.agent}/heartbeat", agent=agent)

    async def retire(self, agent: str | None = None) -> None:
        """Stand this agent down (reversible) — async twin."""
        target = agent or self.agent
        if not target:
            raise ValueError("retire() needs an acting agent: pass agent= or construct with one")
        await self._request("POST", f"/v1/agents/{target}/retire", agent=target)

    async def heartbeat_liveness(self, agent: str | None = None) -> dict[str, Any]:
        """Cheap poll to answer a challenge and learn what is waiting — async twin."""
        result = await self._request("GET", "/v1/inbox?cursor=0&limit=1", agent=agent)
        # REG-5 (round-3 audit): the lock is the invariant, all readers honour
        # it. `is not None` is a diagnostic bool so the impact of reading unlocked
        # is small, but keeping every reader under the lock keeps the discipline
        # honest — a future reader who copies this shape gets the safe pattern.
        with self._challenge_lock:
            answered = self._pending_challenge is not None
        return {
            "waiting": result.get("count", 0),
            "answered_challenge": answered,
        }
