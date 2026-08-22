"""The guard must NEVER hold a session hostage for a dead credential (#107).

2026-08-13: a workspace reset revoked an agent's key while the project config
still declared it. The PreToolUse guard failed CLOSED on the resulting 401 and
denied EVERY tool call — including `echo` — across every session on the host.
The operator had to physically delete identity files to recover their own
machine. Revoking a key turned into taking the session hostage.

The operator's directive, and the rule these tests enforce: **the only thing
that blocks a tool call is a VERIFIED DENY from the guard.** A dead credential,
an unreachable bus, or a non-200 are absences of a verdict, and an absence of a
verdict must DEGRADE the session (allow with a loud warning), never imprison it.

EVERY ASSERTION RUNS IN BOTH DIRECTIONS. A guard that allows when it cannot
check is only correct if it still BLOCKS on a real deny — otherwise the fix
would have quietly disabled the entire approval control, which is a worse bug
than the one it closes.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

import agentbus_client.hooks.claude_code as hk


class _Resp:
    def __init__(self, payload: dict[str, Any], code: int = 200) -> None:
        self._payload = payload
        self.status = code
        self.code = code

    def read(self) -> bytes:
        return json.dumps(self._payload).encode()

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *a: Any) -> None:
        pass


class _HTTPError(urllib.error.HTTPError):
    """A REAL HTTPError so the hook's `except urllib.error.HTTPError` branch
    catches it exactly as it would catch a live one."""

    def __init__(self, code: int, detail: str = "") -> None:
        super().__init__("https://agentbus.rodmena.co.uk/v1/guard/check", code, "x", None, None)
        self._detail = detail

    def read(self) -> bytes:
        return self._detail.encode()


# The TCP reachability pre-check (peer review C5) would otherwise dial the real
# bus from a unit test. Stubbed reachable by default; a test flips it to model
# a dead network.
_REACHABLE: dict[str, tuple[bool, str]] = {"value": (True, "")}


def _run_hook(monkeypatch: pytest.MonkeyPatch, stdin: str, **env: str) -> dict[str, Any]:
    """Invoke pre_tool_use with a mocked stdin and env; capture the decision."""
    monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
    monkeypatch.setattr(hk._gate, "_bus_reachable", lambda *_a, **_k: _REACHABLE["value"])
    for k in ("AGENTBUS_API_KEY", "AGENTBUS_AGENT", "AGENTBUS_BASE_URL"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return hk.pre_tool_use(None)


def _capture(monkeypatch: pytest.MonkeyPatch, **env: str) -> tuple[dict[str, Any], str]:
    out: list[str] = []

    class _Out:
        def write(self, s: str) -> int:
            out.append(s)
            return len(s)

    monkeypatch.setattr(sys, "stdout", _Out())
    _run_hook(monkeypatch, '{"tool_name":"Read","tool_input":{"file_path":"/tmp/x"}}', **env)
    # The JSON decision is the line that carries hookSpecificOutput; the rest of
    # the capture is whatever the hook also printed (diagnostics).
    decision_line = next(line for line in out if "hookSpecificOutput" in line)
    return json.loads(decision_line), "\n".join(out)


def _decision(result: dict[str, Any] | tuple[dict[str, Any], str]) -> str:
    payload = result[0] if isinstance(result, tuple) else result
    return payload["hookSpecificOutput"]["permissionDecision"]


def _reason(result: dict[str, Any] | tuple[dict[str, Any], str]) -> str:
    payload = result[0] if isinstance(result, tuple) else result
    return payload["hookSpecificOutput"]["permissionDecisionReason"]


# --------------------------------------------------------------------------
# A REAL DENY STILL BLOCKS — the positive direction that keeps this a control.
# --------------------------------------------------------------------------


def test_real_deny_from_guard_still_blocks(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(*_a: Any, **_k: Any) -> _Resp:
        return _Resp({"decision": "deny", "reason": "this action requires human approval"})

    monkeypatch.setattr(hk.urllib.request, "urlopen", fake_urlopen)
    result = _capture(monkeypatch, AGENTBUS_API_KEY="ab_sk_ok", AGENTBUS_AGENT="wired-agent")
    assert _decision(result) == "deny", "a verified deny must still block"


def test_real_allow_from_guard_allows(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_urlopen(*_a: Any, **_k: Any) -> _Resp:
        return _Resp({"decision": "allow", "reason": "permitted"})

    monkeypatch.setattr(hk.urllib.request, "urlopen", fake_urlopen)
    result = _capture(monkeypatch, AGENTBUS_API_KEY="ab_sk_ok", AGENTBUS_AGENT="wired-agent")
    assert _decision(result) == "allow"


# --------------------------------------------------------------------------
# A DEAD CREDENTIAL MUST NEVER BLOCK. These are the branches that did.
# --------------------------------------------------------------------------


def test_revoked_key_allows_with_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """401 from the guard: the credential is gone. The session must proceed."""

    def fake_urlopen(*_a: Any, **_k: Any) -> _Resp:
        raise _HTTPError(401, '{"code":"invalid_api_key","detail":"API key is unknown or revoked"}')

    monkeypatch.setattr(hk.urllib.request, "urlopen", fake_urlopen)
    result, _ = _capture(monkeypatch, AGENTBUS_API_KEY="ab_sk_dead", AGENTBUS_AGENT="wired-agent")
    assert _decision(result) == "allow", "a dead key must not imprison the session"
    assert "UNVETTED" in _reason(result), (
        "the warning must say the session is running UNVETTED, so a degraded "
        "session is never mistaken for a protected one"
    )


def test_unreachable_bus_allows_with_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bus down / transport error: no verdict, so the session proceeds."""

    def boom(*_a: Any, **_k: Any) -> _Resp:
        raise ConnectionError("connection refused")

    monkeypatch.setattr(hk.urllib.request, "urlopen", boom)
    result, _ = _capture(monkeypatch, AGENTBUS_API_KEY="ab_sk_ok", AGENTBUS_AGENT="wired-agent")
    assert _decision(result) == "allow"
    assert "UNVETTED" in _reason(result)


def test_dead_network_allows_fast_and_opens_the_circuit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Peer review C5: a dead network must cost ONE bounded connect attempt, not
    the full read budget twice per tool call, and the very next call must
    fast-fail off the recorded circuit — while still degrading to allow."""
    monkeypatch.setenv("AGENTBUS_WAKE_DIR", str(tmp_path))
    calls = {"urlopen": 0}

    def never(*_a: Any, **_k: Any) -> _Resp:
        calls["urlopen"] += 1
        raise AssertionError("urlopen must not be attempted when the bus is unreachable")

    monkeypatch.setattr(hk.urllib.request, "urlopen", never)
    _REACHABLE["value"] = (False, "TimeoutError: timed out")
    try:
        first, _ = _capture(monkeypatch, AGENTBUS_API_KEY="ab_sk_ok", AGENTBUS_AGENT="wired-agent")
        assert _decision(first) == "allow"
        assert "UNVETTED" in _reason(first) and "unreachable" in _reason(first)
        state = json.loads(hk._gate_degraded_file("wired-agent").read_text())
        assert state["reason"] == "connect_failure" and state["count"] == 1
        # Second call: the circuit is open on the FIRST connect failure, so the
        # hook answers from the state file without even dialling.
        _REACHABLE["value"] = (True, "")
        second, _ = _capture(monkeypatch, AGENTBUS_API_KEY="ab_sk_ok", AGENTBUS_AGENT="wired-agent")
        assert _decision(second) == "allow"
        assert "FAST-FAILING" in _reason(second)
        assert calls["urlopen"] == 0
    finally:
        _REACHABLE["value"] = (True, "")


def test_retired_identity_allows_with_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """410: the identity is gone. Re-register to restore gating — but do not block."""

    def fake_urlopen(*_a: Any, **_k: Any) -> _Resp:
        raise _HTTPError(
            410,
            '{"code":"agent_retired","detail":"agent \'david\' is retired; re-register the name"}',
        )

    monkeypatch.setattr(hk.urllib.request, "urlopen", fake_urlopen)
    result, _ = _capture(monkeypatch, AGENTBUS_API_KEY="ab_sk_ok", AGENTBUS_AGENT="david")
    assert _decision(result) == "allow"
    assert "UNVETTED" in _reason(result)


def test_unparseable_answer_allows_with_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 503 from urllib RAISES — an absence of a verdict, not a block.

    A non-JSON 200 body is the other case; both must allow-with-warning because
    neither carries a `decision`.
    """

    def fake_urlopen(*_a: Any, **_k: Any) -> _Resp:
        raise _HTTPError(503, "service unavailable")

    monkeypatch.setattr(hk.urllib.request, "urlopen", fake_urlopen)
    result, _ = _capture(monkeypatch, AGENTBUS_API_KEY="ab_sk_ok", AGENTBUS_AGENT="wired-agent")
    assert _decision(result) == "allow"
    assert "UNVETTED" in _reason(result)


def test_opted_in_project_with_no_credential_allows_with_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """THE EXACT SCENARIO: project wired, no working key. Must run UNVETTED.

    This is what took the operator's machine down — `.claude/settings.local.json`
    declared an identity whose key was revoked, and every tool call was denied.
    Now it runs with a warning that gating is OFF.
    """
    import subprocess

    proj = tmp_path / "checkout"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.local.json").write_text(
        json.dumps({"env": {"AGENTBUS_AGENT": "agentbus-dev"}})
    )
    (proj / "sdk").mkdir()
    # `_repo_root()` needs a real git checkout — it runs `git rev-parse`.
    subprocess.run(["git", "init", "-q", str(proj)], check=True, capture_output=True)
    # Run the hook FROM the subdirectory — #98 fixed resolution from anywhere.
    monkeypatch.chdir(proj / "sdk")

    result, _ = _capture(monkeypatch)  # NO AGENTBUS_API_KEY at all
    assert _decision(result) == "allow", "a project wired to a dead credential must run, not block"
    assert "UNVETTED" in _reason(result)


def test_unwired_project_allows_silently(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The control: a project that never opted in stays ungated and quiet."""
    proj = tmp_path / "plain"
    proj.mkdir()
    monkeypatch.chdir(proj)
    result, _ = _capture(monkeypatch)
    assert _decision(result) == "allow"
    assert "UNVETTED" not in _reason(result), (
        "an unwired project should not be warned about a degraded session — "
        "there is nothing to degrade"
    )


# --------------------------------------------------------------------------
# The monitor must treat a revoked key as TERMINAL, not retryable.
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not (
        REPO / "marketplace" / "plugins" / "agentbus" / "scripts" / "agentbus-monitor.sh"
    ).exists(),
    reason=(
        "Tests a shell script that lives in the rodmena-limited/claude-plugins-marketplace "
        "repo, not this SDK repo. Runs on the main agentbus checkout where the marketplace "
        "is a submodule; skipped in the extracted client repo."
    ),
)
def test_monitor_script_has_a_terminal_branch_for_exit_8() -> None:
    """`agentbus watch` exits 8 on an auth failure; the monitor must stop there
    instead of burning the 5-attempt budget against a dead key.

    EIGHT, NOT THREE. The first cut branched on 3, and 3 is the GENERIC
    AgentBusError exit — it also covers TransportError (bus down, DNS loss,
    connection refused), which are transient and must stay retryable. The
    wake-chain probe caught the conflation: its black-hole test (unreachable
    bus) hit the terminal branch and the monitor stopped saying the inbox was
    unchecked. AuthError now has a dedicated exit 8.
    """
    body = (
        REPO / "marketplace" / "plugins" / "agentbus" / "scripts" / "agentbus-monitor.sh"
    ).read_text()
    assert '[ "$status" -eq 8 ]' in body
    assert '[ "$status" -eq 3 ]' not in body, (
        "branching on 3 would silence legitimate reconnect loops — 3 includes "
        "transport errors, which are retryable"
    )
    assert "REJECTED" in body
    assert "NOT retry" in body
    # The woken session must be told to do NOTHING — naming credential-acquiring
    # commands to an agent reading its own notifications is an instruction.
    assert "TAKE NO ACTION" in body


def test_watch_exits_8_on_a_rejected_credential() -> None:
    """The exit code the monitor branches on is real, not invented: AuthError
    gets its own `return 8`, caught ABOVE the generic AgentBusError's 3."""
    body = (REPO / "src" / "agentbus_client" / "cli" / "_watch_run.py").read_text()
    assert "except AuthError" in body
    assert "return 8" in body
    # And the generic path is still 3 — transport failures must stay retryable.
    assert "return 3" in body
    # Ordering matters WITHIN main()'s ladder: AuthError before AgentBusError or
    # the subclass never matches. Scoped from the AuthError handler forward —
    # a whole-file index() finds an unrelated earlier handler (that bug was this
    # test's own first draft).
    auth_at = body.index("except AuthError")
    generic_after = body.index("except AgentBusError", auth_at)
    assert body.index("return 8", auth_at) < generic_after, (
        "AuthError must return 8 before the generic AgentBusError handler"
    )


# --------------------------------------------------------------------------
# Identity is consolidated: .agentbus/agent authoritative, settings mirror.
# --------------------------------------------------------------------------


def test_identity_files_are_gitignored() -> None:
    gi = (REPO / ".gitignore").read_text()
    assert ".agentbus/" in gi, "the authoritative identity must never be committed"
    assert "settings.local.json" in gi, "the Claude Code mirror must never be committed"


def test_setup_writes_both_identity_sources_from_one_name() -> None:
    """settings.local.json exists only because Claude Code turns its env block
    into hook environment variables; it must be written from the same name as
    .agentbus/agent so the two can never disagree."""
    body = _onboarding_source()
    assert "_write_worktree_identity(name, report)" in body
    assert "The two must never disagree" in body


def _onboarding_source() -> str:
    """onboarding is a package now (one module per concern): read all of it, in a stable order."""
    from pathlib import Path as _P

    pkg = _P(__file__).resolve().parents[1] / "src" / "agentbus_client" / "onboarding"
    return "".join(f.read_text() for f in sorted(pkg.glob("*.py")))
