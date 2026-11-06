"""#93 items 3, 4, 8, #94 item 9: `doctor --wake` reports what it ran, and no more."""

from __future__ import annotations

import argparse
import json
from typing import TYPE_CHECKING, Any

import pytest

from agentbus_client.onboarding import _doctor, _doctor_agy, _paths

if TYPE_CHECKING:
    from pathlib import Path

STOP_CHAIN_PASSED = "The Stop re-waker passed its probe up to the harness boundary."
NOT_COMPARED = "These were not compared"


def _finish(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    loaded: str | None,
    installed: str | None,
) -> tuple[int, str]:
    monkeypatch.setattr(_doctor, "_running_watcher_version", lambda agent: loaded)
    monkeypatch.setattr(_doctor, "_installed_version", lambda: installed)
    rc = _doctor._finish_wake_report([], plugin_wake=True, agent="alpha")
    return rc, capsys.readouterr().out


@pytest.mark.parametrize(
    ("loaded", "installed"),
    [
        ("1.2.2", "0.0.0+source"),
        ("0.0.0+source", "1.2.2"),
        ("0.0.0+source", "0.0.0+source"),
        ("unknown", "1.2.2"),
        ("1.2.2", "unknown"),
        ("1.2.2", None),
    ],
)
def test_a_version_pair_that_cannot_be_compared_is_reported_as_not_compared(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    loaded: str,
    installed: str | None,
) -> None:
    """The watcher's version and the installed one are printed, and no match is claimed."""
    rc, out = _finish(monkeypatch, capsys, loaded, installed)
    assert rc == 0
    assert "matching what is installed" not in out
    assert NOT_COMPARED in out
    assert f"reports client {loaded}" in out
    assert f"the installed client reports {installed or 'no version'}" in out


def test_two_equal_release_versions_are_reported_as_matching(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out = _finish(monkeypatch, capsys, "1.2.2", "1.2.2")
    assert rc == 0
    assert "The running watcher reports client 1.2.2, matching what is installed." in out
    assert NOT_COMPARED not in out


def test_two_different_release_versions_still_refuse_the_proof(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out = _finish(monkeypatch, capsys, "1.2.2", "1.2.3")
    assert rc == 1
    assert "wake chain NOT PROVEN" in out
    assert "matching what is installed" not in out


def test_a_watcher_that_stamps_no_version_keeps_its_note(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    rc, out = _finish(monkeypatch, capsys, None, "1.2.2")
    assert rc == 0
    assert "does not report which client build it loaded" in out
    assert "matching what is installed" not in out


@pytest.fixture
def stop_chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A Claude checkout with a healthy Stop chain and no agentbus plugin."""
    cc = tmp_path / "cc"
    cc.mkdir()
    marker = {"hooks": [{"type": "command", "command": "agentbus-hook"}]}
    rewake = {"hooks": [{"type": "command", "command": "stop-rewake.sh"}]}
    settings = {
        "hooks": {"SessionStart": [marker], "UserPromptSubmit": [marker], "Stop": [rewake]},
    }
    (cc / "settings.json").write_text(json.dumps(settings))
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    script = cfg / "stop-rewake.sh"
    script.write_text("#!/bin/sh\n# agentbus-rewake-version: 4\n")
    script.chmod(0o755)

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cc))
    monkeypatch.setattr(_doctor, "_config_dir", lambda: cfg)
    monkeypatch.setattr(_doctor, "_resolve_agent_name", lambda explain=None: "alpha")
    monkeypatch.setattr(_doctor, "_agent_key", lambda name: "ab_sk_alpha")
    monkeypatch.setattr(_doctor, "_report_wake_channel", lambda key, base_url, name: None)
    monkeypatch.setattr(_doctor, "AgentBus", lambda **kwargs: object())
    monkeypatch.setattr("agentbus_client.onboarding._paths.agy_is_wired", lambda root: False)
    monkeypatch.setattr(_doctor, "_probe_rewake_cycle", lambda *a: [])
    return {"script": script, "settings": cc / "settings.json"}


def _wake(capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    rc = _doctor.doctor_wake(argparse.Namespace(base_url="https://example.invalid"))
    return rc, capsys.readouterr().out


def test_a_passing_stop_chain_says_so_and_the_missing_plugin_still_fails(
    stop_chain: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """What the probe proved is printed, the assumption is named, and the exit stays red."""
    rc, out = _wake(capsys)
    assert rc == 1
    assert STOP_CHAIN_PASSED in out
    assert "asyncRewake" in out
    assert "wake chain PROVEN" not in out
    assert out.index(STOP_CHAIN_PASSED) < out.index("FAIL: the agentbus plugin is not enabled")


def test_a_failed_probe_makes_no_claim_about_the_stop_chain(
    stop_chain: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(_doctor, "_probe_rewake_cycle", lambda *a: ["re-waker did not surface"])
    rc, out = _wake(capsys)
    assert rc == 1
    assert STOP_CHAIN_PASSED not in out
    assert "FAIL: re-waker did not surface" in out
    assert "FAIL: the agentbus plugin is not enabled" in out


def test_no_key_means_no_probe_and_no_claim_about_the_stop_chain(
    stop_chain: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def must_not_run(*a: Any) -> list[str]:
        raise AssertionError

    monkeypatch.setattr(_doctor, "_agent_key", lambda name: None)
    monkeypatch.setattr(_doctor, "_probe_rewake_cycle", must_not_run)
    rc, out = _wake(capsys)
    assert rc == 1
    assert STOP_CHAIN_PASSED not in out
    assert "FAIL: no readable key file for alpha" in out


def test_a_stale_script_makes_no_claim_about_the_stop_chain(
    stop_chain: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    stop_chain["script"].write_text("#!/bin/sh\n")
    rc, out = _wake(capsys)
    assert rc == 1
    assert STOP_CHAIN_PASSED not in out
    assert "stop-rewake.sh is STALE" in out


@pytest.mark.parametrize(
    "hooks",
    [
        None,
        ["x"],
        "x",
        {
            "Stop": None,
            "SessionStart": "x",
            "UserPromptSubmit": ["x", {"hooks": None}, {"hooks": ["y"]}],
        },
    ],
)
def test_a_malformed_hooks_block_reads_as_no_hooks(
    stop_chain: dict[str, Any], capsys: pytest.CaptureFixture[str], hooks: Any
) -> None:
    stop_chain["settings"].write_text(json.dumps({"hooks": hooks}))
    rc, out = _wake(capsys)
    assert rc == 1
    assert "[!!] passive hooks" in out
    assert "[!!] active trigger" in out


@pytest.mark.parametrize("enabled", [["agentbus@rodmena"], "agentbus@rodmena", 1, True])
def test_a_malformed_enabled_plugins_block_reads_as_not_enabled(
    stop_chain: dict[str, Any], capsys: pytest.CaptureFixture[str], enabled: Any
) -> None:
    stop_chain["settings"].write_text(json.dumps({"enabledPlugins": enabled}))
    rc, out = _wake(capsys)
    assert rc == 1
    assert "FAIL: the agentbus plugin is not enabled" in out


def _agy_hook(command: str) -> dict[str, Any]:
    return {"agentbus-wake": {"Stop": [{"command": command}]}}


@pytest.fixture
def agy_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    (repo / ".agentbus").mkdir(parents=True)
    (repo / ".agentbus" / "agent").write_text("alpha\n")
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AGY_CONFIG_HOME", str(tmp_path / "gemini"))
    _paths.agy_mark_wired(repo)
    _paths.agy_plugin_dir().mkdir(parents=True)
    return repo


def _write_agy_hooks(content: object) -> None:
    (_paths.agy_plugin_dir() / "hooks.json").write_text(json.dumps(content))


@pytest.mark.parametrize("printed", ["[1,2]", '"text"', "5", "null", "true"])
def test_a_hook_printing_json_that_is_not_an_object_is_a_plain_failure(
    agy_repo: Path, capsys: pytest.CaptureFixture[str], printed: str
) -> None:
    _write_agy_hooks(_agy_hook(f"printf '%s' '{printed}' # agy-stop"))
    rc = _doctor_agy.doctor_wake_agy("alpha", agy_repo)
    out = capsys.readouterr().out
    assert rc == 1
    assert f"FAIL: the hook printed JSON that is not an object: {printed}" in out
    assert "[ok] hook ran" not in out


def test_a_hook_printing_an_object_still_passes(
    agy_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_agy_hooks(_agy_hook("printf '%s' '{\"decision\":\"continue\"}' # agy-stop"))
    rc = _doctor_agy.doctor_wake_agy("alpha", agy_repo)
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "hook ran and answered agy: decision='continue'" in out


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ([], "does not hold a JSON object"),
        ("x", "does not hold a JSON object"),
        ({"agentbus-wake": None}, "carries no agy-stop Stop handler"),
        ({"agentbus-wake": []}, "carries no agy-stop Stop handler"),
        ({"agentbus-wake": {"Stop": "x"}}, "carries no agy-stop Stop handler"),
        ({"agentbus-wake": {"Stop": ["x", None]}}, "carries no agy-stop Stop handler"),
    ],
)
def test_a_hooks_file_of_the_wrong_shape_is_a_plain_failure(
    agy_repo: Path, capsys: pytest.CaptureFixture[str], content: object, reason: str
) -> None:
    _write_agy_hooks(content)
    rc = _doctor_agy.doctor_wake_agy("alpha", agy_repo)
    out = capsys.readouterr().out
    assert rc == 1
    assert reason in out


def test_a_hooks_file_that_is_not_text_is_a_plain_failure(
    agy_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (_paths.agy_plugin_dir() / "hooks.json").write_bytes(b"\xff\xfe\x00")
    rc = _doctor_agy.doctor_wake_agy("alpha", agy_repo)
    out = capsys.readouterr().out
    assert rc == 1
    assert "is not readable JSON" in out


@pytest.mark.parametrize(
    "state", ["[]", "null", "5", '{"client_version": 5}', '{"client_version": ["1.2.2"]}']
)
def test_a_watcher_state_file_of_the_wrong_shape_reports_no_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str
) -> None:
    monkeypatch.setattr(_doctor, "identity_config_dir", lambda: tmp_path)
    (tmp_path / "monitor-alpha-s1.json").write_text(state)
    assert _doctor._running_watcher_version("alpha") is None


@pytest.mark.parametrize(
    ("loaded", "installed"), [("1.2.2", "1.2.2"), ("1.2.2", "0.0.0+source"), (None, "1.2.2")]
)
def test_a_running_monitor_is_reported_as_a_process_and_nothing_more(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    loaded: str | None,
    installed: str | None,
) -> None:
    rc, out = _finish(monkeypatch, capsys, loaded, installed)
    assert rc == 0
    assert "wake chain: the plugin monitor process is running." in out
    assert "did not test its stream or deliver a message" in out
    assert "PROVEN" not in out
    assert "live stream" not in out
    assert "13-19s" not in out


def test_the_monitor_line_gives_the_pid_and_claims_no_stream(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_doctor, "_monitor_pids", lambda name: [4242])
    monkeypatch.setattr(_doctor, "_running_watcher_version", lambda agent: "1.2.2")
    monkeypatch.setattr(_doctor, "_installed_version", lambda: "1.2.2")
    assert _doctor._report_plugin_wake("alpha", []) == 0
    out = capsys.readouterr().out
    assert "[ok] active trigger: plugin MONITOR process is running (pid 4242)" in out
    assert "stream held" not in out


def test_a_missing_monitor_process_still_fails_the_report(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(_doctor, "_monitor_pids", lambda name: [])
    monkeypatch.setattr(_doctor, "_running_watcher_version", lambda agent: "1.2.2")
    monkeypatch.setattr(_doctor, "_installed_version", lambda: "1.2.2")
    assert _doctor._report_plugin_wake("alpha", []) == 1
    out = capsys.readouterr().out
    assert "FAIL: the agentbus plugin is enabled but its monitor is NOT running." in out
    assert "the plugin monitor process is running" not in out
