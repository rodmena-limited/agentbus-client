"""`agentbus refresh-skill` and the extracted `refresh_skill()` helper.

Reported by peer agentbus-ui-c760a1 (thread 01M06Q4Y282JDK23NV92WH6DJP):
`agentbus doctor` printed "refresh: agentbus setup claude" for a stale
skill, but setup refuses when the cwd's repo fingerprint doesn't match
the one the server has for that agent. That guard is protective and
should stay; the docs refresh needed a path that doesn't go through
registration at all.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import agentbus_client.cli as cli_module
from agentbus_client import onboarding


class _Resp:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text


def _args(**over):
    base = {"agent": None, "json": False}
    base.update(over)
    return argparse.Namespace(**base)


# --------------------------------------------------------------- helper


def test_refresh_installs_fresh_when_no_skill_present(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    served = "# served skill body\n" + "x" * 1000

    with patch("httpx.get", return_value=_Resp(200, served)):
        state, detail = onboarding.refresh_skill(base_url="https://x")

    assert state == "installed"
    skill = tmp_path / ".claude" / "skills" / "agentbus" / "SKILL.md"
    assert skill.exists()
    assert skill.read_text() == served
    assert "fresh install" in detail


def test_refresh_reports_current_when_installed_matches_served(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    served = "# served skill body\n" + "y" * 1000
    skill = tmp_path / ".claude" / "skills" / "agentbus" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(served)

    with patch("httpx.get", return_value=_Resp(200, served)):
        state, detail = onboarding.refresh_skill(base_url="https://x")

    assert state == "current"
    assert "already matches" in detail


def test_refresh_backs_up_existing_before_overwriting(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    old = "# old body\n" + "a" * 1000
    served = "# served body — NEW\n" + "b" * 1000
    skill = tmp_path / ".claude" / "skills" / "agentbus" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text(old)

    with patch("httpx.get", return_value=_Resp(200, served)):
        state, detail = onboarding.refresh_skill(base_url="https://x")

    assert state == "updated"
    assert skill.read_text() == served
    bak = skill.with_suffix(".md.bak")
    assert bak.exists()
    assert bak.read_text() == old
    assert "SKILL.md.bak" in detail


def test_refresh_refuses_small_body(tmp_path, monkeypatch):
    """Reject a truncated body — the D4 install-guard reasoning: a nearly-
    empty file is worse than no file, because it advertises freshness."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with patch("httpx.get", return_value=_Resp(200, "hi")):
        state, detail = onboarding.refresh_skill(base_url="https://x")
    assert state == "unreachable"
    assert "refusing to install" in detail


def test_refresh_reports_unreachable_on_non_200(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with patch("httpx.get", return_value=_Resp(502, "bad gateway")):
        state, detail = onboarding.refresh_skill(base_url="https://x")
    assert state == "unreachable"
    assert "502" in detail


def test_refresh_reports_unreachable_on_transport_error(tmp_path, monkeypatch):
    import httpx

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with patch("httpx.get", side_effect=httpx.ConnectError("no route")):
        state, detail = onboarding.refresh_skill(base_url="https://x")
    assert state == "unreachable"
    assert "could not fetch" in detail


# --------------------------------------------------------------- CLI verb


def test_cli_refresh_skill_exits_zero_on_updated(tmp_path, monkeypatch, capsys):
    """The subcommand does NOT need an acting agent — that's the whole point
    (setup couldn't run for the reporter's agent, refresh-skill can)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    served = "# served skill\n" + "z" * 1000
    with patch("httpx.get", return_value=_Resp(200, served)):
        rc = cli_module.cmd_refresh_skill(_args())
    assert rc == 0
    out = capsys.readouterr().out
    assert "INSTALLED" in out or "UPDATED" in out


def test_cli_refresh_skill_exits_nonzero_on_unreachable(tmp_path, monkeypatch, capsys):
    """A stale skill on a broken network must not silently be reported as
    healthy — the doctor recipe that led here would then be lying too."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    with patch("httpx.get", return_value=_Resp(500, "err")):
        rc = cli_module.cmd_refresh_skill(_args())
    assert rc == 1


def test_cli_refresh_skill_json_output(tmp_path, monkeypatch, capsys):
    """Consumers piping to jq expect {state, detail} — mirror the pattern
    of every other --json emitter in the CLI."""
    import json as _json

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    served = "# served\n" + "q" * 1000
    with patch("httpx.get", return_value=_Resp(200, served)):
        rc = cli_module.cmd_refresh_skill(_args(json=True))
    assert rc == 0
    data = _json.loads(capsys.readouterr().out)
    assert data["state"] in ("installed", "updated", "current")
    assert "detail" in data


# --------------------------------------------------------------- doctor hint


def test_doctor_hint_points_at_refresh_skill_not_setup(tmp_path, monkeypatch):
    """The specific defect the peer reported: doctor's stale-skill hint used
    to name `agentbus setup claude`, which refuses in exactly the scenario
    where the hint appears. The new hint must name a command that works."""
    import httpx

    class _R:
        def __init__(self, code, content):
            self.status_code = code
            self.content = content

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    skill = tmp_path / ".claude" / "skills" / "agentbus" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_bytes(b"old-installed")

    # skill_state reads httpx.get(...).content (bytes), not .text
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _R(200, b"different served body"))
    state, detail = onboarding.skill_state(base_url="https://x")

    assert state == "stale"
    assert "refresh-skill" in detail
    assert "setup claude" not in detail
