"""`agentbus signin`, `agentbus setup <harness>`, `agentbus doctor --wake`.

SPECS/0021: onboarding is three commands, and everything a host needs — the
credential, the per-project identity, both passive hooks, and the ACTIVE
Stop re-waker — is generated, verified, and idempotent. Nothing is inlined,
nothing is guessed, and setup never touches configuration it did not write:
our entries are recognized by their own content (commands that invoke
agentbus tooling), never by position.

Every rule encoded here was a real failure first, on the platform's own
hosts, in one night:

  * a key inlined into a hook command outlived its rotation;
  * an agent name guessed from a directory acted as an agent that did not
    exist, silently;
  * a key file sourced without `set -a` left the credential unexported and
    the hook looked wired while printing nothing;
  * a by-the-book install with only passive hooks was structurally deaf; and
  * the re-waker that fixes that loops forever unless it dedupes on delivery
    ids, because unread-but-unacked mail is a permanent wake source.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

from ._paths import (
    _config_dir,
    _git_root_or_none,
    _key_from_env_file,
    _keys_dir,
    _load_json,
    _signin_state_path,
)

# ---------------------------------------------------------------- setup


def _project_claude_dir() -> Path:
    """This CHECKOUT's `.claude`, resolved from the git root — not the cwd.

    THE CWD VERSION SILENTLY UNWIRED EVERY SUBDIRECTORY. `_agent_from_worktree`
    (below) has always read `.agentbus/agent` from `git rev-parse
    --show-toplevel`, so it resolves the same identity anywhere in a checkout.
    This function did not, and the two disagreed everywhere except the top:

        .claude/settings.local.json at worktree ROOT, run at ROOT   -> resolved
        .claude/settings.local.json at worktree ROOT, run in sdk/   -> None

    That matters more than it looks, for two reasons.

    First, `settings.local.json`'s `env` block is the ONLY identity declaration
    Claude Code injects into hook and monitor processes, so it is the one a
    Claude-wired project actually depends on.

    Second, since the `AGENTBUS_AGENT` kill switch landed (#95), resolving no
    agent means AgentBus goes SILENTLY INERT rather than erroring — which is
    correct for a project that never opted in, and indistinguishable from it
    for a wired project whose identity we simply failed to find. The failure
    mode is silence, so nothing reports it.

    A git worktree is its own project here: `--show-toplevel` returns the
    WORKTREE's root, not the main checkout's, so two worktrees of one repo get
    two `.claude` directories and two identities. That is deliberate — it is
    the same rule `session_key` already encodes by hashing the path.

    Falls back to the cwd outside a git repo, where there is no better answer.
    """
    return (_git_root_or_none() or Path.cwd()) / ".claude"


def _resolve_agent_name(explain: list[str] | None = None) -> str | None:
    """Which agent is THIS project? Never a guess, and never a global default
    applied to a project that told us otherwise.

    THE AUTHORITATIVE SOURCE IS `.agentbus/agent`; `settings.local.json` is a
    DERIVED MIRROR, never an independent declaration. `setup` writes both from
    the same `name` so they cannot disagree, and both are gitignored (#107).

    Precedence, and every step of it was a real failure:

      1. $AGENTBUS_AGENT — the operator's explicit word. THE ENV VAR OUTRANKS
         EVERYTHING (#90). The file used to win, and the 2026-08-11 incident is
         why that was wrong: the operator exported AGENTBUS_AGENT=<their name>,
         setup had earlier written a role-derived name into settings.local.json,
         and from then on every component followed the file while the operator's
         env sat ignored — the session literally identified as an agent the
         operator never named. A stored default must never override an explicit
         instruction present in the environment;
      2. this project's settings.local.json — written by a previous setup, so
         an established project keeps its identity when the shell exports
         nothing;
      3. the signin default — only as a last resort, and only when no role was
         asked for.

    `explain` collects which source answered, because a wrong identity that
    announces its provenance is debuggable and a silent one is not.
    """
    env_name = os.environ.get("AGENTBUS_AGENT")
    if env_name:
        if explain is not None:
            explain.append(
                f"identity from $AGENTBUS_AGENT ({env_name}) — the env var "
                "outranks settings.local.json"
            )
        return str(env_name)

    worktree = _agent_from_worktree()
    if worktree:
        if explain is not None:
            explain.append(f"identity from this worktree's .agentbus/agent ({worktree})")
        return worktree

    local = _load_json(_project_claude_dir() / "settings.local.json")
    name = (local.get("env") or {}).get("AGENTBUS_AGENT")
    if name:
        if explain is not None:
            explain.append(f"identity from this project's settings.local.json ({name})")
        return str(name)

    # THE SIGNIN DEFAULT IS NO LONGER A SOURCE OF IDENTITY. Removed 2026-08-13.
    #
    # It answered "who is this project?" with "whoever last signed in on this
    # machine", which is not an answer about the project at all. Two failures
    # came out of it, and the second is why it is gone rather than merely
    # de-prioritised:
    #
    #   * an UNWIRED scratch directory attached to another agent's inbox and
    #     consumed its mail (bob's reproduction, cursor 474);
    #   * a project nobody had opted in got an identity anyway, so tooling that
    #     should have been inert went looking for an agent, found one, and
    #     activated a bus the operator had deliberately not asked for.
    #
    # Identity is now declared, never inherited: the env var, or the worktree's
    # own .agentbus/agent, or an explicit --role. Nothing else.
    return None


def _agent_from_worktree(root: Path | None = None) -> str | None:
    """The identity this CHECKOUT declares, from `.agentbus/agent`.

    Harness-neutral and per-checkout: Claude Code, opencode and codex all read
    the same file, and two worktrees of one repo are two agents without any
    machine-global state deciding it for them. Read from the repo ROOT so a
    command run in a subdirectory resolves the same identity as one at the top.
    """
    try:
        # #40: OUTSIDE A GIT REPO, FALL BACK TO THE WORKING DIRECTORY. This used
        # to `return None` when `git rev-parse` found no root, which made THE
        # AUTHORITATIVE SOURCE UNREACHABLE in any non-repo directory — so
        # settings.local.json, documented above as a derived mirror rather than
        # an independent declaration, won permanently and silently.
        #
        # That is what produced the recurring "brain split": setup's own
        # mismatch message tells the operator to write `.agentbus/agent`, and in
        # a non-repo directory that file was then ignored with no warning. The
        # operator follows the printed remedy, nothing changes, and nothing says
        # so — a remedy that cannot go green. Measured on infra-manager in
        # /home/farshid/develop: 195 `no_credential` gate failures from
        # 2026-08-17 to 2026-08-22, every one of them silent.
        #
        # Precedence is UNCHANGED ($AGENTBUS_AGENT still outranks this, and this
        # still outranks settings.local.json); only step 2's reachability moves.
        top = root or _git_root_or_none() or Path.cwd()
        declared = top / ".agentbus" / "agent"
        if not declared.is_file():
            return None
        return declared.read_text().strip() or None
    except OSError:
        return None


def _write_worktree_identity(name: str, report: list[str]) -> None:
    """Declare this checkout's agent at `<repo root>/.agentbus/agent`.

    GITIGNORED, not committed, and that is deliberate rather than an oversight.
    Identity is per machine and per checkout — session_key is derived from
    device, repo and path — so a committed file would hand every clone of the
    repo the same agent name and every one of them would fight over one inbox.
    "In the repo" here means DISCOVERABLE AND PER-WORKTREE, not shared.
    """
    root = _git_root_or_none() or Path.cwd()
    path = root / ".agentbus" / "agent"
    # #101: THE WRITER ENSURES THE IGNORE, not each caller. `agentbus register`
    # wrote this file and never touched .gitignore — setup did, in its own
    # flow — so a bare register left a machine-local identity one `git add -A`
    # away from being committed, at which point every clone fights over one
    # inbox (the docstring above is the why). Runs BEFORE the already-declared
    # early return so a checkout wired by an old client is repaired on the next
    # register, and rooted at the REPO root, not cwd: register can run from a
    # subdirectory. `.git` is a file in linked worktrees — exists() covers both.
    if (root / ".git").exists():
        gitignore = root / ".gitignore"
        with contextlib.suppress(OSError):
            lines = gitignore.read_text().splitlines() if gitignore.exists() else []
            if ".agentbus/" not in lines:
                with gitignore.open("a", encoding="utf-8") as fh:
                    fh.write(".agentbus/\n")
                report.append(f"gitignore: added .agentbus/ ({gitignore})")
    try:
        current = path.read_text().strip() if path.is_file() else None
    except OSError:
        current = None
    if current == name:
        report.append(f"worktree identity: {path} (already {name})")
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{name}\n")
    except OSError as exc:
        report.append(f"worktree identity: NOT written ({exc})")
        return
    report.append(f"worktree identity: {path} = {name}")


def _derived_name(role: str | None) -> str | None:
    """The agent name this project WOULD get — computed locally, so an error can
    name it. Identity is derived from device+repo+path, so the client already
    knows this without asking the server."""
    if not role:
        return None
    try:
        from .. import identity

        key = str(identity.describe(None).get("session_key") or "")
        return f"{role}-{key[:6]}" if key else None
    except Exception:
        return None


def _signed_in_bound_agent() -> str | None:
    """The agent this machine's signin key is bound to, if it is a bound key."""
    path = _signin_state_path()
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    return None if state.get("operator") else state.get("default_agent")


def _operator_key() -> str | None:
    path = _config_dir() / "operator.env"
    if not path.exists():
        return None
    return _key_from_env_file(path)


def _session_identity() -> str | None:
    """WHO THIS SESSION IS, decided without reference to what was asked for.

    Project setting first, signin default second. Both are records the operator
    made deliberately; neither is inferred from a flag. Keeping this separate
    from the credential lookup is the whole of #67 — an identity ASSERTION must
    never double as an instruction to go find somebody's secret.
    """
    # THE CHECKOUT'S OWN DECLARATION FIRST (peer review C6): the hooks and the
    # plugin monitor read `.agentbus/agent`; the bare CLI used to skip it and
    # fall through to the signin default, so `agentbus inbox` in a wired
    # checkout acted as whoever last ran signin on the box.
    worktree = _agent_from_worktree()
    if worktree:
        return worktree
    try:
        local = _load_json(_project_claude_dir() / "settings.local.json")
        identity = (local.get("env") or {}).get("AGENTBUS_AGENT")
    except SystemExit:
        identity = None
    if identity:
        return str(identity)
    # THE MONITOR REFUSES THIS FALLBACK AND THE CLI KEEPS IT, DELIBERATELY.
    #
    # bob asked whether this was decided or missed, having watched the same value
    # be deleted from agentbus-monitor.sh hours earlier as a cross-agent leak.
    # It is decided, and the difference is who is watching:
    #
    #   monitor  attaches to an inbox UNATTENDED and streams it. Guessing wrong
    #            there silently consumes another agent's wakes, with nobody
    #            present to notice.
    #   CLI      runs in the foreground for someone who sees the result, and
    #            `signin` is an explicit act by the operator naming their own
    #            default. Dropping it would break `agentbus inbox` in every
    #            directory that is not a wired project.
    #
    # The honest weakness, and it has bitten here: foreground is not the same as
    # NOTICED. Messages went out under a fixture identity on this host and the
    # sender only learned of it from a peer. The fallback stays; what has to
    # change is that it stops being silent where it can do harm.
    sp = _signin_state_path()
    if sp.exists():
        try:
            stored = json.loads(sp.read_text()).get("default_agent")
        except json.JSONDecodeError:
            return None
        return str(stored) if stored else None
    return None


def _agent_key(agent: str) -> str | None:
    """The agent's own stored key, if one exists (keys/<agent>.env).

    REG-8d: THE SITE REG-8b MISSED. That sweep sanitized four sibling
    call sites through `sealing.bound_env_filename` and enumerated them in
    that helper's docstring — cli._key_for_agent, cli join/setup/service,
    hooks._adopt_credential_for. This function was not on the list and kept
    building `keys/{agent}.env` by raw f-string.

    It is the WORST of the family to have missed, because it is the one on
    the `resolve_credentials` path: `_session_identity` reads the agent name
    out of the project's own `.claude/settings.local.json`, so the value is
    controlled by whatever checkout the operator happens to `cd` into.

    Reproduced before fixing, with AGENTBUS_CONFIG_DIR=/tmp/trav/cfg and a
    hostile `.claude/settings.local.json` containing
    {"env": {"AGENTBUS_AGENT": "../operator"}}:

        _agent_key("../operator")  -> 'ab_sk_OPERATOR_SECRET...'
        _credentials.resolve_credentials()      -> ('ab_sk_OPERATOR_SECRET...', '../operator')

    i.e. a hostile repo escalated an ordinary CLI verb from its own bound
    `send` key to the workspace OPERATOR credential — the one this codebase
    elsewhere labels "can MINT — never auto-inherit it". Sanitizing here
    makes the traversal resolve to `keys/__operator.env`, which does not
    exist, so the lookup returns None exactly as the siblings do.
    """
    from .. import sealing

    path = _keys_dir() / sealing.bound_env_filename(agent)
    if not path.exists():
        return None
    return _key_from_env_file(path)
