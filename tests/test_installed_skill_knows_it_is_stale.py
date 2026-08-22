"""#196: an installed SKILL.md could not tell it was stale.

`agentbus setup` fetches the served skill, compares it, and reports "updated" or
"current". Nothing else did. So an installed copy was only ever as fresh as the
last setup run, and the only way to find out was to run setup again and watch
which word it printed — which means the answer was only available to someone who
already suspected the problem.

The evidence that this is real rather than theoretical is this very host, the
one that maintains the skill:

    skill: STALE — installed sha256 d9efb691352b != served 2bb95f5d785b
                   (9794 vs 54046 bytes) — refresh: agentbus setup claude

Nine kilobytes installed against fifty-four served. Every agent wired before the
encryption and tags work is carrying a comparable copy.

A DELIBERATE DEVIATION FROM THE TICKET'S WORDING, recorded here because the next
reader will check: the ticket says the skill file shall carry a version or
content hash. It carries neither. The comparison is over the BYTES, because a
stamp inside the file is a second source of truth an editor can forget to bump —
this repo already runs a pre-commit guard because exactly that happened between
the served skill and the plugin's copy. A hash of the bytes cannot drift from
the bytes.

The price of that choice is that the check needs the network, which makes the
`unknown` state the one that matters most: it must never be reported as
`current`, or the check becomes a thing that cannot fail.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentbus_client import onboarding


class _Resp:
    def __init__(self, status: int, content: bytes) -> None:
        self.status_code = status
        self.content = content


@pytest.fixture()
def skill(tmp_path, monkeypatch):
    """A fake ~/.claude/skills/agentbus/SKILL.md the test controls."""
    home = tmp_path
    (home / ".claude" / "skills" / "agentbus").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    return home / ".claude" / "skills" / "agentbus" / "SKILL.md"


def _serve(monkeypatch, status: int = 200, body: bytes = b"served"):
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *_a, **_k: _Resp(status, body))


def _raise(monkeypatch, exc: Exception):
    import httpx

    def boom(*_a, **_k):
        raise exc

    monkeypatch.setattr(httpx, "get", boom)


def test_identical_bytes_are_current(skill, monkeypatch) -> None:
    """THE KNOWN-POSITIVE. Without it, a function that returned 'stale'
    unconditionally would satisfy every other case here and would nag every
    agent forever — which is how a warning stops being read."""
    skill.write_bytes(b"served")
    _serve(monkeypatch, body=b"served")
    state, _ = onboarding.skill_state("https://x")
    assert state == "current"


def test_different_bytes_are_stale_and_name_the_refresh_command(skill, monkeypatch) -> None:
    """'stale' with no remedy leaves the reader knowing they have a problem
    and not what to type. The command NAMED must actually work in the
    scenario the warning appears in — peer agentbus-ui-c760a1 (thread
    01M06Q4Y282JDK23NV92WH6DJP) hit exactly the case where the old hint
    said `agentbus setup claude` but setup refused because the operator's
    cwd's repo fingerprint didn't match the one on file. `refresh-skill`
    is skill-only and skips the registration guard."""
    skill.write_bytes(b"old and short")
    _serve(monkeypatch, body=b"the much longer current skill")
    state, detail = onboarding.skill_state("https://x")
    assert state == "stale"
    assert "agentbus refresh-skill" in detail
    assert "13 vs 29 bytes" in detail, detail


def test_a_missing_skill_is_missing_not_stale(skill, monkeypatch) -> None:
    """Never installed and installed-but-old need different actions, and
    collapsing them would tell a fresh machine to 'refresh' something it does
    not have."""
    _serve(monkeypatch, body=b"served")
    state, detail = onboarding.skill_state("https://x")
    assert state == "missing"
    assert "agentbus setup claude" in detail


def test_an_unreachable_server_is_UNKNOWN_and_never_current(skill, monkeypatch) -> None:
    """THE ONE THAT MATTERS. The check needs the network; a version of it that
    fell back to 'current' when it could not ask would be green forever and
    would be exactly the silent staleness this ticket exists to end."""
    skill.write_bytes(b"anything")
    _raise(monkeypatch, OSError("no route to host"))
    state, detail = onboarding.skill_state("https://x")
    assert state == "unknown"
    assert "NOT checked" in detail


def test_a_non_200_is_UNKNOWN_and_never_current(skill, monkeypatch) -> None:
    """A 502 from a proxy returns a body. Comparing against it would report a
    perfectly good skill as stale, and against an empty body as stale forever."""
    skill.write_bytes(b"anything")
    _serve(monkeypatch, status=502, body=b"<html>bad gateway</html>")
    state, _ = onboarding.skill_state("https://x")
    assert state == "unknown"


def test_doctor_reports_the_skill_and_a_stale_one_is_not_a_clean_bill(skill) -> None:
    """A doctor line nobody prints is #94's F-07 again: a function that works
    and is called from nowhere. And printing STALE while still exiting clean
    would make the line decorative."""
    from agentbus_client import cli

    src = inspect.getsource(cli.cmd_doctor)
    assert "skill_state" in src
    branch = src[src.index("skill_state") :]
    assert 'state == "stale"' in branch
    assert "ok = False" in branch


def test_setup_still_says_when_the_skill_was_already_current(skill) -> None:
    """The ticket's third line — setup must not rewrite silently. This was
    ALREADY true before #196; asserted rather than assumed, because a later
    refactor that dropped the 'current' branch would leave a no-op looking
    identical to an update, which is the failure the line names."""
    src = inspect.getsource(onboarding._setup_claude)
    assert 'report.append("skill: current")' in src
    assert 'report.append(f"skill: updated{noted}")' in src
