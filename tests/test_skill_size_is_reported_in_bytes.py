"""#51: `refresh-skill` must report BYTES, because that is the word it prints.

Reported by crypto-trader-manager-6a3048:

    $ agentbus refresh-skill
    skill: UPDATED — 62982 bytes, previous saved to SKILL.md.bak
    $ wc -c < ~/.claude/skills/agentbus/SKILL.md
    63281

`len(resp.text)` counts CHARACTERS. `wc -c` counts bytes. The served skill is
full of em-dashes and arrows, so the two disagreed by 299 — and a reader
comparing them cannot tell a UNIT MISMATCH from a TRUNCATED DOWNLOAD.

That is the whole reason it matters at a size this small: the number exists so
somebody can check the install, and a check that disagrees with the obvious way
of verifying it teaches the reader to distrust the check.
"""

from __future__ import annotations

import pytest

from agentbus_client.onboarding import _skill

# Deliberately multi-byte: this is what the served skill is made of.
BODY = "# AgentBus — the skill\n" + ("an em-dash — and an arrow →\n" * 60)


class _Resp:
    status_code = 200

    def __init__(self, text: str) -> None:
        self.text = text


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(_skill.Path, "home", staticmethod(lambda: tmp_path))
    return tmp_path


def _install(monkeypatch, body: str):
    # `httpx` is imported INSIDE refresh_skill, so patch the library itself
    # rather than a module attribute that does not exist until call time.
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp(body))
    return _skill.refresh_skill()


def test_the_reported_size_matches_the_bytes_on_disk(home, monkeypatch):
    """THE REGRESSION: the number must equal what `wc -c` would say."""
    _state, detail = _install(monkeypatch, BODY)
    on_disk = (home / ".claude" / "skills" / "agentbus" / "SKILL.md").stat().st_size
    reported = int(detail.split()[0])
    assert reported == on_disk, f"reported {reported}, file is {on_disk}"


def test_the_number_differs_from_the_character_count(home, monkeypatch):
    """KNOWN-POSITIVE for the fixture itself.

    If the body were pure ASCII, characters and bytes would agree and the test
    above would pass against the OLD code — the bug would be invisible. This
    asserts the stimulus actually distinguishes them.
    """
    _state, detail = _install(monkeypatch, BODY)
    reported = int(detail.split()[0])
    assert reported != len(BODY), "fixture is not multi-byte; the test cannot fail"
    assert reported == len(BODY.encode("utf-8"))


def test_a_suspiciously_small_body_is_still_refused(home, monkeypatch):
    """Known-negative: the guard must survive the unit change."""
    state, _detail = _install(monkeypatch, "tiny")
    assert state == "unreachable"


def test_an_unchanged_skill_reports_the_same_unit(home, monkeypatch):
    _install(monkeypatch, BODY)
    state, detail = _install(monkeypatch, BODY)
    assert state == "current"
    assert int(detail.split()[0]) == len(BODY.encode("utf-8"))
