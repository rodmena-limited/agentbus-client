from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal, NoReturn, cast
from unittest.mock import patch

import pytest
from typing_extensions import Self

from agentbus_client.watch import Watcher

if TYPE_CHECKING:
    from pathlib import Path

# ------------------------------------------------- Fix: revocation is TERMINAL
#
# SPECS/0020: AuthError (confirmed-revoked credential) exits 8 and is never
# retried. The cli.py handler only fires if Watcher.run() actually lets
# AuthError ESCAPE — the generic `except Exception` in the reconnect loop used
# to swallow it into _backoff_and_drain and retry forever (hammering the bus
# with a key that will never work): the SSE revocation asymmetry the audit
# flagged.


class _FakeBus:
    """Minimum surface for Watcher: `.inbox`, `.agent`, `.base_url`,
    `.api_key`, `.whoami`. Everything is patchable."""

    def __init__(self) -> None:
        self.agent = "test-agent"
        self.base_url = "https://x"
        self.api_key = "ab_sk_test"
        self.inbox_raises: BaseException | None = None
        self.inbox_calls = 0

    def inbox(self, cursor: Any, limit: int = 100, agent: Any = None) -> list[Any]:
        self.inbox_calls += 1
        if self.inbox_raises is not None:
            raise self.inbox_raises
        return []

    def whoami(self, agent: Any = None) -> dict[str, Any]:
        return {"workspace": {"slug": "test-ws"}}


def _watcher(bus: Any, tmp_path: Path) -> Watcher:
    return Watcher(bus, agent="test-agent", state_path=tmp_path / "state.json")


def test_run_propagates_confirmed_revocation_from_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run() must re-raise AuthError from _stream_once, not fold it into the
    reconnect backoff. If it were swallowed, _backoff_and_drain would fire —
    which this test turns into a loud failure instead of an infinite loop."""
    from agentbus_client import watch as watch_module
    from agentbus_client.client import AuthError

    bus = _FakeBus()
    w = _watcher(bus, tmp_path)

    def _revoked(self: Any, *, once: bool = False) -> NoReturn:
        msg = "API key was revoked"
        raise AuthError(msg, code="revoked", status=401)

    def _must_not_backoff(self: Any, reason: Any, stream: Any = None) -> NoReturn:
        msg = "revoked credential was folded into the backoff loop"
        raise AssertionError(msg)

    with (
        patch.object(watch_module.Watcher, "_stream_once", _revoked),
        patch.object(watch_module.Watcher, "_backoff_and_drain", _must_not_backoff),
        pytest.raises(AuthError),
    ):
        w.run()


def test_run_propagates_revocation_from_startup_drain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A revoked key at STARTUP is terminal too: run(once=True) must raise
    AuthError, not defer the drain and return 0 (a revoked credential must
    never look like a successful `--once` check)."""
    from agentbus_client.client import AuthError

    bus = _FakeBus()
    bus.inbox_raises = AuthError("revoked", code="revoked", status=401)
    w = _watcher(bus, tmp_path)
    with pytest.raises(AuthError):
        w.run(once=True)


class _FakeStreamResponse:
    """Minimal httpx stream response: needs the context-manager protocol
    (`client.stream(...) as response`) and raise_for_status — nothing else is
    reached for these 4xx tests."""

    def __init__(self, status_code: int) -> None:
        self.status_code = status_code

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        return False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            msg = f"stream error {self.status_code}"
            raise httpx.HTTPStatusError(
                msg,
                request=cast("httpx.Request", object()),
                response=cast("httpx.Response", self),
            )


class _FakeStreamClient:
    def __init__(self, status_code: int) -> None:
        self._status = status_code

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        return False

    def stream(self, method: str, url: str, headers: Any = None) -> _FakeStreamResponse:
        return _FakeStreamResponse(self._status)


def test_stream_once_confirmed_401_raises_auth_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /v1/stream returning 401 with a REST-confirmed revocation raises
    AuthError (-> exit 8), not httpx.HTTPStatusError folded into backoff."""

    from agentbus_client import watch as watch_module

    bus = _FakeBus()
    w = _watcher(bus, tmp_path)
    monkeypatch.setattr(watch_module.httpx, "Client", lambda timeout=None: _FakeStreamClient(401))
    monkeypatch.setattr(w, "_key_really_revoked", lambda: True)
    with pytest.raises(watch_module.AuthError):
        w._stream_once()


def test_stream_once_401_without_confirmation_stays_transient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GET /v1/stream returning 401 that is NOT a confirmed revocation (a
    server blip) re-raises the HTTPStatusError so the backoff loop reconnects —
    it must NOT exit 8 on the server's word alone."""
    import httpx

    from agentbus_client import watch as watch_module

    bus = _FakeBus()
    w = _watcher(bus, tmp_path)
    monkeypatch.setattr(watch_module.httpx, "Client", lambda timeout=None: _FakeStreamClient(401))
    monkeypatch.setattr(w, "_key_really_revoked", lambda: False)
    with pytest.raises(httpx.HTTPStatusError):
        w._stream_once()
