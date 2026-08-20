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
import re

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


# REG-4 (round-3 audit): the parity checks above catch method-name and
# parameter-name drift, but async heartbeat shipped with a WRONG ENDPOINT
# (POST /v1/heartbeat) while the sync one used POST /v1/agents/<agent>/heartbeat
# — same signature, so parameter parity passed and CI stayed green. Presence
# went silently stale for anyone using the async client. Adding an endpoint-URL
# comparison to catch this class of drift.

# The regex intentionally strips f-string interpolation braces so
# f"/v1/agents/{agent}/heartbeat" and f"/v1/agents/{name}/heartbeat" compare
# equal — the URL SHAPE, not the caller's local variable name, is what routes.
_INTERP_RE = re.compile(r"\{[^{}]*\}")


def _endpoint_urls(cls: type) -> dict[str, set[str]]:
    """Extract HTTP URLs from every _request(...) call in every public method
    on `cls`, keyed by method name. Returns the sync-vs-async comparable shape.

    Read from the source rather than by mocking _request and calling every
    method, because many methods require credentials / real state to run at all;
    static extraction is enough to catch a literal string mismatch.
    """
    urls: dict[str, set[str]] = {}
    for name in sorted(_public_methods(cls)):
        try:
            src = inspect.getsource(getattr(cls, name))
        except (TypeError, OSError):
            continue
        # A URL is the second positional to _request/self._request/await ...
        # For robustness, grab every string literal that starts with "/v1/".
        found = set(re.findall(r'["\']((?:/v1/[^"\']*|/[a-z_]+))["\']', src))
        # Normalise interpolation placeholders so agent-name variables do not
        # cause spurious differences.
        urls[name] = {_INTERP_RE.sub("{}", u) for u in found}
    return urls


def test_shared_methods_use_the_same_endpoints() -> None:
    """The API URLs a sync/async twin call MUST match. This is what caught REG-4:
    a caller who calls `bus.heartbeat()` on the async client used to silently 404
    against /v1/heartbeat while the sync client correctly posted to
    /v1/agents/<agent>/heartbeat. Same method name, same parameters, different
    endpoint — a class the earlier parity checks did not cover.
    """
    sync_urls = _endpoint_urls(AgentBus)
    async_urls = _endpoint_urls(AsyncAgentBus)
    drift: list[str] = []
    for name in sorted(set(sync_urls) & set(async_urls)):
        # Some methods legitimately use different URLs on different code paths
        # (e.g. reply resolves either message-id or delivery-id first) — compare
        # AS SETS so any twin that touches the same endpoints in either order
        # matches. What we are catching is a URL that exists on one side and
        # not the other.
        s, a = sync_urls[name], async_urls[name]
        if s and a and s != a:
            drift.append(f"  {name}:\n    sync : {sorted(s)}\n    async: {sorted(a)}")
    assert not drift, (
        "Endpoint drift between AgentBus and AsyncAgentBus (a twin uses a "
        "different HTTP URL than its counterpart, so the async caller may 404 "
        "or hit the wrong resource):\n" + "\n".join(drift)
    )


def test_private_sealing_twin_covers_async_secure_draft(tmp_path, monkeypatch) -> None:
    """C (reliability audit follow-up): async `secure_draft` called the
    UNDEFINED `_seal_to_self_async` — a guaranteed AttributeError on the async
    draft path, invisible to the public-method parity check above because the
    helper is private. Pin the twin exists with the sync signature and seals a
    body to the acting agent's own key on an encrypted workspace.

    (Not part of the public parity sweep on purpose — verified here explicitly,
    with the config dir isolated to tmp so the test never touches real keys.)
    """
    import asyncio
    import inspect

    import agentbus_client.sealing as sealing_mod
    from agentbus_client.client import AsyncAgentBus

    sync_params = list(inspect.signature(AgentBus._seal_to_self).parameters)
    async_params = list(inspect.signature(AsyncAgentBus._seal_to_self_async).parameters)
    assert async_params == sync_params, (
        f"async _seal_to_self_async signature {async_params} diverges from sync "
        f"_seal_to_self {sync_params}"
    )

    monkeypatch.setattr(sealing_mod, "config_dir", lambda: tmp_path)
    bus = AsyncAgentBus(api_key="ab_sk_stub", base_url="https://stub", agent="t")

    async def fake_request(_method, path, **_kwargs):
        if path == "/v1/recipients/resolve":
            return {"encrypted": True, "keys": {}}
        raise AssertionError(f"unexpected request path: {path}")

    monkeypatch.setattr(bus, "_request", fake_request)
    sealed = asyncio.run(bus._seal_to_self_async({"text": "secret"}, "t"))
    assert sealed.get("sealed") is True
    assert sealed["text"].lstrip().startswith("-----BEGIN AGE ENCRYPTED FILE-----")
