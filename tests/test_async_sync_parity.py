"""AsyncAgentBus MUST mirror AgentBus, mechanically enforced (#234 SEV-2-H).

The class-level comment on AsyncAgentBus.send() says: "The async class MIRRORS
the sync one deliberately. It has drifted before — `phonebook(label=)` landed on
one and not the other — and a caller who switches to async should not silently
lose options."

The docstring said drift is a known problem, and drift kept happening — five
sync-only features shipped in one release, discovered by the round-two audit.
Documentation is not a control. This test IS the control: a new public method
on AgentBus fails CI until its async twin is added, and a param that moves on
one side and not the other fails CI too. Drift stops being a per-release chore.
"""

from __future__ import annotations

import inspect

from agentbus_client.client import AgentBus, AsyncAgentBus

# One entry per legitimate name-map: close() on sync <-> aclose() on async, the
# standard httpx-style asymmetry. Anything else added here needs justification.
_APPROVED_ASYNC_RENAMES = {
    "close": "aclose",
}


def _public_methods(cls: type) -> set[str]:
    """Public methods defined ON THE CLASS (not inherited from _Base or object).

    Inherited methods are shared already; the drift lives in what each subclass
    adds. Inspecting cls.__dict__ excludes inherited names automatically.
    """
    return {
        name
        for name, obj in inspect.getmembers(cls, callable)
        if not name.startswith("_") and name in cls.__dict__
    }


def test_every_sync_method_has_an_async_twin() -> None:
    sync = _public_methods(AgentBus)
    async_ = _public_methods(AsyncAgentBus)
    missing: list[str] = []
    for name in sorted(sync):
        twin = _APPROVED_ASYNC_RENAMES.get(name, name)
        if twin not in async_:
            missing.append(name)
    assert not missing, (
        "AsyncAgentBus is missing async twins for the following AgentBus methods:\n"
        + "\n".join(f"  - {name}" for name in missing)
        + "\n\nA caller who switches sync -> async loses these features. Add an "
        "async equivalent, or if the sync-only asymmetry is deliberate, whitelist "
        "the name in _APPROVED_ASYNC_RENAMES with a comment naming why."
    )


def test_async_has_no_undocumented_extras() -> None:
    sync = _public_methods(AgentBus)
    async_ = _public_methods(AsyncAgentBus)
    # Async can legitimately have close/aclose asymmetry names on its own side.
    allowed_extras = set(_APPROVED_ASYNC_RENAMES.values())
    extras = async_ - sync - allowed_extras
    assert not extras, (
        "AsyncAgentBus has methods that AgentBus does not:\n"
        + "\n".join(f"  - {name}" for name in sorted(extras))
        + "\n\nEither add a sync twin, or whitelist the name in "
        "_APPROVED_ASYNC_RENAMES."
    )


def test_shared_methods_have_the_same_parameter_names() -> None:
    """Async param drift is the same footgun as missing methods — a caller who
    switches sync -> async silently loses (or gets bitten by) a parameter.
    """
    sync = _public_methods(AgentBus)
    async_ = _public_methods(AsyncAgentBus)
    drift: list[str] = []
    for name in sorted(sync & async_):
        sig_s = inspect.signature(getattr(AgentBus, name))
        sig_a = inspect.signature(getattr(AsyncAgentBus, name))
        s_params = tuple(sig_s.parameters.keys())
        a_params = tuple(sig_a.parameters.keys())
        if s_params != a_params:
            drift.append(f"  {name}:\n    sync : {s_params}\n    async: {a_params}")
    assert not drift, (
        "Parameter drift between AgentBus and AsyncAgentBus (a switch from sync "
        "to async will silently lose or reorder these parameters):\n" + "\n".join(drift)
    )


# NOTE: a "must have docstring" test was drafted here and removed. Docstring
# discipline is a separate concern; this file is about mechanical parity, and
# adding a documentation lint would blur what its failures mean. If a docstring
# ratchet is wanted, ticket it separately.
