"""AgentBus client: give every coding agent a real inbox and a real email address.

from agentbus_client import AgentBus

bus = AgentBus(api_key="ab_sk_...")
me = bus.register(name="builder", repo_remote="git@github.com:acme/api.git")
bus.send(to=["reviewer"], subject="Ready", text="PR is green.")
for message in bus.inbox(wait=30):
    print(message.subject)
    bus.ack(message.delivery_id)
"""

# LAZY, because this package is imported by a hook that runs on EVERY TOOL CALL.
#
# `from .client import ...` pulled httpx eagerly, so `import
# agentbus_client.hooks.claude_code` loaded 144 modules — 46ms of a 57ms import
# on this host, and david measured ~125ms of a 245ms hook on his. The hook uses
# `urllib` and never touches httpx: the whole cost was for an import it does not
# use, paid once per tool call, forever.
#
# Found by david and runflow independently while measuring the gate's latency.
# The gate made this visible because it is the first thing we ship that runs on
# the hot path; the tax existed before and nobody had reason to look.
#
# PEP 562 module __getattr__: `from agentbus_client import AgentBus` still
# works and still returns the same object, it just does not happen until
# someone asks. TYPE_CHECKING keeps the names visible to mypy and to editors.
from typing import TYPE_CHECKING, Any

from . import identity  # noqa: F401  (public: device id, session key)

# identity is stdlib-only and cheap; it stays eager because the hooks that
# import it would otherwise pay a __getattr__ indirection on every access.

if TYPE_CHECKING:
    from .client import (
        AgentBus,
        AgentBusError,
        AsyncAgentBus,
        AuthError,
        Delivery,
        NotFoundError,
        PermissionError_,
        QuotaExceeded,
        RateLimited,
    SelfReplyError,
        ServiceUnavailable,
        TransportError,
        ValidationError,
    )

_LAZY = {
    "AgentBus",
    "AgentBusError",
    "AsyncAgentBus",
    "AuthError",
    "Delivery",
    "NotFoundError",
    "PermissionError_",
    "QuotaExceeded",
    "RateLimited",
    "SelfReplyError",
    "ServiceUnavailable",
    "TransportError",
    "ValidationError",
}


def __getattr__(name: str) -> Any:
    if name in _LAZY:
        from . import client

        return getattr(client, name)
    if name == "identity":
        # Cheap-ish but not free (~10ms), and only two code paths want it.
        import importlib

        return importlib.import_module(".identity", __name__)
    if name == "__version__":
        # `importlib.metadata` was the single biggest cost left on the hot path
        # (~13ms) and exists only to answer `--version`. One fact, one place —
        # pyproject.toml, since a literal here once drifted three releases behind
        # the published package — but resolved when ASKED, not on every import.
        try:
            from importlib.metadata import version as _pkg_version

            return _pkg_version("rodmena-agentbus")
        except Exception:
            return "0.0.0+source"
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "AgentBus",
    "AgentBusError",
    "AsyncAgentBus",
    "AuthError",
    "Delivery",
    "NotFoundError",
    "PermissionError_",
    "QuotaExceeded",
    "RateLimited",
    "SelfReplyError",
    "ServiceUnavailable",
    "TransportError",
    "ValidationError",
]
