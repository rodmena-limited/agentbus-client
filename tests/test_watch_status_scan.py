"""watch-status must not read pidfile-absence as checked-and-dead (#160), and
the bare CLI must reverse the harness's worktree misinjection (#161).

#160  With no pidfile, watch-status had NOTHING to check and still printed the
      same words as a confirmed-dead process — absence of evidence worded as
      evidence of absence (a live watcher whose pidfile vanished was reported
      NOT running, and everything downstream trusted it). Now: a process scan
      (`ps -axwwo`, the #117 FreeBSD-safe form) runs before the verdict, and
      the verdict says WHAT was checked either way.

#161  Hooks (#130), monitor (#129) and credential adoption (#131) all reverse
      the harness's injection of the MAIN worktree's identity into a linked
      worktree's env — but the bare CLI kept trusting $AGENTBUS_AGENT, which
      is how one session spent an hour sending as another agent. The reversal
      fires ONLY on the narrow signature; a deliberate export still wins (#90).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentbus_client import cli
from agentbus_client.hooks import claude_code


def _ps_returning(output: str):
    def fake_run(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(stdout=output, returncode=0)

    return fake_run


class TestScanWatchProcess:
    def test_finds_a_live_watcher(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ps = "  123 /usr/bin/python3 /usr/local/bin/agentbus watch --agent red9-x --exec f\n"
        monkeypatch.setattr(subprocess, "run", _ps_returning(ps))
        assert cli._scan_watch_process("red9-x") == 123

    def test_agent_name_prefix_does_not_match(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """`--agent red9` must not claim `--agent red9-x`'s watcher."""
        ps = "  123 python3 agentbus watch --agent red9-x --exec f\n"
        monkeypatch.setattr(subprocess, "run", _ps_returning(ps))
        assert cli._scan_watch_process("red9") is None

    def test_watch_status_itself_is_not_a_watcher(self, monkeypatch: pytest.MonkeyPatch) -> None:
        ps = "  777 python3 agentbus watch-status --agent red9-x\n"
        monkeypatch.setattr(subprocess, "run", _ps_returning(ps))
        assert cli._scan_watch_process("red9-x") is None

    def test_no_processes_means_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(subprocess, "run", _ps_returning("  1 /sbin/init\n"))
        assert cli._scan_watch_process("red9-x") is None

    def test_ps_failure_means_none_not_crash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*_args: object, **_kwargs: object) -> None:
            raise OSError("no ps here")

        monkeypatch.setattr(subprocess, "run", boom)
        assert cli._scan_watch_process("red9-x") is None


class TestEnvAgentReversal:
    def test_injected_main_identity_is_reversed_with_notice(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        monkeypatch.setenv("AGENTBUS_AGENT", "main-agent")
        monkeypatch.setattr(
            claude_code._identity,
            "_worktree_identity_bleed",
            lambda env: "worktree-agent" if env == "main-agent" else None,
        )
        assert cli._resolve_env_agent() == "worktree-agent"
        err = capsys.readouterr().err
        assert "#161" in err
        assert "worktree-agent" in err
        assert "main-agent" in err  # the notice names BOTH identities

    def test_deliberate_export_still_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """#90 intact: outside the bleed signature the env is the operator's
        word and must be obeyed silently."""
        monkeypatch.setenv("AGENTBUS_AGENT", "deliberate-export")
        monkeypatch.setattr(claude_code._identity, "_worktree_identity_bleed", lambda _env: None)
        assert cli._resolve_env_agent() == "deliberate-export"

    def test_no_env_resolves_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENTBUS_AGENT", raising=False)
        assert cli._resolve_env_agent() is None

    def test_reversed_agent_uses_its_own_stored_key(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """#161's credential half: the injected env key is the MAIN agent's
        bound key; after reversal the worktree agent's own key file must win."""
        captured: dict = {}

        class _CapturingBus:
            def __init__(self, api_key=None, agent=None, **_kw: object) -> None:
                captured["api_key"] = api_key
                captured["agent"] = agent

        monkeypatch.setenv("AGENTBUS_AGENT", "main-agent")
        monkeypatch.setenv("AGENTBUS_API_KEY", "ab_sk_" + "m" * 16 + "_mainkey")
        monkeypatch.setattr(
            claude_code._identity, "_worktree_identity_bleed", lambda _env: "worktree-agent"
        )
        monkeypatch.setattr(
            cli._common, "_key_for_agent", lambda _agent: "ab_sk_" + "w" * 16 + "_ownkey"
        )
        monkeypatch.setattr(cli._common, "AgentBus", _CapturingBus)
        args = SimpleNamespace(api_key=None, agent=None, base_url=None)
        cli._bus(args)
        assert captured["agent"] == "worktree-agent"
        assert captured["api_key"] == "ab_sk_" + "w" * 16 + "_ownkey"
        capsys.readouterr()  # drain the stderr notice

    def test_explicit_agent_flag_is_never_reversed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """--agent outranks everything; the reversal must not even be consulted."""

        def explode(_env: str) -> str:
            raise AssertionError("bleed check must not run for an explicit --agent")

        captured: dict = {}

        class _CapturingBus:
            def __init__(self, agent=None, **_kw: object) -> None:
                captured["agent"] = agent

        monkeypatch.setenv("AGENTBUS_AGENT", "main-agent")
        monkeypatch.setenv("AGENTBUS_API_KEY", "ab_sk_" + "m" * 16 + "_mainkey")
        monkeypatch.setattr(claude_code._identity, "_worktree_identity_bleed", explode)
        monkeypatch.setattr(cli._common, "AgentBus", _CapturingBus)
        args = SimpleNamespace(api_key=None, agent="chosen-one", base_url=None)
        cli._bus(args)
        assert captured["agent"] == "chosen-one"
