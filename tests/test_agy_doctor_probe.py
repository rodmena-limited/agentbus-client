"""#56: `doctor --wake` on an agy checkout must RUN the hook, not inspect files.

THE INCIDENT THIS ENCODES. Wiring the harness correctly and then seeing nothing
was a two-hour failure with an operator watching: setup reported a clean
wire-up, the hooks were installed, `agy plugin validate` said `[ok]`, and mail
never surfaced. Every check that existed was green, because every check that
existed inspected configuration. The hook was resolving a different agent and
polling the wrong inbox, and nothing could ask "what does it resolve here".

THE SECOND TRAP, caught on this probe's own first live run. It compared the
hook's resolution against the name DOCTOR resolved, which honours
$AGENTBUS_AGENT. The hook deliberately does not. So running doctor from a shell
carrying another agent's identity flagged the hook as broken when the hook was
right — a check that fails on correct systems is worse than no check.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

import pytest

from agentbus_client.onboarding import _doctor_agy, _paths


def _hooks(command: str) -> dict:
    return {"agentbus-wake": {"Stop": [{"command": command}]}}


@pytest.fixture
def wired(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".agentbus").mkdir(parents=True)
    (repo / ".agentbus" / "agent").write_text("alpha\n")
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AGY_CONFIG_HOME", str(tmp_path / "gemini"))
    _paths.agy_mark_wired(repo)
    plugin = _paths.agy_plugin_dir()
    plugin.mkdir(parents=True)
    # A stand-in hook that emits VALID JSON. The `# agy-stop` comment keeps the
    # marker the probe looks for without disturbing what the shell prints — the
    # first version used /bin/echo and the shell ate the quotes, so the probe
    # correctly reported non-JSON and the test failed for the right reason.
    (plugin / "hooks.json").write_text(
        json.dumps(_hooks('printf \'{"decision":"stop"}\' # agy-stop'))
    )
    return repo


def _run(root, doctor_agent: str = "alpha") -> tuple[int, str]:
    """`doctor_agent` is the name DOCTOR resolved for itself — which honours
    $AGENTBUS_AGENT and so legitimately differs from the checkout's."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _doctor_agy.doctor_wake_agy(doctor_agent, root)
    return rc, buf.getvalue()


def test_a_healthy_checkout_passes_and_names_the_operative_agent(wired, monkeypatch):
    rc, out = _run(wired)
    assert rc == 0, out
    assert "hook resolves: alpha" in out
    assert "WIRED for alpha" in out


def test_a_shell_carrying_another_identity_is_not_a_failure(wired):
    """THE SECOND TRAP. doctor honours $AGENTBUS_AGENT; the hook deliberately
    does not. Comparing them flagged a correct system as broken."""
    rc, out = _run(wired, doctor_agent="someone-elses-session")
    assert rc == 0, out
    assert "Not a fault" in out


def test_a_checkout_that_never_opted_in_is_reported(wired, monkeypatch, tmp_path):
    other = tmp_path / "unwired"
    (other / ".agentbus").mkdir(parents=True)
    (other / ".agentbus" / "agent").write_text("beta\n")
    rc, out = _run(other)
    assert rc == 1
    assert "not in the agy opt-in list" in out


def test_a_missing_hook_binary_is_caught(wired, monkeypatch):
    """A hook that cannot execute is indistinguishable from an agent with no
    mail — silence either way."""
    plugin = _paths.agy_plugin_dir()
    (plugin / "hooks.json").write_text(json.dumps(_hooks("/nonexistent/agentbus-hook agy-stop")))
    rc, out = _run(wired)
    assert rc == 1
    assert "does not exist" in out


def test_a_plugin_with_no_stop_handler_is_caught(wired):
    plugin = _paths.agy_plugin_dir()
    (plugin / "hooks.json").write_text(json.dumps({"something-else": {"PreInvocation": []}}))
    rc, out = _run(wired)
    assert rc == 1
    assert "no agy-stop Stop handler" in out


def test_the_workspace_limit_is_always_stated(wired):
    """The probe supplies its own workspace, so a green result does NOT prove the
    operator's real session will have one. Saying so is the whole point — this is
    the fact that cost two hours."""
    _, out = _run(wired)
    assert "workspacePaths`: []" in out or "workspacePaths` is empty" in out or "--add-dir" in out
