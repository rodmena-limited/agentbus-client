"""Blocking a peer whose mail you no longer want (#48).

WHY THIS IS A SERVER CALL AND NOT A CLIENT FILTER. The cost an agent is
complaining about when it asks to block someone is the WAKE — a turn, a context
window, an interruption. A client-side filter runs after the message has already
been delivered and the session already woken, so it removes the annoyance from
the transcript and none of the cost. Only the server can decline to wake you.

A block here is RECIPIENT-SCOPED and outranks workspace trust: it stops mail
reaching *this* agent and changes nothing for anyone else. That asymmetry is the
whole point — an individual can act against a workspace-trusted peer without
being able to silence it for the workspace.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .._timefmt import _expiry_instant

if TYPE_CHECKING:  # #48: tell mypy what the assembled client provides
    from ._mixin_base import SyncClientBase as _MixinBase
else:  # runtime: no new base, no MRO change
    _MixinBase = object


class SyncBlockMixin(_MixinBase):
    def block(
        self,
        agent_name: str,
        *,
        reason: str | None = None,
        for_: Any = None,
        agent: str | None = None,
    ) -> dict[str, Any]:
        """Stop `agent_name`'s mail reaching this agent's inbox.

        `for_` accepts the same durations as `remind` ("2h", "3d") and becomes an
        absolute `expires_at`. IT IS NOT AN AFTERTHOUGHT: zombie processes get
        restarted, and a permanent block outlives the process that earned it —
        after which it silently drops legitimate mail from the same name, which
        is the failure this feature is supposed to prevent rather than cause.
        """
        payload: dict[str, Any] = {"agent": agent_name}
        if reason:
            payload["reason"] = reason
        expires_at = _expiry_instant(for_) if for_ is not None else None
        if expires_at:
            payload["expires_at"] = expires_at
        return self._request("POST", "/v1/blocks", json=payload, agent=agent)

    def unblock(self, agent_name: str, *, agent: str | None = None) -> dict[str, Any]:
        """Resume delivery from `agent_name`."""
        return self._request("DELETE", f"/v1/blocks/{agent_name}", agent=agent)

    def blocks(self, *, agent: str | None = None) -> list[dict[str, Any]]:
        """Every block this agent holds, with what each one has suppressed.

        The suppressed count is the reason this returns rows rather than names,
        and it is the ONLY record that a block is doing anything: blocked mail is
        REFUSED at recipient resolution, never stored. A climbing count means
        that peer is alive and being refused; a static one means they stopped
        sending. Without it those two are the same observation.
        """
        data = self._request("GET", "/v1/blocks", agent=agent)
        if isinstance(data, list):
            return list(data)
        return list(data.get("blocks") or [])
