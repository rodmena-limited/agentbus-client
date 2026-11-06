"""#93 items 9, 10, #94 items 7, 12: a tool or key that fails mid-setup is reported plainly."""

from __future__ import annotations

import argparse
import stat
import subprocess
from typing import TYPE_CHECKING, Any

import pytest

from agentbus_client import sealing
from agentbus_client.onboarding import _claude_setup, _signin

if TYPE_CHECKING:
    from pathlib import Path

SELF_DELETING_CLAUDE = """#!/bin/sh
echo "$@" >> "$FAKE_CLAUDE_LOG"
case "$*" in
  "mcp get agentbus") echo "Local config"; rm -f "$0"; exit 0;;
esac
exit 0
"""


def _write_claude(tmp_path: Path, body: str) -> Path:
    fake = tmp_path / "claude"
    fake.write_text(body)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    return fake


def test_add_with_a_vanished_binary_reports_failure_for_the_helper_path(tmp_path: Path) -> None:
    report: list[str] = []
    added = _claude_setup._add_mcp_entry(
        str(tmp_path / "gone"), "https://x/mcp", "ab_sk_alpha", report
    )
    assert added is False
    assert report == []


def test_add_that_times_out_reports_failure_for_the_helper_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timed_out(*a: Any, **k: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=120)

    monkeypatch.setattr(subprocess, "run", timed_out)
    report: list[str] = []
    assert _claude_setup._add_mcp_entry("claude", "https://x/mcp", "k", report) is False
    assert report == []


def test_add_that_exits_zero_is_wired(tmp_path: Path) -> None:
    fake = _write_claude(tmp_path, "#!/bin/sh\nexit 0\n")
    report: list[str] = []
    assert _claude_setup._add_mcp_entry(str(fake), "https://x/mcp", "k", report) is True
    assert report == ["mcp: wired at project scope — restart the session to connect"]


def test_add_that_reports_already_exists_is_wired(tmp_path: Path) -> None:
    fake = _write_claude(tmp_path, "#!/bin/sh\necho already exists\nexit 1\n")
    report: list[str] = []
    assert _claude_setup._add_mcp_entry(str(fake), "https://x/mcp", "k", report) is True


def test_refresh_with_a_vanished_binary_is_reported(tmp_path: Path) -> None:
    report: list[str] = []
    _claude_setup._refresh_mcp_entry(str(tmp_path / "gone"), "https://x/mcp", "k", report)
    assert report == ["mcp: entry exists but could not be refreshed — restart and re-run setup"]


def test_refresh_that_exits_zero_is_reported(tmp_path: Path) -> None:
    fake = _write_claude(tmp_path, "#!/bin/sh\nexit 0\n")
    report: list[str] = []
    _claude_setup._refresh_mcp_entry(str(fake), "https://x/mcp", "k", report)
    assert report == ["mcp: refreshed with the current key — restart the session to apply"]


def test_a_binary_lost_between_get_and_remove_reaches_the_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real subprocess path: `mcp get` succeeds, then `mcp remove` hits a missing binary."""
    fake = _write_claude(tmp_path, SELF_DELETING_CLAUDE)
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setattr("shutil.which", lambda name: str(fake))
    monkeypatch.setattr(_claude_setup, "_agent_key", lambda name: "ab_sk_alpha")
    report: list[str] = []
    _claude_setup._wire_mcp("alpha", "https://example.invalid", report)
    assert log.read_text().splitlines() == ["mcp get agentbus"]
    assert report == ["mcp: entry exists but could not be refreshed — restart and re-run setup"]


def test_a_binary_lost_before_add_falls_back_to_the_helper_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: str(tmp_path / "gone"))
    monkeypatch.setattr(_claude_setup, "_agent_key", lambda name: "ab_sk_alpha")
    monkeypatch.setattr(_claude_setup, "_keys_dir", lambda: tmp_path / "keys")
    report: list[str] = []
    _claude_setup._wire_mcp("alpha", "https://example.invalid", report)
    helper = tmp_path / "keys" / "alpha-mcp-add.sh"
    assert report == [f"mcp: run once in the project, then restart: sh {helper}"]
    assert stat.S_IMODE(helper.stat().st_mode) == 0o700
    assert "Authorization: Bearer ab_sk_alpha" in helper.read_text()


class _Bus:
    base_url = "https://example.invalid"

    def __init__(self, *, encrypted: bool = True, agent: str = "alpha") -> None:
        self.agent = agent
        self.encrypted = encrypted
        self.refuse_publish = False
        self.posted: list[str] = []

    def whoami(self) -> dict[str, Any]:
        return {
            "key": {"scope": "send", "bound_agents": [self.agent]},
            "workspace": {"slug": "w", "id": "1"},
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if method == "GET":
            return {"encrypted": self.encrypted}
        self.posted.append(path)
        if self.refuse_publish:
            msg = "publish refused"
            raise RuntimeError(msg)
        return {"fingerprint": "fp"}


@pytest.fixture
def signin_box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    cfg = tmp_path / "cfg"
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(cfg))
    bus = _Bus()
    monkeypatch.setattr(_signin, "AgentBus", lambda **kwargs: bus)
    return {"bus": bus, "cfg": cfg, "key_file": cfg / "keys" / "alpha.env"}


def _signin_args() -> argparse.Namespace:
    return argparse.Namespace(key="ab_sk_alpha", base_url="https://example.invalid")


def test_a_healthy_encrypted_signin_stores_the_key_and_publishes(
    signin_box: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _signin.cmd_signin(_signin_args()) == 0
    assert "export AGENTBUS_API_KEY=ab_sk_alpha" in signin_box["key_file"].read_text()
    assert signin_box["bus"].posted == ["/v1/agents/alpha/pubkey"]
    assert "sealing key ready" in capsys.readouterr().out


def test_an_unusable_sealing_key_path_still_stores_the_credential(
    signin_box: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """A real IsADirectoryError from the key file. The credential lands and the run exits 1."""
    sealing.key_path("alpha").mkdir(parents=True)
    assert _signin.cmd_signin(_signin_args()) == 1
    out = capsys.readouterr().out
    assert "export AGENTBUS_API_KEY=ab_sk_alpha" in signin_box["key_file"].read_text()
    assert signin_box["bus"].posted == []
    assert "the sealing key could not be prepared" in out
    assert str(sealing.key_path("alpha")) in out
    assert "agentbus keys rotate" in out


def test_a_sealing_key_file_that_cannot_be_parsed_still_stores_the_credential(
    signin_box: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    path = sealing.key_path("alpha")
    path.parent.mkdir(parents=True)
    path.write_text("not an age key\n")
    assert _signin.cmd_signin(_signin_args()) == 1
    assert "export AGENTBUS_API_KEY=ab_sk_alpha" in signin_box["key_file"].read_text()
    assert path.read_text() == "not an age key\n"
    assert "the sealing key could not be prepared" in capsys.readouterr().out


def test_an_unencrypted_workspace_needs_no_sealing_key(signin_box: dict[str, Any]) -> None:
    signin_box["bus"].encrypted = False
    assert _signin.cmd_signin(_signin_args()) == 0
    assert signin_box["key_file"].exists()
    assert not sealing.key_path("alpha").exists()


def _remove_then_add_fake(tmp_path: Path, *, remove_rc: int, add_rc: int) -> Path:
    return _write_claude(
        tmp_path,
        "#!/bin/sh\n"
        'echo "$@" >> "$FAKE_CLAUDE_LOG"\n'
        'case "$1 $2" in\n'
        '  "mcp get") echo "Local config"; exit 0;;\n'
        f'  "mcp remove") exit {remove_rc};;\n'
        f'  "mcp add") exit {add_rc};;\n'
        "esac\n"
        "exit 0\n",
    )


REMOVED_NOT_ADDED = (
    "mcp: the old entry was removed and the new one could not be added, "
    "so this project has no agentbus MCP entry right now"
)


def test_refresh_that_removed_the_entry_and_failed_to_add_says_the_entry_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _remove_then_add_fake(tmp_path, remove_rc=0, add_rc=1)
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "calls.log"))
    report: list[str] = []
    refreshed = _claude_setup._refresh_mcp_entry(str(fake), "https://x/mcp", "k", report)
    assert refreshed is False
    assert report == [REMOVED_NOT_ADDED]


def test_wiring_writes_the_helper_when_refresh_lost_the_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _remove_then_add_fake(tmp_path, remove_rc=0, add_rc=1)
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setattr("shutil.which", lambda name: str(fake))
    monkeypatch.setattr(_claude_setup, "_agent_key", lambda name: "ab_sk_alpha")
    monkeypatch.setattr(_claude_setup, "_keys_dir", lambda: tmp_path / "keys")
    report: list[str] = []
    _claude_setup._wire_mcp("alpha", "https://example.invalid", report)
    helper = tmp_path / "keys" / "alpha-mcp-add.sh"
    calls = log.read_text().splitlines()
    assert [call.split()[:2] for call in calls] == [
        ["mcp", "get"],
        ["mcp", "remove"],
        ["mcp", "add"],
    ]
    assert report == [REMOVED_NOT_ADDED, f"mcp: run once in the project, then restart: sh {helper}"]
    assert stat.S_IMODE(helper.stat().st_mode) == 0o700
    assert "Authorization: Bearer ab_sk_alpha" in helper.read_text()


def test_wiring_writes_no_helper_when_the_refresh_succeeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _remove_then_add_fake(tmp_path, remove_rc=0, add_rc=0)
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "calls.log"))
    monkeypatch.setattr("shutil.which", lambda name: str(fake))
    monkeypatch.setattr(_claude_setup, "_agent_key", lambda name: "ab_sk_alpha")
    monkeypatch.setattr(_claude_setup, "_keys_dir", lambda: tmp_path / "keys")
    report: list[str] = []
    _claude_setup._wire_mcp("alpha", "https://example.invalid", report)
    assert report == ["mcp: refreshed with the current key — restart the session to apply"]
    assert not (tmp_path / "keys" / "alpha-mcp-add.sh").exists()


def test_wiring_writes_no_helper_when_the_entry_is_still_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _remove_then_add_fake(tmp_path, remove_rc=1, add_rc=1)
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(tmp_path / "calls.log"))
    monkeypatch.setattr("shutil.which", lambda name: str(fake))
    monkeypatch.setattr(_claude_setup, "_agent_key", lambda name: "ab_sk_alpha")
    monkeypatch.setattr(_claude_setup, "_keys_dir", lambda: tmp_path / "keys")
    report: list[str] = []
    _claude_setup._wire_mcp("alpha", "https://example.invalid", report)
    assert report == ["mcp: entry exists but could not be refreshed — restart and re-run setup"]
    assert not (tmp_path / "keys" / "alpha-mcp-add.sh").exists()


def test_a_public_key_the_server_refuses_fails_the_signin_and_keeps_the_credential(
    signin_box: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    signin_box["bus"].refuse_publish = True
    assert _signin.cmd_signin(_signin_args()) == 1
    out = capsys.readouterr().out
    assert "export AGENTBUS_API_KEY=ab_sk_alpha" in signin_box["key_file"].read_text()
    assert signin_box["bus"].posted == ["/v1/agents/alpha/pubkey"]
    assert "the public key could not be registered: publish refused" in out
    assert "Run `agentbus signin <key>` again" in out
    assert "sealing key ready" not in out


def test_a_second_signin_after_the_server_recovers_exits_zero(
    signin_box: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    signin_box["bus"].refuse_publish = True
    assert _signin.cmd_signin(_signin_args()) == 1
    signin_box["bus"].refuse_publish = False
    assert _signin.cmd_signin(_signin_args()) == 0
    assert signin_box["bus"].posted == ["/v1/agents/alpha/pubkey"] * 2
    assert "sealing key ready" in capsys.readouterr().out
