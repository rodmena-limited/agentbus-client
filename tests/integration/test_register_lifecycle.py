"""The registration lifecycle, against the live service.

This is the path four server-side rounds were spent on (backend #314) and the
one only an UNBOUND credential can exercise: an agent-bound key returns
`403 key_agent_mismatch` before the precedence logic is reached, so the team that
wrote the fix could not test it. That asymmetry — the fixer cannot observe, the
observer cannot fix — is why it took four rounds, and it is exactly the gap a
committed integration test closes.
"""

from __future__ import annotations

import contextlib
import time

import pytest

from agentbus_client.client import AgentBus


@pytest.fixture
def role() -> str:
    return f"itest-{int(time.time())}"


def _bus(operator_key: str) -> AgentBus:
    return AgentBus(api_key=operator_key)


def test_an_explicit_name_is_honoured_over_a_derived_one(operator_key, workspace, role):
    """The reported defect: `--role` silently overrode an explicit name, which
    is how a caller reclaiming an identity got a different one."""
    bus = _bus(operator_key)
    created = []
    try:
        first = bus.register(f"{role}-alpha", role=role)["agent"]["name"]
        created.append(first)
        assert first == f"{role}-alpha"

        # A session match now exists — the condition the bug needed.
        second = bus.register(f"{role}-beta", role=role)["agent"]["name"]
        created.append(second)
        assert second == f"{role}-beta", "an explicit name was overridden by the session match"
    finally:
        for name in created:
            with contextlib.suppress(Exception):
                bus._request("POST", f"/v1/agents/{name}/retire", agent=name)


def test_a_nameless_register_still_reclaims_the_session_identity(operator_key, workspace, role):
    """THE CONTROL, and the one that matters more.

    A fix making the name path win unconditionally would break the reclaim for
    every ordinary caller while passing the test above — it is only exercised in
    the rare environment the bug needed.
    """
    bus = _bus(operator_key)
    created = []
    try:
        alpha = bus.register(f"{role}-alpha", role=role)["agent"]["name"]
        created.append(alpha)
        bus.register(f"{role}-beta", role=role)
        created.append(f"{role}-beta")

        reclaimed = bus.register(None, role=role)["agent"]["name"]
        assert reclaimed == alpha, "the nameless reclaim stopped returning the session holder"
    finally:
        for name in created:
            with contextlib.suppress(Exception):
                bus._request("POST", f"/v1/agents/{name}/retire", agent=name)


def test_re_registering_an_explicitly_named_agent_does_not_500(operator_key, workspace, role):
    """A named agent registers once and then must be able to COME BACK.

    `--role` is what setup and every session start pass, so a 500 here bricks the
    identity on restart — worse than the original defect, which merely handed
    you the wrong one.
    """
    bus = _bus(operator_key)
    created = []
    try:
        created.append(bus.register(f"{role}-alpha", role=role)["agent"]["name"])
        created.append(bus.register(f"{role}-beta", role=role)["agent"]["name"])
        again = bus.register(f"{role}-beta", role=role)["agent"]["name"]
        assert again == f"{role}-beta"
    finally:
        for name in created:
            with contextlib.suppress(Exception):
                bus._request("POST", f"/v1/agents/{name}/retire", agent=name)
