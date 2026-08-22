"""Typed bases that tell mypy what the client mixins may rely on.

WHY THIS EXISTS (#48). The client is assembled from mixins — the file-size cap
(review #23) split one class across several files — and every mixin calls
`self._request(...)`, `self.agent`, `self.unseal_message(...)` and friends, which
are defined on the CONCRETE class that combines them. mypy sees each mixin
alone, so all of that read as `has no attribute`: 171 errors, and they drowned
the handful of real ones. A type checker whose output nobody reads is a check
that cannot go red.

These bases are TYPE_CHECKING-only. At runtime the mixins still inherit `object`,
so there is NO new base class, no MRO change, and no import cycle — the concrete
client remains the only thing that actually provides these members.

They are DELIBERATELY not `Protocol`. A Protocol advertises a structural
contract others may implement; this is a private note to the type checker about
what the concrete client already provides, and saying so keeps anyone from
building against it as an interface.
"""

from __future__ import annotations

from typing import Any


class SyncClientBase:
    """What every SYNC mixin may assume the assembled client provides."""

    agent: str | None
    workspace: str | None
    timeout: float
    _client: Any
    _challenge_lock: Any
    _pending_challenge: Any

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _headers(self, *args: Any, **kwargs: Any) -> dict[str, str]:
        raise NotImplementedError

    def read(self, delivery_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError

    def unseal_message(self, message: Any, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _apply_seal(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _seal_if_needed(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _seal_to_self(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _sign_if_possible(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


class AsyncClientBase:
    """What every ASYNC mixin may assume. Kept separate from the sync base on
    purpose: the twins have drifted before (async `read` once skipped unsealing
    entirely), and one shared base would let a coroutine and a plain value be
    interchangeable to the checker."""

    agent: str | None
    workspace: str | None
    timeout: float
    _client: Any
    _challenge_lock: Any
    _pending_challenge: Any

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _headers(self, *args: Any, **kwargs: Any) -> dict[str, str]:
        raise NotImplementedError

    async def read(self, delivery_id: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError

    def unseal_message(self, message: Any, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _apply_seal(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _seal_if_needed(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def _seal_to_self_async(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _seal_to_self(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _sign_if_possible(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError
