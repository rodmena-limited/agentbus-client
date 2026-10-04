from __future__ import annotations

import io
import sys
import time

import pytest


class _Bus:
    agent = "a"
    base_url = "https://x"
    api_key = "k"

    def inbox(self, *a: object, **kw: object) -> list[object]:
        return []


def _watcher(tmp_path):  # type: ignore[no-untyped-def]
    from agentbus_client import watch as watch_module

    return watch_module, watch_module.Watcher(_Bus(), agent="a", state_path=tmp_path / "s.json")


def test_backoff_announces_on_the_stderr_current_at_call_time(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watch_module, w = _watcher(tmp_path)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(watch_module.random, "uniform", lambda lo, hi: 0.0)
    replacement = io.StringIO()
    monkeypatch.setattr(sys, "stderr", replacement)
    w._backoff_and_drain("stream dropped (test)")
    assert "agentbus watch: stream dropped (test); retrying in 1.0s" in replacement.getvalue()


def test_an_explicit_stream_still_wins(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    watch_module, w = _watcher(tmp_path)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr(watch_module.random, "uniform", lambda lo, hi: 0.0)
    target = io.StringIO()
    w._backoff_and_drain("explicit", stream=target)
    assert "agentbus watch: explicit; retrying in" in target.getvalue()
