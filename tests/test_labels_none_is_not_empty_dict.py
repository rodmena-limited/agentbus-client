"""`register()` must not send `labels: {}` when the caller passed nothing.

issuedb #37 follow-up. The backend's F11 fix (b7832bf) distinguishes two cases
with `model_fields_set`:

    sent {}          -> "you asked to clear; labels MERGE, here is how to remove"
    never mentioned  -> silent, because the caller asked for nothing

That distinction is correct and it is the difference between a useful advisory
and noise on every register. This client defeated it: `"labels": labels or {}`
coerced None to {}, so EVERY register looked like a clear request and the
advisory fired on plain re-registers — the commonest call there is.

CAUGHT BY A NEGATIVE CONTROL, not by review. Re-running the F11 reproduction
against the fix, the positive case passed and the "must stay silent" case
FAILED. Had the negative control been left out — the easy thing to do, since
the fix visibly worked — this would have shipped as noise that teaches everyone
to ignore the field before the one caller who needed it ever sees it.

BOTH SURFACES. sync and async have drifted on exactly this kind of detail
before (`phonebook(label=)` landed on one and not the other), so the parity is
asserted rather than assumed.
"""

from __future__ import annotations

import pytest

from agentbus_client.client import AgentBus, AsyncAgentBus


class _Spy:
    """Captures the register payload without touching the network."""

    def __init__(self):
        self.body = None

    def __call__(self, method, path, **kw):
        if "register" in path:
            self.body = kw.get("json")
        return {"agent": {"name": "a", "labels": {}}, "address": "a@b", "rooms": []}


def _payload(**kwargs):
    bus = AgentBus(api_key="ab_sk_x_y", base_url="http://localhost")
    spy = _Spy()
    bus._request = spy
    bus.register("a", **kwargs)
    return spy.body


def test_omitting_labels_omits_the_key_entirely():
    """The silent case. `labels` absent means "I did not ask about labels"."""
    assert "labels" not in _payload()


def test_an_explicit_empty_dict_is_still_sent():
    """The clear attempt. It must reach the server so the advisory can fire —
    dropping it would trade noise for silence, which is the same bug."""
    body = _payload(labels={})
    assert "labels" in body and body["labels"] == {}


def test_real_labels_are_sent_unchanged():
    assert _payload(labels={"team": "core"})["labels"] == {"team": "core"}


def test_none_and_empty_dict_are_not_the_same_request():
    """The whole point, stated as one assertion."""
    assert _payload() != _payload(labels={})


@pytest.mark.asyncio
async def test_the_async_twin_behaves_identically():
    """Parity, asserted rather than assumed — this pair has drifted before."""
    bus = AsyncAgentBus(api_key="ab_sk_x_y", base_url="http://localhost")
    captured = {}

    async def spy(method, path, **kw):
        if "register" in path:
            captured["body"] = kw.get("json")
        return {"agent": {"name": "a", "labels": {}}, "address": "a@b", "rooms": []}

    bus._request = spy
    await bus.register("a")
    assert "labels" not in captured["body"]
    await bus.register("a", labels={})
    assert captured["body"]["labels"] == {}


def test_neither_surface_coerces_none_to_an_empty_dict():
    """Regression guard on the exact expression that caused this.

    `labels or {}` is the bug: it cannot distinguish None from {} because both
    are falsy. Asserting on the source keeps a future refactor from quietly
    reintroducing it on either twin.
    """
    import inspect

    from agentbus_client.client import async_directory, sync_directory

    for module in (sync_directory, async_directory):
        assert '"labels": labels or {}' not in inspect.getsource(module), (
            f"{module.__name__} coerces None to {{}}, which erases the "
            f"distinction the server's advisory depends on"
        )
