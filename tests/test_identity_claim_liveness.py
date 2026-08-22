"""#126 — a claim FILE is not a live session.

`_warn_if_identity_shared` fired on a timestamp alone: any session id other than
ours written in the last 12 hours produced "ANOTHER SESSION IS ALREADY '<agent>'
... last seen 0 min ago". Nothing checked whether that session still existed, and
nothing removed the claim when a session ended — so a session that exited SECONDS
ago warned exactly as loudly as one running right now.

The predecessor it names is very often the session that just handed over: on a
restart that rotates identity, the short-lived session that CREATED the agent
leaves precisely this residue. The alarm was loudest where it was most likely to
be wrong, and phrased as if action were required.

Found by bikeroom-freebsd-operato-dd8bca on their own restart. They repeated it to
two peers as established fact before checking, and by then it had propagated into
a PRIVACY decision about a real screen capture — a warning about a swallowed inbox
changed what a human was told about where their desktop image would land. That is
why a false alarm here is not a cosmetic defect.

BOTH DIRECTIONS, because a fix that simply silenced the warning would pass a
one-sided test and destroy the guard: the live-holder case must still fire.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentbus_client.hooks import claude_code

AGENT = "bikeroom-freebsd-operato-dd8bca"
OURS = "11c10641-87e1-4d50-bdf5-299f9af49657"
GONE = "37fce55e-0000-0000-0000-000000000000"


@pytest.fixture
def claim(tmp_path, monkeypatch):
    """Redirect the claim file into a tmp dir and pin our session id."""
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", OURS)
    monkeypatch.setattr(
        claude_code._session, "_identity_claim_path", lambda a: tmp_path / f"claim-{a}.json"
    )
    return tmp_path / f"claim-{AGENT}.json"


def _write_claim(path: Path, session: str, age_seconds: float = 5.0) -> None:
    path.write_text(json.dumps({"session": session, "at": time.time() - age_seconds}))


def test_exited_session_does_not_raise_a_collision_alarm(claim, capsys, monkeypatch):
    """The #126 fix: a fresh claim from a DEAD session must stay silent.

    Stubs `_monitor_pids` — which exists on BOTH sides of this change — rather
    than the new helper, so failing here means the code ALARMED, not merely that
    a symbol was missing. Verified red against the pre-fix revision on exactly
    this assertion.
    """
    _write_claim(claim, GONE, age_seconds=5.0)
    # No live watcher holds it — the state bikeroom actually verified on their box.
    monkeypatch.setattr("agentbus_client.onboarding._monitor_pids", lambda _a, **_k: [])

    claude_code._warn_if_identity_shared(AGENT)

    out = capsys.readouterr().out
    # The #126 assertion: no COLLISION CLAIM about a session nothing verified.
    # Total silence was the original wording here and #128 replaced it — see
    # test_unverified_claim_says_so_instead_of_going_silent: silence asserted a
    # safety that had not been established, which is the same error inverted.
    assert "ANOTHER SESSION IS ALREADY" not in out, (
        f"alarmed about a session that no longer exists: {out!r}"
    )


def test_live_holder_still_alarms(claim, capsys, monkeypatch):
    """THE KNOWN-POSITIVE. Without this, the test above passes on a build that
    never warns at all — which would delete the guard rather than fix it."""
    _write_claim(claim, GONE, age_seconds=5.0)
    monkeypatch.setattr("agentbus_client.onboarding._monitor_pids", lambda _a, **_k: ["67474"])

    claude_code._warn_if_identity_shared(AGENT)

    out = capsys.readouterr().out
    assert "ANOTHER SESSION IS ALREADY" in out
    assert AGENT in out
    assert "read/ack state is shared" in out


def test_liveness_is_actually_consulted(claim, monkeypatch):
    """Assert the check is CALLED with the right pair, not merely defined.

    A guard that computes liveness and ignores it would pass both tests above if
    they only ever stubbed the result.
    """
    _write_claim(claim, GONE, age_seconds=5.0)
    seen: list[tuple[str, str]] = []

    def _spy(agent: str, session: str) -> bool:
        seen.append((agent, session))
        return False

    monkeypatch.setattr(claude_code._session, "_identity_held_live", _spy)
    claude_code._warn_if_identity_shared(AGENT)

    assert seen == [(AGENT, GONE)], (
        "liveness must be checked against the OTHER session's id, not ours"
    )


def test_our_own_prior_claim_is_never_a_collision(claim, capsys, monkeypatch):
    """Re-running in the same session is a no-op, and must not consult liveness."""
    _write_claim(claim, OURS, age_seconds=5.0)

    def _boom(_agent: str, _session: str) -> bool:
        raise AssertionError("liveness checked for our own session id")

    monkeypatch.setattr(claude_code._session, "_identity_held_live", _boom)
    claude_code._warn_if_identity_shared(AGENT)

    assert capsys.readouterr().out == ""


def test_claim_is_taken_over_after_a_stale_one(claim, monkeypatch):
    """Silence is not enough — the stale record must be replaced by ours."""
    _write_claim(claim, GONE, age_seconds=5.0)
    monkeypatch.setattr(claude_code._session, "_identity_held_live", lambda _a, _s: False)

    claude_code._warn_if_identity_shared(AGENT)

    assert json.loads(claim.read_text())["session"] == OURS


def test_liveness_helper_fails_closed_on_a_broken_process_table(monkeypatch):
    """An unreadable `ps` must not manufacture an alarm about a session we could
    not look for. Absence of evidence is reported as 'not held', deliberately."""

    def _explode(*_a, **_k):
        raise OSError("ps unavailable")

    monkeypatch.setattr("agentbus_client.onboarding._monitor_pids", _explode)

    assert claude_code._identity_held_live(AGENT, GONE) is False


def test_liveness_helper_reports_true_when_a_watcher_exists(monkeypatch):
    """Known-positive for the helper itself, so its False is meaningful."""
    monkeypatch.setattr("agentbus_client.onboarding._monitor_pids", lambda _a, **_k: ["4242"])

    assert claude_code._identity_held_live(AGENT, GONE) is True


# ---------------------------------------------------------------------------
# #128 — two voices, because there are two epistemic states.
#
# The #126 fix above folded "cannot confirm a holder" into "no holder" and went
# silent. That is the same category error as the original bug, inverted: the old
# code asserted a collision it had not verified, the new code asserted safety it
# had not verified. Reported by the same peer whose incident produced #126, and
# demonstrated in the wild — a worktree session holding this identity with no
# monitor read two messages and they would have shown as "no new messages" here,
# permanently, with no error anywhere.
#
# The tiers differ in what they CLAIM, not merely in volume. "last seen 0 min ago"
# read as MEASURED when nothing had measured it, and that grammar is why a careful
# reader propagated it instead of checking it.
# ---------------------------------------------------------------------------


def test_unverified_claim_says_so_instead_of_going_silent(claim, capsys, monkeypatch):
    """No monitor found must NOT be reported as no collision."""
    _write_claim(claim, GONE, age_seconds=5.0)
    monkeypatch.setattr("agentbus_client.onboarding._monitor_pids", lambda _a, **_k: [])

    claude_code._warn_if_identity_shared(AGENT)

    out = capsys.readouterr().out
    assert out != "", "silence asserts safety that was never verified"
    assert "LIVENESS NOT VERIFIED" in out
    assert GONE[:8] in out
    # It must NOT claim a collision — that was the original #126 defect.
    assert "ANOTHER SESSION IS ALREADY" not in out
    # And it must name the command that settles it, or the reader is left where
    # bikeroom was: holding an unverifiable claim with nowhere to take it.
    assert "watch-status" in out


def test_verified_holder_says_the_liveness_was_verified(claim, capsys, monkeypatch):
    """The loud tier must distinguish itself as MEASURED, not inferred."""
    _write_claim(claim, GONE, age_seconds=5.0)
    monkeypatch.setattr("agentbus_client.onboarding._monitor_pids", lambda _a, **_k: ["67474"])

    claude_code._warn_if_identity_shared(AGENT)

    out = capsys.readouterr().out
    assert "ANOTHER SESSION IS ALREADY" in out
    assert "FOUND" in out and "verified, not inferred" in out
    assert "LIVENESS NOT VERIFIED" not in out


def test_the_two_tiers_are_never_both_emitted(claim, capsys, monkeypatch):
    """One arrival at this code, one voice."""
    _write_claim(claim, GONE, age_seconds=5.0)
    monkeypatch.setattr("agentbus_client.onboarding._monitor_pids", lambda _a, **_k: ["67474"])

    claude_code._warn_if_identity_shared(AGENT)

    out = capsys.readouterr().out
    assert not ("LIVENESS NOT VERIFIED" in out and "ANOTHER SESSION IS ALREADY" in out)


@pytest.mark.usefixtures("claim")  # needed for its tmp-dir/session setup, value unused
def test_still_silent_when_there_is_no_claim_at_all(capsys, monkeypatch):
    """The soft tier must not become a startup banner. No claim, no line."""
    monkeypatch.setattr("agentbus_client.onboarding._monitor_pids", lambda _a, **_k: [])

    claude_code._warn_if_identity_shared(AGENT)

    assert capsys.readouterr().out == ""


def test_still_silent_for_our_own_claim_without_a_monitor(claim, capsys, monkeypatch):
    """Re-running in the same session must not report itself as a maybe-collision."""
    _write_claim(claim, OURS, age_seconds=5.0)
    monkeypatch.setattr("agentbus_client.onboarding._monitor_pids", lambda _a, **_k: [])

    claude_code._warn_if_identity_shared(AGENT)

    assert capsys.readouterr().out == ""


def test_a_stale_claim_beyond_the_window_stays_silent(claim, capsys, monkeypatch):
    """Yesterday's residue is not news; the 12h window still bounds the notice."""
    _write_claim(claim, GONE, age_seconds=43200 + 60)
    monkeypatch.setattr("agentbus_client.onboarding._monitor_pids", lambda _a, **_k: [])

    claude_code._warn_if_identity_shared(AGENT)

    assert capsys.readouterr().out == ""
