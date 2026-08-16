"""Standalone-repo test fixtures.

Isolates every test in its own $HOME so state files written by the client
(the fast-fail circuit's gate-degraded record, sealing keys, watch cursors,
identity claims) cannot leak between tests. The main agentbus repo's suite
happens to run in an order where this hasn't bitten; the extracted repo has
a different test set + order and needs the explicit isolation.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Every test gets its own HOME so ~/.config/agentbus/ is clean.

    Applied autouse because ANY test that constructs an AgentBus, calls a hook,
    or writes a state file MUST NOT inherit the previous test's residue —
    especially the gate-degraded-<agent>.json which the SEV-1-C fast-fail
    circuit reads on every pre_tool_use call.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    # Some code paths honor AGENTBUS_CONFIG_DIR directly; also isolate that.
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(tmp_path / ".config" / "agentbus"))
    # Undo any inherited AGENTBUS_* keys that would poison a fresh test.
    for k in ("AGENTBUS_API_KEY", "AGENTBUS_AGENT", "AGENTBUS_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    yield tmp_path
