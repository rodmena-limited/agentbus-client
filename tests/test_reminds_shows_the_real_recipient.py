"""#50: `reminds` renders the recipient the server states, never an inference.

The renderer was `row.get("target") or "(you)"`. Before the server carried
`target`, that made EVERY reminder list as "-> (you)" including ones addressed
to somebody else (#316) — reported by a platform setting reminders for household
members, who said a listing that cannot show who a reminder is for is one they
would eventually misread.

TWO DIRECTIONS OR NEITHER, which is the backend's point and it applies to this
test as much as to their probe: without a self-addressed case, `target` could be
a field that is always populated and `self_addressed` a constant, and the
targeted assertion would pass on a renderer that had learned nothing.

THE NULL CASE IS THE ONE THE FIELD EXISTS FOR. A null target means the recipient
AGENT NO LONGER EXISTS. The old `or` fallback answered that with "(you)" — a
confident wrong answer where "I cannot tell" is the true one, and the same
null-is-unknown rule this repo applied to blocks (#48).
"""

from __future__ import annotations

from agentbus_client.cli._remind import _render

BASE = {"id": "01X", "due_at": "2026-09-01T09:00:00", "state": "scheduled"}


def _row(**over):
    return {**BASE, **over}


def test_a_self_note_renders_as_you():
    out = _render(_row(target="agentbus-client-c70fbf", self_addressed=True))
    assert "(you)" in out
    # the agent's own long name is noise on the common case
    assert "agentbus-client-c70fbf" not in out


def test_a_targeted_reminder_names_the_recipient():
    """THE REPORTED BUG: this used to render '(you)'."""
    out = _render(_row(target="agentbus-ui-c760a1", self_addressed=False))
    assert "agentbus-ui-c760a1" in out
    assert "(you)" not in out


def test_a_null_target_is_reported_unknown_not_you():
    """The `or` fallback's failure. Guessing the sender is the defect."""
    out = _render(_row(target=None, self_addressed=False))
    assert "(you)" not in out
    assert "gone" in out or "unknown" in out


def test_self_addressed_wins_over_the_target_name():
    """The flag is authoritative; the client must not re-derive it by comparing
    `target` to its own identity — the server computes it precisely so the
    client does not put an identity lookup on every row."""
    out = _render(_row(target="some-other-name", self_addressed=True))
    assert "(you)" in out


def test_the_renderer_still_shows_when_and_state():
    """Known-positive: the line must not have lost its other content while the
    recipient logic changed."""
    out = _render(_row(target="peer", self_addressed=False, repeat="daily"))
    assert "2026-09-01" in out
    assert "scheduled" in out
    assert "daily" in out
