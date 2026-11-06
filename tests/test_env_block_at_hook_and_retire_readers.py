"""#94 item 5: the hook and retire readers treat a settings file of the wrong shape as no identity.

Each reader used to raise AttributeError on a non-object `env`, or on a non-object file, because
their handlers cover OSError and ValueError only. The import pin keeps the shared reader out of
the onboarding package, which costs about 200 modules on every hook.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import agentbus_client
from agentbus_client.cli import _service
from agentbus_client.hooks import _identity, _session

WRONG_SHAPES = [
    '{"env": "x"}',
    '{"env": ["a"]}',
    '{"env": 5}',
    '{"env": true}',
    '{"env": null}',
    '{"env": {"AGENTBUS_AGENT": 5}}',
    '{"env": {"AGENTBUS_AGENT": ["alpha"]}}',
    '{"env": {"AGENTBUS_AGENT": null}}',
    '{"env": {"AGENTBUS_AGENT": ""}}',
    "[]",
    "null",
    "5",
    '"x"',
]
DECLARED = '{"env": {"AGENTBUS_AGENT": "alpha"}}'
HEAVY_MODULES = """
import sys
heavy = ("agentbus_client.onboarding", "agentbus_client.client", "httpx")
print(*sorted(name for name in sys.modules if name.startswith(heavy)))
"""


def _checkout(root: Path, settings: str) -> None:
    (root / ".claude").mkdir()
    (root / ".claude" / "settings.local.json").write_text(settings)


@pytest.mark.parametrize("settings", WRONG_SHAPES)
def test_the_shared_reader_finds_no_agent_in_a_wrong_shape(settings: str) -> None:
    from agentbus_client._settings_env import _settings_agent

    assert _settings_agent(json.loads(settings)) is None


def test_the_shared_reader_returns_a_declared_agent() -> None:
    from agentbus_client._settings_env import _settings_agent

    assert _settings_agent(json.loads(DECLARED)) == "alpha"


@pytest.mark.parametrize("settings", WRONG_SHAPES)
def test_the_hook_reader_finds_no_agent_in_a_wrong_shape(tmp_path: Path, settings: str) -> None:
    _checkout(tmp_path, settings)
    assert _identity._read_declared_agent(tmp_path) == ""


def test_the_hook_reader_returns_a_declared_agent(tmp_path: Path) -> None:
    _checkout(tmp_path, DECLARED)
    assert _identity._read_declared_agent(tmp_path) == "alpha"


def test_the_hook_reader_prefers_the_agent_file_over_a_wrong_shape(tmp_path: Path) -> None:
    _checkout(tmp_path, '{"env": "x"}')
    (tmp_path / ".agentbus").mkdir()
    (tmp_path / ".agentbus" / "agent").write_text("beta\n")
    assert _identity._read_declared_agent(tmp_path) == "beta"


def _override_warning(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    monkeypatch.setattr(_identity, "_repo_root", lambda: root)
    monkeypatch.setenv("AGENTBUS_AGENT", "other")
    _session._warn_if_env_overrides_this_checkout("other")
    return capsys.readouterr().out


@pytest.mark.parametrize("settings", WRONG_SHAPES)
def test_the_session_warning_stays_quiet_on_a_wrong_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: str,
) -> None:
    _checkout(tmp_path, settings)
    assert _override_warning(tmp_path, monkeypatch, capsys) == ""


def test_the_session_warning_names_a_declared_agent_that_the_env_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _checkout(tmp_path, DECLARED)
    out = _override_warning(tmp_path, monkeypatch, capsys)
    assert "this checkout is wired to 'alpha'" in out
    assert "says 'other'" in out


def test_the_session_warning_strips_a_declared_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _checkout(tmp_path, '{"env": {"AGENTBUS_AGENT": " alpha "}}')
    assert "wired to 'alpha'," in _override_warning(tmp_path, monkeypatch, capsys)


def _retire_warning(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> str:
    monkeypatch.setattr("agentbus_client.onboarding._git_root_or_none", lambda: root)
    _service._warn_local_wiring_will_revive("alpha")
    return capsys.readouterr().out


@pytest.mark.parametrize("settings", WRONG_SHAPES)
def test_the_retire_warning_stays_quiet_on_a_wrong_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: str,
) -> None:
    _checkout(tmp_path, settings)
    assert _retire_warning(tmp_path, monkeypatch, capsys) == ""


def test_the_retire_warning_names_a_settings_file_that_declares_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _checkout(tmp_path, DECLARED)
    out = _retire_warning(tmp_path, monkeypatch, capsys)
    assert "still DECLARES 'alpha'" in out
    assert str(tmp_path / ".claude" / "settings.local.json") in out


def test_the_retire_warning_stays_quiet_for_another_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _checkout(tmp_path, '{"env": {"AGENTBUS_AGENT": "beta"}}')
    assert _retire_warning(tmp_path, monkeypatch, capsys) == ""


def _modules_after(statement: str) -> list[str]:
    source = Path(agentbus_client.__file__).parents[1]
    done = subprocess.run(
        [sys.executable, "-c", f"{statement}\n{HEAVY_MODULES}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        env={**os.environ, "PYTHONPATH": str(source)},
    )
    return done.stdout.split()


def test_importing_the_hooks_loads_neither_onboarding_nor_the_client() -> None:
    assert _modules_after("import agentbus_client.hooks.claude_code") == []


def test_the_import_probe_goes_red_on_the_onboarding_package() -> None:
    loaded = _modules_after("import agentbus_client.onboarding._settings_shape")
    assert "agentbus_client.onboarding" in loaded
    assert "agentbus_client.client" in loaded
