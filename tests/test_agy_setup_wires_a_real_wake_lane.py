"""#56: `setup agy` must wire a real wake lane, and refuse the gate out loud.

THE INCIDENT. This client refused `setup agy` from the day the verb existed:

    "agy's CLI exposes no hook contract, no MCP server support, and no wake
     path (no daemon, stream, or socket)"

Measured against agy 1.1.27, all three are false. A refusal that is factually
wrong is worse than no refusal — it turns away an operator whose harness works.
These tests pin the replacement, and one of them pins that we deleted only the
STALE refusal: `codex` was never re-checked, so it must still refuse.

WHY THE PLUGIN IS MACHINE-WIDE, which these tests encode. A workspace plugin is
the obvious design and it silently does not run: measured on agy 1.1.27 with a
probe hook that appends to a file, only `~/.gemini/config/...` hooks fire, while
`<workspace>/.agents/` hooks stay silent even with the workspace trusted. And
`agy plugin validate` calls the workspace plugin VALID, reporting "hooks: 2
processed" — so validation alone would have shipped a plugin that never ran.

The merge tests exist because Antigravity runs every plugin's named hooks
sequentially out of one shared `hooks.json`. Clobbering a foreign key there does
not break us, it breaks somebody else's tooling, silently.
"""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout

import pytest

from agentbus_client import onboarding
from agentbus_client.onboarding import _agy_plugin, _agy_setup, _paths, _provision


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A git repo, a provisioned agent, and an isolated global config root."""
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.chdir(repo)

    keys = tmp_path / "cfg" / "keys"
    keys.mkdir(parents=True)
    (keys / "alpha.env").write_text("export AGENTBUS_API_KEY=ab_sk_alpha_secret\n")
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(tmp_path / "cfg"))
    # The plugin is MACHINE-WIDE; redirect its root so tests never touch the
    # operator's real ~/.gemini.
    monkeypatch.setenv("AGY_CONFIG_HOME", str(tmp_path / "gemini-config"))

    monkeypatch.setattr(
        _provision, "_provision_project_agent", lambda args, report, harness: "alpha"
    )
    monkeypatch.setattr(_agy_setup, "_provision_project_agent", lambda a, r, h: "alpha")
    monkeypatch.setattr(_agy_setup, "doctor_credential_scope", lambda base_url=None: [])
    # No network in the unit suite: the skill note asks /skills/index.json.
    monkeypatch.setattr(_agy_setup, "_skill_note", lambda base_url: "skill: NOT installed (stub)")
    return repo


def _run_setup(monkeypatch) -> str:
    args = argparse.Namespace(harness="agy", base_url=None, role=None, persona=None)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _agy_setup._setup_agy(args)
    assert rc == 0
    return buf.getvalue()


def test_setup_writes_a_plugin_with_both_lanes(wired, monkeypatch):
    out = _run_setup(monkeypatch)
    plugin = _paths.agy_plugin_dir()

    assert json.loads((plugin / "plugin.json").read_text())["name"] == "agentbus"
    hooks = json.loads((plugin / "hooks.json").read_text())
    assert "Stop" in hooks["agentbus-wake"], "no active wake lane = a half-wire"
    assert "PreInvocation" in hooks["agentbus-catchup"]
    assert "agy-stop" in hooks["agentbus-wake"]["Stop"][0]["command"]
    assert "setup agy" in out


def test_the_gate_is_absent_and_the_report_says_why(wired, monkeypatch):
    """Not `enabled: false` — absent. A disabled block pointing at an
    unimplemented subcommand is a trap for whoever flips it."""
    out = _run_setup(monkeypatch)
    hooks = json.loads((_paths.agy_plugin_dir() / "hooks.json").read_text())
    assert not any("PreToolUse" in block for block in hooks.values())
    assert "gate (PreToolUse): NOT wired" in out
    assert "fail CLOSED" in out


def test_the_report_does_not_promise_a_claude_shaped_wake(wired, monkeypatch):
    """agy hooks BLOCK the loop. An operator told they have Claude's 540s idle
    hold has been promised something this host cannot do."""
    out = _run_setup(monkeypatch)
    assert "BLOCK" in out
    assert "agentbus service" in out, "must name what DOES give always-on reachability"


def test_the_report_says_the_plugin_is_machine_wide(wired, monkeypatch):
    """It is installed outside the repo and fires for every agy session, so an
    operator who thinks it is project-scoped has the wrong mental model."""
    out = _run_setup(monkeypatch)
    assert "MACHINE-WIDE" in out
    assert "workspacePaths" in out, "must explain how it stays per-project anyway"


def test_setup_writes_no_credential_anywhere(wired, monkeypatch):
    """A machine-wide plugin holds exactly ONE bearer key, so writing one would
    make every checkout on this box act as this project's agent — the
    global-only identity asymmetry that gets `codex` refused."""
    out = _run_setup(monkeypatch)
    plugin = _paths.agy_plugin_dir()
    for f in plugin.rglob("*"):
        if f.is_file():
            assert "ab_sk_" not in f.read_text(), f"{f} contains key material"
    assert not (plugin / "mcp_config.json").exists()
    assert "mcp: NOT configured" in out
    assert "agy mcp add" in out, "must still tell the operator how to opt in"


def test_setup_puts_nothing_of_ours_in_the_repo(wired, monkeypatch):
    """The plugin lives outside the checkout, so there is no in-repo secret and
    nothing new to gitignore beyond the identity file the shared step handles."""
    _run_setup(monkeypatch)
    assert not (wired / ".agents").exists()


def test_the_mcp_shape_matches_what_agy_itself_writes():
    """Derived by running `agy mcp add --header` into a throwaway HOME, not read
    from the docs — the embedded MCP schema omits `headers` entirely, so a
    docs-derived file would be wrong-shaped and fail silently as 'no tools'.

    Still pinned even though setup writes no MCP file: this is the shape any
    future MCP lane must produce, and it was expensive to establish.
    """
    server = _agy_plugin.mcp_config("https://x.test/mcp/", "ab_sk_k")["mcpServers"]["agentbus"]
    assert set(server) == {"disabled", "headers", "serverUrl"}
    assert server["headers"]["Authorization"] == "Bearer ab_sk_k"
    assert server["serverUrl"] == "https://x.test/mcp/"


def test_setup_is_idempotent(wired, monkeypatch):
    _run_setup(monkeypatch)
    plugin = _paths.agy_plugin_dir()
    before = {p.name: p.read_bytes() for p in plugin.iterdir() if p.is_file()}
    out = _run_setup(monkeypatch)
    after = {p.name: p.read_bytes() for p in plugin.iterdir() if p.is_file()}
    assert before == after, "a second run must change nothing on disk"
    assert "(current" in out or "already" in out


def test_a_per_checkout_teardown_leaves_the_machine_plugin_alone(wired, monkeypatch):
    """Removing it because ONE checkout opted out would silently unwire every
    other project on the machine — the same rule the Claude lane follows for its
    machine-wide entries in ~/.claude/settings.json."""
    _run_setup(monkeypatch)
    plugin = _paths.agy_plugin_dir()
    assert plugin.is_dir()

    buf = io.StringIO()
    with redirect_stdout(buf):
        onboarding.cmd_teardown(argparse.Namespace(purge_key=False, machine=False))
    assert plugin.is_dir(), "a per-checkout teardown must not remove machine-wide wiring"


def test_teardown_machine_does_remove_it(wired, monkeypatch):
    """KNOWN-POSITIVE TWIN: without it, the test above passes just as well
    against a teardown that can never remove the plugin at all."""
    _run_setup(monkeypatch)
    plugin = _paths.agy_plugin_dir()
    removed: list[str] = []
    _agy_setup.teardown_agy_machine(removed)
    assert not plugin.exists()
    assert any("machine-wide" in line for line in removed)


# ---------------------------------------------------------------- the merge


def _foreign() -> dict:
    return {"team-linter": {"PostToolUse": [{"matcher": "*", "hooks": [{"command": "./lint.sh"}]}]}}


def test_a_foreign_hook_survives_byte_for_byte():
    existing = _foreign()
    original = json.dumps(existing, sort_keys=True)
    merged, state = _agy_plugin.ensure_hooks(existing, _agy_plugin.hooks_config("/bin/hook"))
    assert state == "updated"
    assert json.dumps({"team-linter": merged["team-linter"]}, sort_keys=True) == original
    assert "agentbus-wake" in merged


def test_re_running_the_merge_is_a_no_op():
    desired = _agy_plugin.hooks_config("/bin/hook")
    merged, _ = _agy_plugin.ensure_hooks(_foreign(), desired)
    again, state = _agy_plugin.ensure_hooks(merged, desired)
    assert state == "ok", "an unchanged file must not be rewritten"
    assert again == merged


def test_removal_takes_only_ours():
    """Recognition must survive the binary being installed under another name —
    the command embeds an absolute path, so the SUBCOMMAND is the durable half
    of the marker."""
    merged, _ = _agy_plugin.ensure_hooks(_foreign(), _agy_plugin.hooks_config("/opt/x/renamed"))
    remaining, changed = _agy_plugin.remove_hooks(merged)
    assert changed and set(remaining) == {"team-linter"}


def test_an_empty_hooks_file_is_not_malformed(tmp_path):
    """Empty means 'nothing configured' for a file we merge into. This machine
    has a zero-byte ~/.gemini/config/mcp_config.json, so the case is real."""
    p = tmp_path / "hooks.json"
    p.write_text("")
    assert _agy_plugin.load_hooks(p) == {}


def test_a_corrupt_hooks_file_refuses_rather_than_clobbering(tmp_path):
    p = tmp_path / "hooks.json"
    p.write_text("{ this is not json")
    with pytest.raises(SystemExit):
        _agy_plugin.load_hooks(p)


# ------------------------------------------------------------- the dispatch


def test_the_agy_refusal_is_gone_but_codex_still_refuses(monkeypatch, tmp_path):
    """We corrected ONE stale claim. codex's blockers were never re-checked, so
    assuming they expired too would be exactly the same mistake in reverse."""
    monkeypatch.chdir(tmp_path)
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = onboarding.cmd_setup(
            argparse.Namespace(harness="codex", base_url=None, role=None, persona=None)
        )
    assert rc == 1
    assert "refused" in buf.getvalue()


def test_antigravity_is_accepted_as_an_alias():
    from agentbus_client.onboarding import canonical_harness

    assert canonical_harness("antigravity") == "agy"
    assert canonical_harness("agy") == "agy"
    assert canonical_harness("claude") == "claude"
    assert "antigravity" not in onboarding.HARNESSES, "an alias, never a second entry"


def test_the_window_stays_under_the_hook_timeout():
    """The one-number invariant, mirroring REWAKE_WINDOW_SEC/HOOK_TIMEOUT. A
    window at or above the timeout means the harness kills the poll before it
    finishes — a monitor that cannot monitor."""
    assert _paths.AGY_WAKE_WINDOW_SEC < _paths.AGY_WAKE_HOOK_TIMEOUT_SEC
    assert _paths.AGY_WAKE_HOOK_TIMEOUT_SEC <= 30, "must not exceed agy's DOCUMENTED default"


# ------------------------------------------------- the skill note asks, not assumes


class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


def _index(monkeypatch, resp):
    import httpx

    monkeypatch.setattr(httpx, "get", lambda *a, **k: resp)


def test_the_skill_note_reports_served_when_the_index_lists_it(monkeypatch):
    """THE REGRESSION THIS REPLACES: the note used to assert '/skills/antigravity.md
    is 404'. True when written, and it would have kept saying so forever."""
    _index(monkeypatch, _Resp(200, {"harnesses": ["claude-code", "antigravity"], "aliases": {}}))
    assert "SERVED" in _agy_setup._skill_note("https://x.test")


def test_the_skill_note_reports_absent_when_the_index_does_not(monkeypatch):
    """The other direction, against the index as it really is today."""
    _index(
        monkeypatch,
        _Resp(
            200, {"harnesses": ["claude-code", "opencode"], "aliases": {"claude": "claude-code"}}
        ),
    )
    note = _agy_setup._skill_note("https://x.test")
    assert "NOT installed" in note
    assert "shadow" in note, "must still say WHY bundling one would be wrong"


def test_an_alias_in_the_index_counts_as_served(monkeypatch):
    """Their canonical name is `antigravity`; `agy` is an alias they publish."""
    _index(
        monkeypatch,
        _Resp(200, {"harnesses": ["agy"], "aliases": {"antigravity": "agy"}}),
    )
    assert "SERVED" in _agy_setup._skill_note("https://x.test")


def test_an_unreachable_index_installs_nothing_and_says_so(monkeypatch):
    """Absence of an answer is not an answer. Never guess in either direction."""
    import httpx

    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", boom)
    assert "NOT checked" in _agy_setup._skill_note("https://x.test")
    _index(monkeypatch, _Resp(503))
    assert "NOT checked" in _agy_setup._skill_note("https://x.test")


# ------------------------------------------- the shared-identity warning


def test_a_checkout_already_wired_for_claude_gets_the_shared_identity_warning(wired, monkeypatch):
    """Reported by the server team with field evidence the same day: read/ack
    state belongs to the AGENT, not the connection, so two live hosts on one
    identity hide messages from each other."""
    (wired / ".claude").mkdir()
    (wired / ".claude" / "settings.local.json").write_text("{}")
    out = _run_setup(monkeypatch)
    assert "same agent" in out.lower() or "SAME agent" in out
    assert "worktree" in out, "must name the fix, not just the hazard"


def test_a_single_harness_checkout_gets_no_such_warning(wired, monkeypatch):
    """KNOWN-POSITIVE TWIN: a warning printed unconditionally teaches nothing."""
    out = _run_setup(monkeypatch)
    assert "SAME agent" not in out


# ------------------------------------------- the sealing key (#189)


def test_setup_publishes_the_sealing_key_on_an_encrypted_workspace(wired, monkeypatch):
    """THE FIELD BUG. The first version of this module omitted the publish step.
    An agent with no published pubkey cannot be WRITTEN TO on an encrypted
    workspace — senders get "cannot seal: these recipients have published no
    public key" — so setup reported success over an agent that could send and
    never receive. Caught within minutes of the first real wiring."""
    published: list[tuple] = []

    class _Bus:
        def __init__(self, **kw):
            pass

        def _request(self, *a, **k):
            return {"encrypted": True}

    monkeypatch.setattr(_agy_setup, "AgentBus", _Bus)
    monkeypatch.setattr(
        _agy_setup,
        "_sealing_publish_with_retry",
        lambda bus, agent, pub: published.append((agent, pub)) or {"fingerprint": "fp123"},
    )
    out = _run_setup(monkeypatch)
    assert published and published[0][0] == "alpha"
    assert "registered as fp123" in out


def test_a_failed_publish_is_loud_not_a_soft_line(wired, monkeypatch):
    """An operator who does not read the whole report must still catch this:
    the agent is registered and unreachable."""

    class _Bus:
        def __init__(self, **kw):
            pass

        def _request(self, *a, **k):
            return {"encrypted": True}

    monkeypatch.setattr(_agy_setup, "AgentBus", _Bus)
    monkeypatch.setattr(_agy_setup, "_sealing_publish_with_retry", lambda *a: None)
    out = _run_setup(monkeypatch)
    assert "!!!" in out and "CANNOT seal" in out
    assert "agentbus keys rotate" in out, "must name the recovery command"


def test_an_unencrypted_workspace_needs_no_key(wired, monkeypatch):
    """KNOWN-POSITIVE TWIN: without it, the tests above pass against a setup
    that publishes unconditionally and wastes a round trip everywhere."""

    class _Bus:
        def __init__(self, **kw):
            pass

        def _request(self, *a, **k):
            return {"encrypted": False}

    monkeypatch.setattr(_agy_setup, "AgentBus", _Bus)
    monkeypatch.setattr(
        _agy_setup, "_sealing_publish_with_retry", lambda *a: pytest.fail("must not publish")
    )
    out = _run_setup(monkeypatch)
    assert "not needed" in out
