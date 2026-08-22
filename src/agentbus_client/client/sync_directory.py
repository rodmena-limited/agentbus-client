"""Typed sync and async clients for the AgentBus API."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # #48: tell mypy what the assembled client provides
    from ._mixin_base import SyncClientBase as _MixinBase
else:  # runtime: no new base, no MRO change, no import cycle
    _MixinBase = object


class SyncDirectoryMixin(_MixinBase):
    def register(
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
        """Register (idempotently) and remember the name for later calls.

        PREFER `role` over `name`. With a role, identity is DERIVED from this
        machine, this checkout and this directory, so reopening the session
        recomputes the same agent — same address, same inbox, same cursor —
        with nothing to remember. A name has to be recalled correctly every
        time, and when it is not, a brand new identity is minted silently.

            bus.register(role="api-refactor")

        Everything else is discovered locally: the device id, the git remote,
        the working directory, and whether this is a throwaway CI environment.

        ``persona`` declares this agent's RESPONSIBILITY LANE (legal,
        frontend, backend, ...). POLICY enforcement lives on the server: an
        admin assigns the lane, and an agent cannot lie about which lane it
        holds. The server validates against the workspace vocabulary and
        refuses unknown values. A server that does not yet have the persona
        column silently ignores this field (forward-compatible).
        """
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
        result: dict[str, Any] = self._request("POST", "/v1/agents/register", json=payload)
        self.agent = result["agent"]["name"]
        return result

    def whoami(self, agent: str | None = None) -> dict[str, Any]:
        result: dict[str, Any] = self._request("GET", "/v1/whoami", agent=agent)
        return result

    def health(self, target_agent: str, *, agent: str | None = None) -> dict[str, Any]:
        """Query the canary heartbeat endpoint for a target agent (0.9.26).

        Distinguishes "watcher alive" from "agent alive". `presence` on the
        phonebook is age-based (last_pong within N seconds); `wake_channel_state`
        from this endpoint is subscriber-existence-plus-activity. They can and
        do disagree — a subscribed process outliving its parent will pong for a
        while but wake_channel goes stale. Whichever answers "yes" is your
        actual reachability guarantee; both saying "yes" is what you want
        before assuming a peer will act on a send.

        Returns the endpoint body verbatim:
          {
            "agent": ...,
            "wake_channel_state": "live" | "stale" | "webhook" | "none",
            "subscriber_count": <int>,
            "last_seen_at": <iso8601 or null>,
            "last_pong_at": <iso8601 or null>,
            "last_stream_attached_at": <iso8601 or null>,
            "last_stream_detached_at": <iso8601 or null>,
            "keepalive_age_seconds": <int or null>,
            "watcher_alive": <bool>,
            "capabilities": {"supports_canary_heartbeat": true}
          }

        Rules (agreed contract with backend, thread 01M08ZABM8B3N2VB1TV7R7J2ED):
          * scope=read is enough to read one's OWN agent's health
          * scope>=send is enough for arbitrary agents in the workspace
          * unknown agent name in the caller's workspace returns 404 (never
            200 for a name that doesn't exist)
          * transient redis/db blip -> the affected field is null, response
            stays 200 (never 5xx — advisory semantics preserved)
        """
        result: dict[str, Any] = self._request(
            "GET", f"/v1/agents/{target_agent}/health", agent=agent
        )
        return result

    def mint_key(
        self, *, scope: str = "send", agents: Sequence[str] | None = None, label: str | None = None
    ) -> dict[str, Any]:
        """Mint a key no stronger than this one. Minting DE-ESCALATES: `send`
        keys must be bound, and this is how `agentbus setup` gives each
        project's agent its own credential without a human ever seeing it."""
        result: dict[str, Any] = self._request(
            "POST",
            "/v1/keys",
            json={
                "scope": scope,
                "agents": list(agents) if agents else None,
                "label": label,
            },
        )
        return result

    def create_invite(self, *, role: str | None = None, ttl_seconds: int = 3600) -> dict[str, Any]:
        """Mint a ONE-TIME join token so a NEW agent can register itself.

        The counterpart to `mint_key`, and the one to reach for when the agent
        does not exist YET. `mint_key` produces a credential that keeps working;
        this produces a token that creates exactly one agent and then stops
        existing. Handing a `full` workspace key to a machine so it can create
        one agent gives that machine the ability to read every inbox in the
        workspace and mint more keys — a join token gives it neither.

        Returns {token, expires_in_seconds, role}. The token is shown once and
        is stored only as a Redis entry under its TTL, so a lost token is
        re-minted, never recovered.
        """
        # Annotated rather than returned straight through: `_request` is typed
        # Any, so a bare `return` here adds a no-any-return and the ratchet is
        # meant to fall, not rise. New code pays its own way.
        invite: dict[str, Any] = self._request(
            "POST",
            "/v1/invites",
            json={"role": role, "ttl_seconds": ttl_seconds},
        )
        return invite

    def phonebook(
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
            # #149: repeatable, ANDed server-side. httpx encodes a list value
            # as repeated query params.
            params["label"] = [label] if isinstance(label, str) else list(label)
        result: dict[str, Any] = self._request("GET", "/v1/agents", params=params)
        return result["agents"]

    def tag(
        self,
        set: dict[str, str] | None = None,
        remove: Sequence[str] | None = None,
        *,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Merge-mutate an agent's tags (#149): `set` upserts, `remove` deletes.

        Tags survive re-registration; peers find them via phonebook(label=...).
        Discovery metadata only — a tag never grants permissions. Not the
        delivery mail-filing labels (that is `label()`).
        """
        target = agent or self.agent
        if not target:
            raise ValueError("tag() needs an acting agent: pass agent= or construct with one")
        result: dict[str, Any] = self._request(
            "PATCH",
            f"/v1/agents/{target}/labels",
            json={"set": set or {}, "remove": list(remove or [])},
            agent=target,
        )
        return result

    def heartbeat(self, agent: str | None = None) -> None:
        self._request("POST", f"/v1/agents/{agent or self.agent}/heartbeat")

    def retire(self, agent: str | None = None) -> None:
        self._request("POST", f"/v1/agents/{agent or self.agent}/retire")

    def heartbeat_liveness(self, agent: str | None = None) -> dict[str, Any]:
        """Poll once purely to answer a challenge and learn what is waiting.

        Cheap: one request, no message, no quota. Call it on a timer if your
        session is long-lived and quiet, so you read as `responsive` rather than
        merely `reachable`.
        """
        result = self._request("GET", "/v1/inbox?cursor=0&limit=1", agent=agent)
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
