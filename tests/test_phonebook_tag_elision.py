"""#165 — the phonebook's tag elision must be legible to a PROGRAM.

Two failures with real victims, both from a character-slice + bare ellipsis:

  * the `…` was the only signal anything was missing, so a consumer parsing the
    display counted it as a tag. agentbus-frontend computed a team-bucket table
    from this output while `team:hive` sat past the cutoff — their evidence said
    1 agent where 5 was true, then 0 an hour later. Anything computed from a
    display is a claim about the LISTING, not the data.
  * a character slice lands mid-token: `role:alice` rendered as `ro…`, which
    reads as a tag named `ro` rather than as a marker.

The fix drops WHOLE tags and states the count. Both directions pinned: nothing
is elided when it fits, and what IS elided is counted, never sliced.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentbus_client.cli import _format_tags


def test_short_tag_list_is_untouched():
    """Known-positive: the eliding path must not fire when everything fits."""
    out = _format_tags({"team:hive": "", "skill:py": ""}, limit=40)
    assert out == "skill:py,team:hive"
    assert "more" not in out


def test_no_bare_ellipsis_anywhere():
    labels = {f"tag{i}:value-that-is-long": "" for i in range(12)}
    out = _format_tags(labels, limit=40)
    assert "…" not in out
    assert "..." not in out


def test_elision_never_splits_a_token():
    """The `ro…` failure: every rendered tag must be a WHOLE tag."""
    labels = {"demo:agentbus": "", "film:the-hive": "", "role:alice": "", "team:hive": ""}
    out = _format_tags(labels, limit=40)
    body, _, suffix = out.partition(" +")
    assert suffix, "this input must elide, or the test proves nothing"
    for token in body.split(","):
        assert token in labels or any(token == f"{k}={v}" for k, v in labels.items()), (
            f"{token!r} is a fragment, not a whole tag"
        )


def test_elision_states_how_many_were_dropped():
    labels = {f"k{i}:v": "" for i in range(10)}
    out = _format_tags(labels, limit=40)
    body, _, suffix = out.partition(" +")
    dropped = int(suffix.split()[0])
    kept = len(body.split(","))
    assert kept + dropped == 10, f"count must reconcile: {kept} kept + {dropped} dropped != 10"


def test_output_never_exceeds_the_limit_including_its_own_marker():
    for count in range(1, 30):
        labels = {f"namespace{i}:some-value": "" for i in range(count)}
        out = _format_tags(labels, limit=40)
        assert len(out) <= 40, f"{count} tags -> {len(out)} chars: {out!r}"


def test_a_single_oversized_tag_reports_a_count_not_a_fragment():
    out = _format_tags({"a" * 100: ""}, limit=40)
    assert out == "+1 more"
    assert "aaa" not in out


def test_the_team_hive_regression_that_started_this():
    """frontend's exact case: team: sorts LATE, so a naive cut hides it — and
    the reader cannot tell. It may still be elided (that is a display), but the
    output must SAY it, so nothing computed from it can silently undercount."""
    labels = {
        "demo:agentbus": "",
        "film:the-hive": "",
        "role:alice": "",
        "skill:python": "",
        "team:hive": "",
    }
    out = _format_tags(labels, limit=40)
    if "team:hive" not in out:
        assert "more" in out, "an elided team: tag must leave a countable marker"
