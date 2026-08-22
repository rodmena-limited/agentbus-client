"""The version-dependent property that made the SEV-1 invisible to CI.

`concurrent.futures.TimeoutError` changed identity between Python 3.10
and 3.11:

    py3.10   a DISTINCT class, NOT a subclass of OSError
    py3.11+  an ALIAS of the builtin TimeoutError, which IS an OSError

Every `except (AgentBusError, OSError, httpx.HTTPError, ...)` guard in
this codebase was written assuming the second. On 3.10 — which
`uv tool install` pins by default, i.e. production — they all missed it,
and the watcher died on network blips inside its own reconnect handler.

These tests exist so the DIVERGENCE ITSELF is asserted rather than
assumed, and so a reader on 3.13 (where the bug is invisible) can see
why the 3.10 leg of tests/run_all_pythons.sh is load-bearing.

The key insight for anyone maintaining this: a test that merely raises
CFT and asserts the watcher survives PASSES ON 3.11+ EVEN IF THE FIX IS
REVERTED, because there the exception is caught by the OSError clause
for free. Only the 3.10 leg can fail. That is exactly the shape of "a
check that cannot go red" — hence the explicit matrix below.
"""

from __future__ import annotations

import concurrent.futures
import sys

import pytest

from agentbus_client.client import AgentBusError, TransportError

IS_PRE_311 = sys.version_info < (3, 11)


def test_cft_identity_matches_this_interpreter_version():
    """Pin the documented divergence. If a future Python changes this
    again, THIS test tells you before the watcher does."""
    is_alias = concurrent.futures.TimeoutError is TimeoutError
    is_oserror = issubclass(concurrent.futures.TimeoutError, OSError)

    if IS_PRE_311:
        assert not is_alias, (
            "py3.10 is documented to have a DISTINCT "
            "concurrent.futures.TimeoutError; it now appears to be an alias. "
            "The exception-translation fix's rationale needs revisiting."
        )
        assert not is_oserror, (
            "py3.10's CFT is documented NOT to subclass OSError. If it now "
            "does, the hand-written except tuples would catch it and the "
            "boundary translation is no longer load-bearing here."
        )
    else:
        assert is_alias
        assert is_oserror


@pytest.mark.skipif(
    not IS_PRE_311,
    reason=(
        "Only meaningful on py3.10, where CFT is NOT an OSError. On 3.11+ "
        "the naive guard catches it for free, so this assertion cannot fail "
        "and would be a check that cannot go red."
    ),
)
def test_on_py310_a_naive_oserror_guard_would_have_missed_cft():
    """THE ORIGINAL BUG, asserted as a property rather than a story.

    This is what every pre-0.9.24 guard in watch.py / cli.py /
    onboarding.py looked like. On 3.10 it does NOT catch the exception a
    network stall actually raises."""
    import contextlib

    import httpx

    escaped = False
    try:
        with contextlib.suppress(AgentBusError, OSError, httpx.HTTPError, ValueError, KeyError):
            raise concurrent.futures.TimeoutError()
    except concurrent.futures.TimeoutError:
        escaped = True

    assert escaped, (
        "on py3.10 a raw concurrent.futures.TimeoutError must ESCAPE the "
        "old hand-written suppress tuple — that escape is the SEV-1. If it "
        "no longer escapes, this interpreter's CFT changed and the fix's "
        "rationale should be re-read."
    )


def test_the_translated_error_is_caught_by_that_same_naive_guard():
    """THE FIX, asserted as a property, on EVERY version.

    The boundary translation converts CFT into TransportError, which is an
    AgentBusError — so the ~29 pre-existing hand-written tuples across the
    codebase catch it without any of them being edited. That is what
    'closing the class' means, and it holds on 3.10 and 3.11+ alike."""
    import contextlib

    import httpx

    caught = True
    try:
        with contextlib.suppress(AgentBusError, OSError, httpx.HTTPError, ValueError, KeyError):
            raise TransportError("SDK call did not complete within 35.0s")
    except TransportError:
        caught = False

    assert caught, (
        "TransportError escaped the standard guard tuple — the boundary "
        "translation would no longer protect the un-audited call sites"
    )
    assert issubclass(TransportError, AgentBusError)
