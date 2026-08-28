"""#336 — `reminds` must filter on the SERVER, with the parameter the API has.

WHAT WENT WRONG, and why an ordinary test would not have caught it. The client
sent `{"all": "true"}`. The API's query parameter is `state` (scheduled | all).
An unknown query parameter is not an error — it is ignored — so every call asked
for EVERY state and got back the newest 50 rows by `created_at`, and the CLI then
filtered live rows in Python.

That put the truncation BEFORE the state filter. An old but still-live recurring
reminder was crowded off the page by newer FINISHED one-shots and simply
disappeared from the listing. A customer reported five recurring reminders as
vanished; every row was still in the database.

Nothing failed. The request succeeded, the response was well-formed, the CLI
rendered it, and the only symptom was rows that were not there. So the assertion
that matters is on THE PARAMETERS THAT GO OUT — a test that merely checks
`reminds()` returns a list passes just as happily with a parameter the server
throws away.

The `limit` assertion is here for the same reason: the endpoint has no cursor, so
whatever this client asks for is the most a user can ever see.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentbus_client.client import AgentBus
from agentbus_client.client.async_client import AsyncAgentBus


class _Recorder(AgentBus):
    """Captures the request instead of sending it."""

    def __init__(self) -> None:
        super().__init__(api_key="ab_sk_test_key", agent="a")
        self.seen: dict[str, Any] = {}

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.seen = {"method": method, "path": path, **kwargs}
        return {"reminders": [], "count": 0, "total": 0, "has_more": False}


class _AsyncRecorder(AsyncAgentBus):
    def __init__(self) -> None:
        super().__init__(api_key="ab_sk_test_key", agent="a")
        self.seen: dict[str, Any] = {}

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        self.seen = {"method": method, "path": path, **kwargs}
        return {"reminders": [], "count": 0, "total": 0, "has_more": False}


# ------------------------------------------------------- the parameter name


def test_the_default_asks_the_server_for_live_reminders_only():
    bus = _Recorder()
    bus.reminds()
    params = bus.seen["params"]
    assert params.get("state") == "scheduled", (
        f"expected state=scheduled, got {params}. If this says all=true again, "
        "the filter is back on the client and truncation precedes it."
    )


def test_all_asks_the_server_for_every_state():
    bus = _Recorder()
    bus.reminds(all=True)
    assert bus.seen["params"].get("state") == "all"


def test_the_dead_parameter_is_never_sent_again():
    """`all` is not a parameter this API has. Sending it is not an error — which
    is exactly why it survived: the request succeeded and the filter silently
    did not happen."""
    for flag in (False, True):
        bus = _Recorder()
        bus.reminds(all=flag)
        assert "all" not in bus.seen["params"], (
            "the client is sending `all`, which the server ignores; the state "
            "filter then never reaches the database"
        )


def test_it_asks_for_the_maximum_page_the_api_will_give():
    """There is no cursor on this endpoint, so whatever this client asks for is
    the ceiling on what any user can see. The default of 50 is what let 27 of a
    customer's reminders hide behind newer ones."""
    bus = _Recorder()
    bus.reminds()
    assert int(bus.seen["params"]["limit"]) >= 200


# --------------------------------------------------------------- async twin


@pytest.mark.anyio
async def test_the_async_twin_sends_the_same_parameters():
    """`every_send_path_not_one`: this package has paid three times for a rule
    implemented on only some of its surfaces."""
    bus = _AsyncRecorder()
    await bus.reminds()
    assert bus.seen["params"].get("state") == "scheduled"
    assert "all" not in bus.seen["params"]
    assert int(bus.seen["params"]["limit"]) >= 200

    bus = _AsyncRecorder()
    await bus.reminds(all=True)
    assert bus.seen["params"].get("state") == "all"


# ------------------------------------------------------------- known-negative


def test_the_recorder_would_notice_a_wrong_parameter():
    """KNOWN-POSITIVE for the recorder itself. If `_request` were not being
    intercepted, `seen` would stay empty and every assertion above would fail on
    a KeyError rather than passing vacuously — but prove it captures, so a future
    refactor that bypasses `_request` is caught here and not in the field."""
    bus = _Recorder()
    bus.reminds()
    assert bus.seen["method"] == "GET"
    assert bus.seen["path"] == "/v1/reminders"
    assert isinstance(bus.seen["params"], dict) and bus.seen["params"]
