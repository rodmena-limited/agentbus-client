"""A stored key from a deleted workspace must not be used to register.

MEASURED on a macOS box, 2026-08-16. The machine still held a bound key from a
workspace that had since been deleted. `signin` verified the NEW key and printed
the NEW workspace; `setup` then registered with the OLD stored key and failed:

    registration failed: this workspace has been deleted
      credential used: stored key file .../keys/macbook-admin-bd8e86.env

The machine kept its device-id, so setup re-derived the SAME agent name and
found the stale file. A guard for exactly this existed and could not see it: it
caught only AuthError, and `workspace_deleted` is HTTP 410, which is not in the
client's error map — so it arrived as a plain AgentBusError and hit `pass`.

The cost is the shape that hurts: onboarding reported a verified key and the
right workspace one line before failing on a different, invisible credential.
"""

from __future__ import annotations


def _guard() -> str:
    src = _onboarding_source()
    start = src.index("stored_key = _agent_key(name) if name else None")
    # Wide enough to contain the WHOLE guard. A window that silently cuts the
    # block short turns these into tests of the slice, not of the code — they
    # went red for exactly that reason when the guard grew.
    end = src.index("register_key = stored_key or operator")
    return src[start:end]


def test_a_deleted_workspace_marks_the_stored_key_dead():
    g = _guard()
    assert "workspace_deleted" in g, "a key from a deleted workspace is still reused"


def test_an_auth_rejection_still_marks_it_dead():
    """The original case must not have been lost while widening the guard."""
    g = _guard()
    assert "AuthError" in g


def test_a_transport_failure_does_NOT_mark_it_dead():
    """THE CONTROL. Widening this too far destroys a good credential every time
    the bus blips — the guard must act on an explicit verdict from the server,
    never on a failure to reach it."""
    g = _guard()
    assert "dead = isinstance(exc, AuthError)" in g, "the verdict must be an explicit allow-list"
    # A bare `except AgentBusError: stored_key = None` would be the wrong fix.
    assert 'getattr(exc, "code", "")' in g


def test_the_dead_key_is_renamed_not_deleted():
    """An operator must be able to see what was set aside, and undo it."""
    g = _guard()
    assert ".env.dead" in g


def test_a_stored_key_for_ANOTHER_LIVE_workspace_is_not_reused():
    """THE WORSE CASE, and the one the operator's question exposed.

    "Is the key alive" was the only question asked. A stored key for a
    DIFFERENT but still-live workspace answers whoami() happily, so the guard
    passed and setup would register the agent into THAT workspace while
    printing the one the operator had just signed into — a silent
    wrong-workspace registration, worse than the clear failure that led here.

    The operator typed a key seconds ago and watched it verify. That is the
    intent. A file left on disk from a previous life is not.
    """
    g = _guard()
    assert "workspace" in g
    assert 'mine.get("workspace") != here.get("workspace")' in g, (
        "the stored key is still accepted without checking WHICH workspace it belongs to"
    )


def test_the_mismatched_key_is_set_aside_under_a_distinct_name():
    """`.env.other` rather than `.env.dead`: the key is not dead, it belongs
    somewhere else. An operator returning to that workspace should be able to
    tell the two cases apart."""
    g = _guard()
    assert ".env.other" in g
    assert ".env.dead" in g


def _onboarding_source() -> str:
    """onboarding is a package now (one module per concern): read all of it, in a stable order."""
    from pathlib import Path as _P

    pkg = _P(__file__).resolve().parents[1] / "src" / "agentbus_client" / "onboarding"
    return "".join(f.read_text() for f in sorted(pkg.glob("*.py")))
