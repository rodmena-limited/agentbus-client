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

import argparse
import contextlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

from .client import AgentBus, AgentBusError
from .identity import config_dir as identity_config_dir


def _device_hash(device_id: str | None) -> str:
    """Hash a device id the way the phonebook publishes it (#74).

    The phonebook emits `device_hash`, not the raw id: the raw id is one of the
    three inputs to session_key = sha256(device_id : repo_fingerprint :
    path_hash), so publishing it handed every workspace member material that
    DEFINES another agent's identity.

    Equality is all this guard ever needed, so a hash serves it exactly as well.
    """
    import hashlib

    return hashlib.sha256((device_id or "").encode()).hexdigest()


HARNESSES = ("claude", "opencode", "codex", "agy")

# Recognition markers: an entry in a harness config is OURS iff its command
# contains one of these. This is what makes setup safe to re-run and safe to
# run beside anyone else's hooks.
_MARKER_HOOK = "agentbus-hook"
_MARKER_REWAKE = "stop-rewake.sh"

_SESSION_START_CMD = (
    '[ -n "${AGENTBUS_AGENT:-}" ] || exit 0; '
    '[ -n "${AGENTBUS_API_KEY:-}" ] || { set -a; '
    '[ -f "$HOME/.config/agentbus/keys/${AGENTBUS_AGENT}.env" ] && . "$HOME/.config/agentbus/keys/${AGENTBUS_AGENT}.env" 2>/dev/null; set +a; } || true; '
    "agentbus-hook session-start || true"
)
_PENDING_CMD = _SESSION_START_CMD.replace("session-start", "pending")

# The re-waker's arming window AND the harness hook timeout are DERIVED FROM ONE
# NUMBER, on purpose. They were two constants in two files — window=600 in
# rewake.py, timeout=15 in the emitted hook — and they drifted 40x apart: the
# harness killed the monitor at 15s while its window was 600s, so it was inert
# for anything arriving after the first 15 seconds and only ever caught mail
# already waiting at turn-end. A monitor the harness kills before its window
# opens is not a monitor (david D10). The hook timeout MUST exceed the window,
# or the monitor is executed for a fraction of the time it was built to run.
# 600s is Claude Code's DOCUMENTED practical maximum for a command hook (and its
# default). Sizing the timeout ABOVE it — the old 660 — risks the harness
# clamping it back to 600, which would silently make timeout == window and
# violate the very invariant D10 established, in a way the probe cannot detect
# because it reads the number we EMITTED, not the number the harness ENFORCED.
# So keep BOTH under the ceiling: window 60s below the timeout, timeout exactly
# at the documented maximum. (david's catch.)
REWAKE_WINDOW_SEC = 540
REWAKE_HOOK_TIMEOUT_SEC = 600  # invariant: strictly greater
# The Stop command injects the window so the value the monitor uses and the
# value the timeout is sized against come from the SAME source and cannot drift.
_STOP_CMD = (
    '[ -n "${AGENTBUS_AGENT:-}" ] && '
    f"AGENTBUS_REWAKE_WINDOW={REWAKE_WINDOW_SEC} "
    '"$HOME/.config/agentbus/stop-rewake.sh"'
)

# The Stop re-waker. Load-bearing properties, each a real failure first:
# dedupe on DELIVERY IDS never on "output exists" (idempotent pending makes
# unread-but-unacked mail a permanent wake source, so "output exists" loops
# forever); hash fallback so a format change degrades to one wake per message
# instead of a loop or silence; credential resolved FROM the agent name;
# `set -a` around the sourcing; AGENTBUS_REWAKE_STATE override so `doctor
# --wake` never poisons the production ledger.
#
# v2 (the reason the first version kept missing messages): the old script
# CHECKED ONCE and exited. As an asyncRewake Stop hook that fires at TURN END,
# check-once could only wake for mail already waiting when the turn finished —
# anything arriving one minute later sat until a human typed. v2 LONG-POLLS:
# after the turn ends it stays armed for a bounded window, polling every
# interval, and exits 2 the moment a genuinely new delivery lands. The window
# is bounded on purpose — a truly idle session eventually goes quiet, which is
# honest, and permanent reachability needs a supervised injector, not a hook.
#
# STOP_REWAKE_VERSION is stamped so `doctor --wake` can refuse to trust a stale
# copy left behind by a client upgrade (david D9).
STOP_REWAKE_VERSION = 3
STOP_REWAKE_SH = r"""#!/bin/sh
# Stop-hook re-wake for AgentBus (installed by `agentbus setup`; SPECS/0021).
# agentbus-rewake-version: 3
#
# THIN wrapper on purpose. Its whole job is the one thing shell does better than
# Python — resolve the per-agent credential with `set -a` sourcing so the child
# inherits an EXPORTED key (an unexported key is the classic wired-but-silent
# failure) — and then hand off to the resilient monitor, which long-polls for a
# bounded window and survives a laptop: wifi drops, DNS loss, suspend/resume,
# via retry+circuit-breaker+failsafe (resilient-circuit). Exit 2 = new mail,
# re-wake the session; exit 0 = nothing, stay idle. It never exits non-zero for
# any other reason, so it cannot break a session.
set -u
if [ -n "${AGENTBUS_AGENT:-}" ] && [ -r "$HOME/.config/agentbus/keys/${AGENTBUS_AGENT}.env" ]; then
    set -a
    . "$HOME/.config/agentbus/keys/${AGENTBUS_AGENT}.env"
    set +a
else
    exit 0
fi
exec agentbus-hook monitor
"""


def _config_dir() -> Path:
    """ONE config directory, honoured by every path that reads config.

    `AGENTBUS_CONFIG_DIR` was documented as "move the whole config directory"
    and moved exactly one thing: `device-id`, which lives in identity.py and
    read the override. Identity, credentials, key files and watch state all
    hardcoded `~/.config/agentbus` and ignored it.

    david measured the consequence, and it is worse than the parts being
    inconsistent:

        AGENTBUS_CONFIG_DIR=/tmp/x agentbus identity  -> a NEW device_id
        AGENTBUS_CONFIG_DIR=/tmp/x agentbus whoami    -> the REAL agent, the
                                                         REAL key, the REAL
                                                         address

    So following the documentation produced a session presenting a fresh
    machine identity while acting with its real credential — precisely the
    mismatch identity derivation exists to prevent, reached by doing what the
    docs say. A half-isolated environment is worse than none, because it looks
    isolated.

    Second consequence, and it is why this was found: on any host that has run
    `signin`, the no-recorded-identity branch added in 0.4.10 is UNREACHABLE by
    the documented route — the signin default resolves through every override.
    That branch shipped verifiable only on hosts that happen never to have
    signed in, which is an accident of configuration and not a test.
    """
    return identity_config_dir()


def _keys_dir() -> Path:
    return _config_dir() / "keys"


def _signin_state_path() -> Path:
    return _config_dir() / "signin.json"


def _say(msg: str) -> None:
    print(msg)


def _write_private(path: Path, content: str) -> bool:
    """Write with 0600, parents 0700 where we create them. True if changed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == content:
        return False
    path.write_text(content)
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return True


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"refusing to touch {path}: it is not valid JSON ({exc}). "
            "A broken settings file silently disables everything in it — "
            "fix it first, then re-run setup."
        ) from exc
    if not isinstance(data, dict):
        raise SystemExit(f"refusing to touch {path}: expected a JSON object")
    return data


def _dump_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n")


# ---------------------------------------------------------------- signin


def _ensure_sealing_key(bus: Any, ui: Any) -> None:
    """Provision this machine's sealing key when the workspace is encrypted (#189).

    ONE EXTRA LINE OF OUTPUT AND NOTHING ELSE TO DO. Farshid's requirement was
    that a customer sees no new complexity: the install is the same single curl
    and the same signin, and if the workspace happens to be sealed the key is
    generated here, registered, and never mentioned again.

    SILENT WHEN THE WORKSPACE IS NOT ENCRYPTED. Generating a key nobody uses
    would leave an unexplained secret on disk, and an operator auditing the box
    later would rightly ask what it was for.
    """
    from . import sealing

    try:
        state = bus._request("GET", "/v1/workspace/pubkeys")
    except Exception:
        # Not an error: this machine is talking to a deployment that predates
        # encryption, and signin must still work against it.
        return
    if not state.get("encrypted"):
        return

    if not bus.agent:
        # An unbound operator key has no agent to publish a key FOR. The key is
        # provisioned by `agentbus setup` in each project instead, where the
        # identity exists. Saying so beats a confusing 404.
        ui.item("encrypted workspace", "sealing key is provisioned per agent by `agentbus setup`")
        return

    private, public = sealing.ensure_keypair(bus.agent)
    del private  # never transmitted, never logged, never returned
    try:
        registered = bus._request(
            "POST", f"/v1/agents/{bus.agent}/pubkey", json={"public_key": public}
        )
    except Exception as exc:
        ui.fail(f"this workspace is ENCRYPTED but the public key could not be registered: {exc}")
        _say("  Until it is, this agent cannot read sealed mail and peers cannot seal to it.")
        return
    ui.ok("encrypted workspace — sealing key ready")
    ui.item("fingerprint", str(registered.get("fingerprint")))
    ui.item("private key", f"{sealing.key_path(bus.agent)}  (0600, never leaves this machine)")


def cmd_signin(args: argparse.Namespace) -> int:
    """Validate the key against the live service BEFORE storing anything."""
    from . import ui

    key = args.key.strip()
    if not key.startswith("ab_sk_"):
        ui.fail("that does not look like an AgentBus key (expected ab_sk_...). Nothing stored.")
        return 1

    ui.banner("sign in — once per machine")
    bus = AgentBus(api_key=key, base_url=args.base_url)
    try:
        who = bus.whoami()
    except AgentBusError as exc:
        ui.fail(f"key REFUSED by {bus.base_url}: {exc}")
        _say("Nothing was stored. Check the key (revoked? truncated? wrong service?).")
        return 1

    key_info = who.get("key") or {}
    workspace = who.get("workspace") or {}
    scope = key_info.get("scope")
    bound = key_info.get("bound_agents") or []

    ui.ok(f"key VERIFIED against {bus.base_url}")
    ui.item("workspace", f"{workspace.get('slug')} ({workspace.get('id')})")
    _ensure_sealing_key(bus, ui)
    ui.item("scope", f"{scope}  (scopes are cumulative — every scope may read)")
    ui.item("bound to", ", ".join(bound) if bound else "(unbound — operator credential)")

    if len(bound) == 1:
        agent = bound[0]
        path = _keys_dir() / f"{agent}.env"
        changed = _write_private(
            path, f"export AGENTBUS_API_KEY={key}\nexport AGENTBUS_AGENT={agent}\n"
        )
        _write_private(
            _signin_state_path(), json.dumps({"default_agent": agent, "operator": False}) + "\n"
        )
        _say(f"  stored:    {path} (0600){'' if changed else '  [unchanged]'}")
        _say("")
        _say(f"Next: cd <your-project> && agentbus setup claude   # wires everything for '{agent}'")
        _say("")
        _say(f"NOTE: this key is BOUND to '{agent}', so it can only ever serve that")
        _say("one agent. `agentbus setup` will work in that agent's project and will")
        _say("REFUSE elsewhere, because a bound key cannot provision anyone else.")
        _say("Running several agents on this machine? Sign in with the WORKSPACE key")
        _say("instead — setup then provisions each project its own agent and its own")
        _say("bound key automatically, and you never type a per-agent secret.")
    elif bound:
        for agent in bound:
            path = _keys_dir() / f"{agent}.env"
            _write_private(path, f"export AGENTBUS_API_KEY={key}\nexport AGENTBUS_AGENT={agent}\n")
            _say(f"  stored:    {path} (0600)")
        _write_private(
            _signin_state_path(), json.dumps({"default_agent": None, "operator": False}) + "\n"
        )
        _say("Next: agentbus setup claude --role <which>   # the key binds several agents")
    else:
        path = _config_dir() / "operator.env"
        changed = _write_private(path, f"export AGENTBUS_API_KEY={key}\n")
        _write_private(
            _signin_state_path(), json.dumps({"default_agent": None, "operator": True}) + "\n"
        )
        ui.item("stored", f"{path} (0600){'' if changed else '  [unchanged]'}")
        _say("")
        _say("Operator credential: setup mints each project its own bound key —")
        _say("you never type a per-agent secret.")
        ui.next_steps("cd <your-project>", "agentbus setup claude")
    return 0


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
        top = root or _git_root_or_none()
        if top is None:
            return None
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


def _git_root_or_none() -> Path | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if out.returncode != 0:
            return None
        return Path(out.stdout.strip()) if out.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        return None


def _derived_name(role: str | None) -> str | None:
    """The agent name this project WOULD get — computed locally, so an error can
    name it. Identity is derived from device+repo+path, so the client already
    knows this without asking the server."""
    if not role:
        return None
    try:
        from . import identity

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
    path = _keys_dir() / f"{agent}.env"
    if not path.exists():
        return None
    return _key_from_env_file(path)


def resolve_credentials(preferred_agent: str | None = None) -> tuple[str | None, str | None]:
    """(api_key, agent) for ordinary CLI verbs when nothing was passed or
    exported. Signin promised "once per machine"; before this fallback the
    promise held only for the two onboarding commands, and every plain verb
    still demanded a hand-exported secret — the exact friction the flow
    exists to delete (clean-slate finding D2).

    Order: this session's OWN identity key file (project setting, else the
    signin default), then the operator credential. Asking to act as a DIFFERENT
    agent never loads that agent's key file — see below.
    """
    # WHO THIS SESSION IS, decided WITHOUT reference to what was asked for.
    # `--agent NAME` used to feed straight into the key lookup, so asking to act
    # as a peer silently loaded THAT PEER'S PRIVATE KEY from keys/NAME.env and
    # acted as them. On a shared host `agentbus --agent mailapi whoami` returned
    # mailapi's identity to a caller holding no credential of their own.
    #
    # `--agent` is an ASSERTION OF IDENTITY, the same as the X-AgentBus-Agent
    # header — it says which agent to act as, not "go find that agent's
    # credential". Conflating the two made cross-agent action the path of least
    # effort rather than a deliberate act. Reported by `runflow`, read-only,
    # with a reproduction.
    #
    # The file permissions were never the control here: every key file is 0600
    # under one UID, so anyone who can run the CLI could already read them. What
    # changed is that you now have to mean it.
    identity = _session_identity()
    # SPECS/0038 + stabilize's container finding: the operator exports
    # AGENTBUS_AGENT as this session's identity ("for one command"). In a fresh
    # container or host with NO project settings.local.json, NO signin, and NO
    # operator.env, that env var is the ONLY record of who this session is — and
    # without it the bound key mounted alongside could never be auto-loaded,
    # forcing a hand source (`set -a; . keys/<agent>.env`). The env var names the
    # session's OWN identity, so loading ITS bound key is exactly the #67
    # guarantee: `--agent PEER` (a DIFFERENT identity) still never loads PEER's
    # key — that requires the operator credential.
    if identity is None:
        env_identity = os.environ.get("AGENTBUS_AGENT")
        if env_identity:
            identity = str(env_identity)
    agent = preferred_agent or identity

    # Auto-load a bound key ONLY for the identity this session actually holds.
    if identity and (not preferred_agent or preferred_agent == identity):
        key = _agent_key(str(identity))
        if key:
            return key, str(identity)

    # Asking to act as somebody else is legitimate — with an OPERATOR key, which
    # may act as any agent by design. It is not legitimate by borrowing their
    # bound credential. Fall through to the operator key, or to nothing.
    op = _operator_key()
    if op:
        return op, agent if agent else None
    return None, None


def doctor_credential_scope(base_url: str | None = None) -> list[str]:
    """#64: what credentials are reachable from the CURRENT directory, at what
    scope — the finding the incident rested on.

    The auto-inherited fallback (user-scope ~/.claude.json / opencode.json MCP
    entry) is exactly the slot that held an UNBOUND OPERATOR key: anything
    running in an unwired directory inherits it, and a `full` key can MINT a
    bound key for any agent and send at platform_attested (forging the
    attestation the whole bus rests on). `agentbus setup` neither discourages
    nor detects a `full` key sitting in that slot.

    Report, naming which config supplied it and at what scope:
      1. this directory's OWN wiring   — .claude/settings.local.json env
      2. the user-scope fallback       — ~/.claude.json projects-less MCP entry
      3. the opencode fallback         — ~/.config/opencode/opencode.json(.c)
      4. the operator credential       — ~/.config/agentbus/operator.env

    The scope of each credential is resolved against the LIVE bus via /whoami
    (the server's own read-only answer, never guessed from the key string — a
    bearer token does not encode scope). A credential reachable by inheritance
    with scope send-or-above is a finding. This is a REPORT (no mutation); the
    scope is always the credential's own, and a bound key is never `full`.
    """
    lines: list[str] = []
    try:
        # 1. THIS DIRECTORY's own wiring.
        local = _load_json(_project_claude_dir() / "settings.local.json")
        proj_agent = (local.get("env") or {}).get("AGENTBUS_AGENT")
        if proj_agent:
            lines.append(
                f"project ({_project_claude_dir()}/settings.local.json): agent {proj_agent}"
            )
    except Exception:
        pass

    # 2. User-scope fallback in ~/.claude.json (Claude). A projects-less MCP
    #    entry is inherited by every directory that has no its-own block.
    try:
        import json as _json

        cj = Path.home() / ".claude.json"
        if cj.exists():
            data = _json.loads(cj.read_text())
            # {global,project} less entry for 'agentbus' -> auto-inherited.
            entry = (data.get("mcpServers") or {}).get("agentbus")
            if entry:
                header = (entry.get("headers") or {}).get("Authorization") or ""
                scope = _scope_of_bearer(header, base_url)
                lines.append(
                    f"user-scope ~/.claude.json agentbus MCP: {scope}" + _inherited_flag(scope)
                )
    except Exception:
        pass

    # 3. opencode fallback config.
    try:
        import json as _json
        import re as _re

        for name in ("opencode.jsonc", "opencode.json"):
            p = Path.home() / ".config" / "opencode" / name
            if not p.exists():
                continue
            text = p.read_text()
            text = _re.sub(r"/\*.*?\*/", "", text, flags=_re.DOTALL)
            text = _re.sub(r"//.*", "", text)
            data = _json.loads(_re.sub(r",(\s*[}\]])", r"\1", text))
            entry = (data.get("mcp") or {}).get("agentbus")
            if entry:
                header = (entry.get("headers") or {}).get("Authorization") or ""
                scope = _scope_of_bearer(header, base_url)
                lines.append(f"opencode {name} agentbus MCP: {scope}" + _inherited_flag(scope))
            break
    except Exception:
        pass

    # 4. The operator credential on disk (deliberate, 0600, not inherited).
    op_path = _config_dir() / "operator.env"
    if op_path.exists():
        scope = "full (operator.env: can MINT — never auto-inherit it)"
        lines.append(f"operator: {op_path} — {scope}")
    return lines


def _inherited_flag(scope: str) -> str:
    """#64: a send-or-above credential reachable by inheritance is a finding —
    and the finding must NAME the escalation, not just label the slot. A `full`
    key in an auto-inherited slot can mint a bound key for any agent and then
    send at platform_attested, forging the attestation the whole bus rests on.
    """
    if scope in ("send", "full", "admin"):
        return (
            "  — FINDING: reachable by inheritance; a "
            + {
                "send": "send-scope key in an inherited slot can act as any "
                "agent it is not bound to — this slot wants `read`, "
                "nothing above",
                "full": "`full` key here can MINT a bound key for any agent "
                "and send at platform_attested — this slot wants "
                "`read`, nothing above",
                "admin": "`admin` key here is worse than full: it can revoke "
                "and purge, and is inherited by every unwired "
                "directory",
            }[scope]
        )
    return ""


def _scope_of_bearer(bearer: str, base_url: str | None = None) -> str:
    """Resolve the scope of a bearer credential against the LIVE bus via
    /whoami — the server's own read-only answer, since a `ab_sk_...` token does
    not encode scope. Failures report the state honestly (unusable,
    unreachable) rather than guessing.
    """
    import re as _re

    m = _re.search(r"ab_sk_[A-Za-z0-9_]+", bearer or "")
    if not m:
        return "unknown"
    try:
        from .client import AgentBus

        who = AgentBus(api_key=m.group(0), base_url=base_url).whoami()
        scope = (who.get("key") or {}).get("scope")
        return scope if isinstance(scope, str) else "unknown"
    except AgentBusError as exc:
        # invalid_api_key is the SERVER's code (src/agentbus/errors.py) for a
        # missing, malformed or revoked key. "unusable" — the credential does
        # not work; "unreachable" — the bus could not be asked at all.
        if exc.code in ("invalid_api_key", "forbidden", "not_found"):
            return f"unusable ({exc.code})"
        return f"unreachable ({exc.code})"
    except Exception:
        return "unknown (resolution failed)"


def explain_refusal(preferred_agent: str | None) -> str | None:
    """Why no credential was resolved, when the reason is the cross-agent guard.

    A refusal that surfaces as the generic "no API key: set AGENTBUS_API_KEY"
    teaches the wrong lesson — the operator exports a key, or worse copies the
    peer's key file, and the guard is defeated by the very message meant to
    explain it. So when the reason is specifically "you asked to be somebody
    else", say that, and say what the two legitimate routes are.
    """
    if not preferred_agent:
        return None
    if not (_keys_dir() / f"{preferred_agent}.env").exists():
        return None

    # DO NOT LEAD WITH "GO AND SOURCE A CREDENTIAL".
    #
    # bob found this text giving the opposite advice to the MCP 403 in the SAME
    # release: that message was rewritten to say explicitly not to go hunting for
    # a key to paste, after sessions did exactly that and one was stopped by a
    # permission classifier that was right to stop it. An agent reading THIS
    # would go and do it, and the wording here is the older, worse one.
    #
    # So the route that needs no secret goes first, and sourcing a key file is
    # labelled an OPERATOR action at a terminal rather than offered as the
    # natural next step for whoever hit the refusal.
    export_hint = (
        f"  * simplest, and needs no credential: run from "
        f"{preferred_agent}'s own project directory, or wire this "
        f"one with `agentbus setup claude --role <role>`\n"
        f"  * OPERATOR ONLY (a human at a terminal — not an agent, "
        f"and not something to go hunting for): source that agent's "
        f"own key file, {_keys_dir()}/{preferred_agent}.env"
    )

    # TWO DIFFERENT REFUSALS, because they have two different fixes and saying
    # the wrong one sends the operator somewhere useless.
    #
    # `futex` reported, and `runflow` reproduced, that running from a directory
    # with no project identity and no signin.json gave "it is not this session's
    # identity" for the caller's OWN agent name. True of the resolver's state
    # and false as a human reads it: they ARE that agent, and the message tells
    # them they are not. The real problem there is that this session has no
    # recorded identity at all, and the fix is to record one — not to go
    # exporting credentials, which is what the other message steers you toward.
    if not _session_identity():
        return (
            f"cannot act as '{preferred_agent}': this session has NO recorded "
            f"identity — no AGENTBUS_AGENT in this project's "
            f".claude/settings.local.json and no signin default — so there is "
            f"nothing to check '{preferred_agent}' against.\n"
            f"This is NOT a claim that you are not {preferred_agent}. It is that "
            f"nothing here says who you are.\n"
            f"  * if this is your project: run `agentbus setup claude` in it\n"
            f"  * if you have signed in on this machine: run from the project "
            f"directory, or re-run `agentbus signin <key>`\n"
            f"{export_hint}"
        )
    return (
        f"refusing to act as '{preferred_agent}': that agent has a bound key on "
        f"this machine, but it is not this session's identity "
        f"(this session is '{_session_identity()}'), and --agent says WHICH "
        f"AGENT TO ACT AS, not 'load that agent's credential'.\n"
        f"{export_hint}"
    )


def _key_from_env_file(path: Path) -> str | None:
    for raw in path.read_text().splitlines():
        stripped = raw.removeprefix("export ").strip()
        if stripped.startswith("AGENTBUS_API_KEY="):
            return stripped.split("=", 1)[1]
    return None


def _remove_hook_entry(hooks: dict[str, Any], event: str, marker: str) -> bool:
    """Drop OUR entry for this event, leaving every foreign hook untouched.

    Idempotency has to mean CONVERGENCE, not just "adding twice is safe". When
    the plugin takes over the wiring, the copies setup wrote earlier must go —
    otherwise an upgrade path silently leaves both, and every greeting, catch-up
    and wake fires twice, which reads as the platform malfunctioning rather than
    as a stale config.
    """
    groups = hooks.get(event)
    if not isinstance(groups, list):
        return False
    changed = False
    for group in list(groups):
        kept = [h for h in group.get("hooks", []) if marker not in str(h.get("command", ""))]
        if len(kept) != len(group.get("hooks", [])):
            changed = True
            group["hooks"] = kept
        if not group.get("hooks"):
            groups.remove(group)
    if not groups:
        hooks.pop(event, None)
    return changed


def _plugin_provides_wake(settings: dict[str, Any]) -> bool:
    """True when the agentbus PLUGIN is enabled, so it owns the wake.

    The plugin ships a `monitors` entry that runs for the LIFETIME of the
    session — strictly better than a Stop hook, which Claude Code caps at its
    documented 600s and which spawns one process per turn. If both were wired
    the same message would wake the session twice, so setup defers: the plugin
    wins and the Stop hook is not written (SPECS/0022).
    """
    enabled = settings.get("enabledPlugins") or {}
    return any(str(k).split("@", 1)[0] == "agentbus" and v for k, v in enabled.items())


def _ensure_hook_entry(
    hooks: dict[str, Any], event: str, command: str, extra: dict[str, Any] | None = None
) -> str:
    """Idempotent merge of OUR hook into settings hooks, leaving all else alone.

    Returns 'added' | 'updated' | 'ok'. Ours is any command hook whose command
    carries our marker; foreign entries are never touched.
    """
    marker = _MARKER_REWAKE if _MARKER_REWAKE in command else _MARKER_HOOK
    groups = hooks.setdefault(event, [])
    if not isinstance(groups, list):
        raise SystemExit(f"hooks.{event} is not a list; refusing to touch it")
    for group in groups:
        for hook in group.get("hooks", []):
            if hook.get("type") == "command" and marker in str(hook.get("command", "")):
                changed = False
                if hook.get("command") != command:
                    hook["command"] = command
                    changed = True
                for k, v in (extra or {}).items():
                    if hook.get(k) != v:
                        hook[k] = v
                        changed = True
                return "updated" if changed else "ok"
    entry: dict[str, Any] = {"type": "command", "command": command, "timeout": 15}
    entry.update(extra or {})
    groups.append({"hooks": [entry]})
    return "added"


def _provision_project_agent(
    args: argparse.Namespace, report: list[str], harness: str
) -> str | None:
    """The harness-independent core of setup: resolve the project's agent
    identity, register it, and ensure its bound key exists.

    Steps 1-3 of `_setup_claude`, shared verbatim by every harness so the
    trickiest logic — identity resolution, the reinstall guard (D3), the
    bound-key-refusal message — has ONE implementation, not one per harness.
    Returns the agent name on success, or None after printing the refusal.
    """
    base_url = args.base_url

    # 1. Who is this project's agent, and with which credential?
    provenance: list[str] = []
    name = _resolve_agent_name(explain=provenance)
    role = getattr(args, "role", None)
    operator = _operator_key()
    if name is None and role is None:
        # DERIVE THE ROLE FROM THE DIRECTORY NAME rather than failing (#116).
        #
        # The installer and signin both print `agentbus setup claude` BARE as
        # the next step; on the FreeBSD onboarding the operator ran exactly
        # that and was refused with "pass a role" — we printed a command that
        # fails. The directory basename IS the operator's chosen project name:
        # deriving from it is the same doctrine as everything else here
        # (identity from machine+repo+path, never asked twice), it is strictly
        # project-local (nothing machine-global to leak across checkouts, the
        # #90/#95 failure family), it is deterministic on reopen, and it is
        # visible in the agent name it produces — a wrong guess announces
        # itself. Sanitized to the agent charset; a name that sanitizes away
        # (e.g. all CJK) falls through to the explicit-role error.
        derived = re.sub(r"[^a-z0-9._-]", "-", Path.cwd().name.lower()).strip("-._")[:40]
        if derived:
            role = derived
            _say(f"role derived from this directory's name: {role}")
            _say(
                f"  (pass --role <name> to choose differently: agentbus setup {harness} --role <name>)"
            )
    if name is None and role is None:
        _say("cannot determine this project's agent identity.")
        _say("  Either sign in with an agent-bound key (agentbus signin <key>),")
        _say(f"  or pass a role so one can be derived: agentbus setup {harness} --role <role>")
        return None

    ephemeral = os.environ.get("AGENTBUS_SETUP_EPHEMERAL") == "1"

    # VALIDATE THE STORED KEY BEFORE IT WINS OVER THE OPERATOR KEY (#110).
    #
    # A per-agent key file used to be trusted on existence alone. After the
    # 2026-08-13 workspace reset this host held 46 dead key files, and setup in
    # a previously-wired project picked the dead file over the operator key the
    # operator had verified SECONDS earlier — then failed with 'API key is
    # unknown or revoked', one command after signin printed VERIFIED. A file on
    # disk is a record that a key EXISTED, not that it still works; only the
    # bus can say that. On rejection, say which file was dead and fall through
    # to the operator credential — that is what it is for.
    stored_key = _agent_key(name) if name else None
    if stored_key and operator and stored_key != operator:
        from .client import AuthError

        try:
            mine = AgentBus(api_key=stored_key, base_url=base_url).whoami()
            # AND DOES IT BELONG TO THE WORKSPACE WE JUST SIGNED INTO?
            #
            # "Is the key alive" is not the question. A stored key for a
            # DIFFERENT but still-live workspace answers whoami() perfectly
            # happily, and setup would then register this agent into that other
            # workspace while printing the one the operator actually signed
            # into. A silent wrong-workspace registration is worse than the
            # clear failure that led here.
            #
            # The operator typed a key seconds ago and watched it verify. That
            # is the intent; a file left on disk from a previous life is not.
            here = AgentBus(api_key=operator, base_url=base_url).whoami()
            if mine.get("workspace") != here.get("workspace"):
                _say(
                    f"  stored key for '{name}' belongs to workspace "
                    f"'{mine.get('workspace')}', not '{here.get('workspace')}'; using the"
                )
                _say("  operator credential instead. A fresh bound key will be minted.")
                with contextlib.suppress(OSError):
                    (_keys_dir() / f"{name}.env").rename(_keys_dir() / f"{name}.env.other")
                stored_key = None
        except AgentBusError as exc:
            # WHICH FAILURES PROVE THE KEY IS DEAD.
            #
            # An auth rejection does. So does `workspace_deleted`: the key is
            # perfectly well-formed, it just belongs to a workspace that no
            # longer exists, and no amount of retrying will change that.
            #
            # This used to catch only AuthError. 410 is not in the client's
            # error map, so a deleted workspace arrived as a plain
            # AgentBusError and hit `pass` — the guard written for exactly this
            # case could not see it. Measured on a macOS box that still held a
            # bound key from a deleted workspace: signin verified the NEW key
            # and printed the NEW workspace, then setup registered with the OLD
            # stored key and failed with "this workspace has been deleted",
            # naming a file the operator had no reason to suspect.
            #
            # A TransportError still says NOTHING about the key (bus down,
            # DNS), and renaming a good credential on that evidence would
            # destroy it every time the bus blipped. So the test stays narrow:
            # an explicit verdict from the server, never a failure to reach it.
            dead = isinstance(exc, AuthError) or getattr(exc, "code", "") in (
                "workspace_deleted",
                "workspace_suspended",
            )
            if dead:
                _say(f"  stored key for '{name}' is dead ({exc.code}); using the operator")
                _say("  credential instead. A fresh bound key will be minted.")
                with contextlib.suppress(OSError):
                    (_keys_dir() / f"{name}.env").rename(_keys_dir() / f"{name}.env.dead")
                stored_key = None
    register_key = stored_key or operator
    if register_key is None:
        target = name or _derived_name(role) or f"<role '{role}'>"
        bound = _signed_in_bound_agent()
        _say(f"cannot provision {target} in this project.")
        if bound:
            # The real problem, which the old message named neither: they signed
            # in with a key BOUND to a different agent, so nothing on this
            # machine is allowed to create or credential this one. The old text
            # said "no credential for 'None' ... run signin", and signin is the
            # command that had just succeeded — so the user re-ran it with the
            # same key and hit the same wall.
            _say(f"  You signed in with a key BOUND to '{bound}'. A bound key can act")
            _say(f"  only as '{bound}', so it cannot register or mint a key for another")
            _say("  agent — which is exactly what a second project needs.")
            _say("")
            _say("  Fix: sign in with your WORKSPACE key instead. setup then provisions")
            _say("  each project its own agent and its own bound key, automatically:")
            _say("      agentbus signin <workspace key>")
            _say(f"      agentbus setup {harness}")
        else:
            _say("  No credential is stored for this machine yet.")
            _say("  Fix: agentbus signin <workspace key>")
        return None

    # 2. Register (idempotent server-side; derivable identity when role-based).
    bus = AgentBus(api_key=register_key, base_url=base_url)

    # Reinstall guard (clean-slate finding D3): identity derives from the
    # device-id file, so a wiped config directory plus a role-based setup
    # silently mints a STRANGER while the original agent goes quiet holding
    # its inbox — and the failure prints as success. If an active agent for
    # this role and repo already exists under a DIFFERENT device-id, stop and
    # make the choice explicit instead of guessing.
    if name is None and role and not getattr(args, "force_new", False):
        from . import identity

        remote = _git_remote_or_none()
        fingerprint = identity.repo_fingerprint(remote) if remote else None
        local_device = identity.device_id()
        try:
            peers = bus.phonebook(repo_fingerprint=fingerprint) if fingerprint else []
        except AgentBusError:
            peers = []
        strangers = [
            p
            for p in peers
            if p.get("role") == role
            and p.get("device_hash")
            and p["device_hash"] != _device_hash(local_device)
            and not p.get("ephemeral")
        ]
        if strangers:
            existing = strangers[0]["name"]
            _say(
                f"STOP: an agent '{existing}' for role '{role}' on this repo already "
                "exists, registered from a DIFFERENT device-id."
            )
            _say(
                "  If this machine was reinstalled, its identity file was lost. "
                f"Restore {_config_dir() / 'device-id'} from backup and re-run setup — "
                "it will recover the SAME agent, address and inbox."
            )
            _say(
                "  To deliberately create a NEW agent instead: "
                f"agentbus setup {harness} --role <role> --force-new"
            )
            _say("Nothing was changed.")
            return None
    try:
        result = bus.register(
            name,
            role=role or (name if name else None),
            workdir=str(Path.cwd()),
            repo_remote=_git_remote_or_none(),
            ephemeral=True if ephemeral else None,
        )
    except AgentBusError as exc:
        _say(f"registration failed: {exc}")
        # #109: NAME THE CREDENTIAL THAT FAILED. 'API key is unknown or
        # revoked' with no source sent the operator hunting — was it the
        # stored per-agent file, or the signin credential that printed
        # VERIFIED a minute ago? Two different remedies; the message named
        # neither. The key_id identifies WHICH key without exposing a secret:
        # keys are `ab_sk_<key_id>_<secret>` and the dashboard Keys page lists
        # the key_id half in full.
        key_id = ""
        with contextlib.suppress(Exception):
            if register_key.startswith("ab_sk_"):
                key_id = register_key.split("_")[2]
        if register_key and register_key == stored_key and name:
            _say(f"  credential used: stored key file {_keys_dir() / (name + '.env')}")
        elif register_key == operator:
            _say("  credential used: the signin/operator credential (agentbus signin)")
        if key_id:
            _say(f"  key id: {key_id}…  (revoke/inspect it in the dashboard Keys page)")
        return None
    registered = str(result["agent"]["name"])
    if name and registered != name:
        # NEVER adopt a rename (#90) — but DISTINGUISH RECOGNITION FROM
        # RENAMING (#156). Farshid hit this on the tokengate repo: the server
        # answered an EXISTING row it had matched by session identity, and
        # this branch told him his name was possibly "invalid" — a refusal
        # worded for the renaming case, aimed at the recognition case. The
        # discriminator is measurable: the register response carries the
        # answered row's device_hash (sha256 of its stored device_id) — equal
        # to OUR device's hash means the server recognized this machine's
        # session, not renamed a stranger.
        recognized = False
        with contextlib.suppress(Exception):
            import hashlib

            from .identity import device_id as _device_id

            local_device = _device_id()
            recognized = bool(
                local_device
                and result["agent"].get("device_hash")
                == hashlib.sha256(local_device.encode()).hexdigest()
            )
        if recognized:
            _say(
                f"The server RECOGNIZES this machine and project as '{registered}' — an "
                f"identity that already exists here (same device, matched by session "
                f"identity). You asked for '{name}', so nothing was wired without your say."
            )
            changed = result.get("identity_changed")
            if changed:
                _say(f"  what moved since it was registered: {', '.join(changed)}")
            _say("  ADOPT the existing identity (keeps its address, inbox and history):")
            _say(
                f"    mkdir -p .agentbus && printf '%s\\n' '{registered}' > .agentbus/agent"
                "   # then re-run this setup"
            )
            _say("  or keep your requested name by retiring the old identity first:")
            _say(f"    agentbus retire {registered} --agent {registered}")
            return None
        _say(
            f"REFUSING: asked to register '{name}' but the server answered "
            f"'{registered}'. The identity you name is the identity you get — "
            "nothing was wired. If the name is invalid, the server's error "
            "says the exact rename; choose it yourself and re-run."
        )
        return None
    name = registered
    report.extend(provenance)
    report.append(f"registered: {name}  ({result['address']})")

    # 3. Ensure the agent's own bound key exists (mint with the operator key
    #    if needed — minting de-escalates; this is its designed use).
    if _agent_key(name) is None:
        if operator is None:
            _say(f"'{name}' has no key file and there is no operator credential to mint one.")
            _say("  Sign in with the workspace key once: agentbus signin <key>")
            return None
        op_bus = AgentBus(api_key=operator, base_url=base_url)
        try:
            minted = op_bus.mint_key(scope="send", agents=[name], label=f"setup-{name}")
        except AgentBusError as exc:
            _say(f"could not mint a bound key for '{name}': {exc}")
            return None
        secret = minted.get("key") or minted.get("api_key")
        if not secret:
            _say("mint succeeded but no secret in the response; refusing to continue.")
            return None
        _write_private(
            _keys_dir() / f"{name}.env",
            f"export AGENTBUS_API_KEY={secret}\nexport AGENTBUS_AGENT={name}\n",
        )
        report.append(f"minted bound send key for {name} -> {_keys_dir()}/{name}.env (0600)")
    else:
        report.append(f"key: {_keys_dir()}/{name}.env (existing)")
    # The agent name from register() is server-returned; coerce to str so mypy
    # can see the function's str | None contract instead of Any.
    return str(name)


def skill_state(base_url: str | None = None) -> tuple[str, str]:
    """(state, detail) for the installed skill vs the one the server serves.

    #196: an installed SKILL.md could not tell it was stale. `setup` compares
    and reports, but nothing else did — so every agent wired before a skill
    change kept the old copy on disk indefinitely, and the only way to find out
    was to re-run setup and watch whether it said "updated". The content is
    current at the source; an installed copy is only as fresh as the last setup
    run, and nothing anywhere reported the difference.

    COMPARED BY CONTENT HASH, NOT BY A VERSION STAMP IN THE FILE, and that is a
    deliberate deviation from the ticket's literal wording. A stamp is a second
    source of truth that an editor can forget to bump — this repo already runs a
    pre-commit guard because exactly that happened between the served skill and
    the plugin's copy. A hash of the bytes cannot drift from the bytes.

    The cost of that choice is that the check needs the network, so:

        current   installed bytes == served bytes
        stale     they differ, and the refresh command is named
        missing   nothing installed
        unknown   the server could not be asked — NEVER reported as current,
                  because a check that cannot fail is the thing this fixes
    """
    import hashlib

    skill_path = Path.home() / ".claude" / "skills" / "agentbus" / "SKILL.md"
    root = (base_url or "https://agentbus.rodmena.co.uk").rstrip("/")
    if not skill_path.exists():
        # `agentbus setup claude` remains the fresh-install path: on a machine
        # with no prior identity yet, setup does the full wire-up and skill
        # install in one command. `refresh-skill` also works but does not do
        # the other setup steps (identity, keys, hooks, MCP).
        return "missing", f"no skill at {skill_path} — run `agentbus setup claude`"
    installed = skill_path.read_bytes()
    try:
        import httpx

        resp = httpx.get(f"{root}/skills/claude-code.md", timeout=10.0)
        if resp.status_code != 200:
            return "unknown", f"served {resp.status_code}; NOT checked"
        served = resp.content
    except Exception as exc:
        return "unknown", f"could not reach {root}: {str(exc)[:60]} — NOT checked"

    if installed == served:
        return "current", f"{len(installed)} bytes, matches the served copy"
    # NAME THE COMMAND THAT WORKS. `agentbus setup claude` used to be the
    # advice, but setup refuses when the current cwd's repo fingerprint does
    # not match the fingerprint the server has on file for this agent
    # (protective: it stops accidental re-registration). Reported by peer
    # agentbus-ui-c760a1 (thread 01M06Q4Y282JDK23NV92WH6DJP): the doctor
    # recipe was unusable in exactly the scenario the warning was about.
    # `agentbus refresh-skill` is skill-only, no registration flow, works
    # from any cwd.
    return (
        "stale",
        f"installed sha256 {hashlib.sha256(installed).hexdigest()[:12]} != "
        f"served {hashlib.sha256(served).hexdigest()[:12]} "
        f"({len(installed)} vs {len(served)} bytes) — refresh: agentbus refresh-skill",
    )


def refresh_skill(base_url: str | None = None) -> tuple[str, str]:
    """Fetch the served SKILL.md and install it, without touching registration.

    Extracted from `_setup_claude` step 7 so an operator whose repo has
    since moved (or who is on a machine where the registered fingerprint
    predates this checkout) can still comply with a `doctor` "skill:
    STALE" warning. Setup's registration guard refuses to re-point an
    agent across repos, and that guard is correct — but it should not be
    on the path of a docs refresh. Reported by peer agentbus-ui-c760a1
    (thread 01M06Q4Y282JDK23NV92WH6DJP).

    Returns (state, detail). state is one of:
      "updated"     the served copy overwrote a differing installed copy
                    (previous saved to SKILL.md.bak)
      "current"     the installed copy already matches served
      "installed"   nothing installed before; the served copy was written
      "unreachable" the server did not answer 200 with a non-trivial body
    """
    import httpx

    skill_path = Path.home() / ".claude" / "skills" / "agentbus" / "SKILL.md"
    url = f"{(base_url or 'https://agentbus.rodmena.co.uk').rstrip('/')}/skills/claude-code.md"
    try:
        resp = httpx.get(url, timeout=15)
    except Exception as exc:
        return "unreachable", f"could not fetch {url}: {exc}"

    if resp.status_code != 200 or len(resp.text) <= 500:
        return "unreachable", (
            f"served {resp.status_code} ({len(resp.text)} bytes) at {url}; "
            "refusing to install a suspiciously small body"
        )

    if skill_path.exists() and skill_path.read_text() == resp.text:
        return "current", f"{len(resp.text)} bytes, already matches the served copy"

    skill_path.parent.mkdir(parents=True, exist_ok=True)
    noted = ""
    if skill_path.exists():
        # Preserve a hand-authored skill: same D4 lesson as setup step 7.
        bak = skill_path.with_suffix(".md.bak")
        bak.write_text(skill_path.read_text())
        noted = ", previous saved to SKILL.md.bak"
        skill_path.write_text(resp.text)
        return "updated", f"{len(resp.text)} bytes{noted}"

    skill_path.write_text(resp.text)
    return "installed", f"{len(resp.text)} bytes (fresh install)"


def _setup_claude(args: argparse.Namespace) -> int:
    report: list[str] = []
    base_url = args.base_url

    # 1-3. Shared identity resolution, registration, and bound-key minting.
    name = _provision_project_agent(args, report, "claude")
    if name is None:
        return 1

    # 4. Per-project identity, declared in the WORKTREE and in the harness file.
    #
    # `.agentbus/agent` is the primary record and the one every harness reads.
    # It is written at the repo root rather than the cwd so the identity is a
    # property of the checkout, which is what makes two worktrees of one repo
    # two agents without any machine-global state deciding that.
    #
    # settings.local.json is still written because Claude Code turns its `env`
    # block into real environment variables for hooks, which is the mechanism
    # that puts AGENTBUS_AGENT in the session's environment in the first place.
    # The two must never disagree, so both are written from the same `name`.
    _write_worktree_identity(name, report)

    local_path = _project_claude_dir() / "settings.local.json"
    local = _load_json(local_path)
    if (local.get("env") or {}).get("AGENTBUS_AGENT") != name:
        local.setdefault("env", {})["AGENTBUS_AGENT"] = name
        _dump_json(local_path, local)
        report.append(f"project identity: {local_path} env.AGENTBUS_AGENT={name}")
    else:
        report.append(f"project identity: {local_path} (already {name})")
    gitignore = Path.cwd() / ".gitignore"
    if (Path.cwd() / ".git").exists():
        lines = gitignore.read_text().splitlines() if gitignore.exists() else []
        added = [
            entry for entry in (".claude/settings.local.json", ".agentbus/") if entry not in lines
        ]
        if added:
            with gitignore.open("a", encoding="utf-8") as fh:
                for entry in added:
                    fh.write(f"{entry}\n")
            report.append(f"gitignore: added {', '.join(added)}")

    # 5. The re-waker script.
    rewake_path = _config_dir() / "stop-rewake.sh"
    if not rewake_path.exists() or rewake_path.read_text() != STOP_REWAKE_SH:
        rewake_path.parent.mkdir(parents=True, exist_ok=True)
        rewake_path.write_text(STOP_REWAKE_SH)
        report.append(f"re-waker: {rewake_path} (installed)")
    else:
        report.append(f"re-waker: {rewake_path} (current)")
    rewake_path.chmod(0o755)

    # 6. Global hooks — merge into ~/.claude/settings.json, foreign entries
    #    untouched byte-for-byte.
    settings_path = Path.home() / ".claude" / "settings.json"
    settings = _load_json(settings_path)
    hooks = settings.setdefault("hooks", {})
    states: dict[str, str] = {}
    if _plugin_provides_wake(settings):
        # The plugin ships ALL of it — both passive hooks AND the monitor. Writing
        # our own copies alongside would double every greeting, every per-turn
        # catch-up and every wake, which reads as the platform malfunctioning.
        removed = [
            e
            for e, m in (
                ("SessionStart", _MARKER_HOOK),
                ("UserPromptSubmit", _MARKER_HOOK),
                ("Stop", _MARKER_REWAKE),
            )
            if _remove_hook_entry(hooks, e, m)
        ]
        report.append(
            "hooks + wake: provided by the agentbus PLUGIN (session-start, "
            "per-turn catch-up, and a monitor that runs for the whole "
            "session) — none wired here, so nothing fires twice"
        )
        if removed:
            report.append(f"removed our now-duplicate hooks: {', '.join(removed)}")
    else:
        states = {
            "SessionStart": _ensure_hook_entry(
                hooks, "SessionStart", _SESSION_START_CMD, {"timeout": 10}
            ),
            "UserPromptSubmit": _ensure_hook_entry(
                hooks, "UserPromptSubmit", _PENDING_CMD, {"timeout": 10}
            ),
        }
        states["Stop"] = _ensure_hook_entry(
            hooks,
            "Stop",
            _STOP_CMD,
            {
                "timeout": REWAKE_HOOK_TIMEOUT_SEC,
                "asyncRewake": True,
                "rewakeSummary": "AgentBus mail arrived",
            },
        )
    if any(v != "ok" for v in states.values()) or _plugin_provides_wake(settings):
        _dump_json(settings_path, settings)
    hook_report = ", ".join(f"{k}={v}" for k, v in states.items())
    if hook_report:
        report.append(f"hooks: {hook_report}")

    # 6b. #189 — the sealing key, when the workspace is encrypted. Placed here
    #     because the agent identity now exists, which signin cannot assume:
    #     an unbound operator key has no agent to publish a key FOR.
    try:
        from . import sealing as _sealing

        _bus = AgentBus(base_url=base_url, agent=name)
        _state = _bus._request("GET", "/v1/workspace/pubkeys")
        if _state.get("encrypted"):
            _private, _public = _sealing.ensure_keypair(name)
            del _private
            _reg = _bus._request("POST", f"/v1/agents/{name}/pubkey", json={"public_key": _public})
            report.append(
                f"sealing key: {_sealing.key_path(name)} (0600) "
                f"registered as {_reg.get('fingerprint')}"
            )
    except Exception as exc:
        # NEVER FAIL SETUP OVER THIS. Registration is retried on the next run,
        # and a half-wired project is worse than an unsealed one — but say it
        # loudly, because until it succeeds this agent cannot read sealed mail.
        report.append(f"sealing key: NOT REGISTERED ({exc}) — rerun `agentbus setup`")

    # 7. The skill: llms.txt calls it part of setup, so setup installs it
    #    (clean-slate finding D4). Served canonically; installed if changed.
    #    The fetch/install/backup logic itself lives in `refresh_skill` so
    #    `agentbus refresh-skill` can share the same path without going
    #    through registration (peer agentbus-ui-c760a1's ask).
    state, detail = refresh_skill(base_url=base_url)
    if state in ("updated", "installed"):
        # Mirror the historic setup line ("skill: updated" or "skill: updated,
        # previous saved to SKILL.md.bak") so existing operator eyes still
        # find it in the setup report. Local `noted` name preserved so the
        # inspect-based regression test in tests/test_installed_skill_knows_it_is_stale.py
        # (test_setup_still_says_when_the_skill_was_already_current) keeps passing.
        noted = ", previous saved to SKILL.md.bak" if "SKILL.md.bak" in detail else ""
        report.append(f"skill: updated{noted}")
    elif state == "current":
        report.append("skill: current")
    else:
        report.append(f"skill: NOT installed ({detail}) — re-run setup when reachable")

    # 8. MCP entry — wired without ever printing the secret. The first version
    #    echoed the full bearer in a suggested command; agent terminals are
    #    transcribed, so the key landed verbatim in a peer's session log the
    #    first time setup ran outside this repo (finding D7). Now setup runs
    #    `claude mcp add` itself when the binary exists, else writes a 0700
    #    helper and prints only the PATH.
    import shutil

    key_for_mcp = _agent_key(name)
    mcp_url = f"{(base_url or 'https://agentbus.rodmena.co.uk').rstrip('/')}/mcp"
    claude_bin = shutil.which("claude")
    wired = False
    if claude_bin and key_for_mcp:
        # DO NOT TRUST `claude mcp get`. It succeeds when a GLOBAL entry exists,
        # so setup reported "already configured for this project" while the
        # project actually inherited the machine's read-scope key — which cannot
        # register and cannot send. The customer was told they were wired, then
        # hit a 403 on their first action and had to go and find out why.
        #
        # Check for a PROJECT-scoped entry specifically, and treat a global-only
        # match as NOT configured, because for this agent it is not.
        # ASK CLAUDE, NEVER PARSE ITS FILES (#122). Detection used to read
        # ~/.claude.json's per-project mcpServers — Claude 2.1.231 moved
        # local-scope MCP storage, the probe found nothing forever, and setup
        # printed the fallback-helper line ("run once in the project...") on a
        # box where the entry existed and was CONNECTED. Homework printed for
        # work already done. `claude mcp get` in the project cwd is the
        # storage-format-proof question; "Local config" in its output is the
        # project-scope answer (a global-only entry says otherwise, which is
        # the #64 trap the old file-parse existed to avoid).
        project_entry = False
        try:
            got = subprocess.run(
                [claude_bin, "mcp", "get", "agentbus"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                cwd=str(Path.cwd()),
            )
            project_entry = got.returncode == 0 and "Local config" in got.stdout
        except (OSError, subprocess.SubprocessError):
            project_entry = False
        if project_entry:
            # AN EXISTING ENTRY IS ONLY CORRECT IF ITS KEY IS CURRENT (#123).
            # After the workspace reset, a project's MCP entry still carried the
            # RETIRED agent's key; setup said "already configured" and every
            # mcp__agentbus__* call failed agent_retired while the CLI worked —
            # two components disagreeing about who this agent is. `claude mcp
            # get` does not print headers, so the key cannot be compared in
            # place: remove and re-add with the CURRENT bound key. Idempotent,
            # cheap, and converges the entry on every setup run.
            subprocess.run(
                [claude_bin, "mcp", "remove", "-s", "local", "agentbus"],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                cwd=str(Path.cwd()),
            )
            readd = subprocess.run(
                [
                    claude_bin,
                    "mcp",
                    "add",
                    "-s",
                    "local",
                    "--transport",
                    "http",
                    "agentbus",
                    mcp_url,
                    "--header",
                    f"Authorization: Bearer {key_for_mcp}",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
                cwd=str(Path.cwd()),
            )
            if readd.returncode == 0:
                report.append("mcp: refreshed with the current key — restart the session to apply")
                wired = True
            else:
                report.append(
                    "mcp: entry exists but could not be refreshed — restart and re-run setup"
                )
                wired = True
        else:
            add = subprocess.run(
                [
                    claude_bin,
                    "mcp",
                    "add",
                    "-s",
                    "local",
                    "--transport",
                    "http",
                    "agentbus",
                    mcp_url,
                    "--header",
                    f"Authorization: Bearer {key_for_mcp}",
                ],
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            # "already exists" is SUCCESS, whatever the exit code — the state
            # we want is the state that holds.
            if add.returncode == 0 or "already exists" in (add.stdout + add.stderr):
                report.append("mcp: wired at project scope — restart the session to connect")
                wired = True
    if not wired and key_for_mcp:
        helper = _keys_dir() / f"{name}-mcp-add.sh"
        helper_body = (
            "#!/bin/sh\n"
            "exec claude mcp add -s local --transport http "
            f"agentbus {mcp_url} "
            f'--header "Authorization: Bearer {key_for_mcp}"\n'
        )
        _write_private(helper, helper_body)
        helper.chmod(0o700)
        report.append(f"mcp: run once in the project, then restart: sh {helper}")

    # 9. #64: warn if a send-or-above credential sits in an auto-inherited
    #    fallback slot. `setup` never WRITES one there — a fallback wants `read`
    #    scope, nothing above — but it can detect one an operator left behind.
    #    This is exactly the finding the gate incident rested on: a `full` key
    #    in the user-scope ~/.claude.json slot is inherited by every unwired
    #    directory, and can mint a bound key for any agent.
    try:
        findings = [ln for ln in doctor_credential_scope(base_url=base_url) if "FINDING" in ln]
        if findings:
            _say("WARNING — a send-or-above credential is reachable by inheritance:")
            for ln in findings:
                _say(f"  {ln}")
            _say(
                "  A fallback slot wants a READ-scope key, nothing above. "
                "Replace it with a read-scope key from the dashboard, then "
                "re-run `agentbus doctor` to confirm."
            )
            report.append("credential scope: WARNING (inherited send-or-above credential found)")
        else:
            report.append("credential scope: ok (no inherited send-or-above credential)")
    except Exception as exc:
        report.append(f"credential scope: not checked ({exc})")

    from . import ui

    ui.banner("setup claude — everything wired, nothing foreign touched")
    for line in report:
        # `label: value` report lines get the aligned treatment; frame lines
        # (indented continuations) pass through untouched.
        if ": " in line and not line.startswith(" "):
            label, _, value = line.partition(": ")
            ui.item(label, value)
        else:
            _say(f"  {line}")
    # BEING REACHABLE IS NOT BEING FINDABLE, and setup previously stopped at
    # the first. A new agent ended up addressable only by a name nobody knows,
    # so peers either hardcoded it or never found it at all — and `tag:` routing
    # (#171), which exists precisely so address lists cannot go stale, was
    # unreachable from the one screen every agent reads exactly once.
    #
    # One line, because the rest belongs in the skill this just installed:
    # onboarding's job is sign in, get woken, be found.
    ui.next_steps(
        "restart this Claude session (the monitor arms at session start)",
        "agentbus doctor --wake   # prove the wake, don't assume it",
        "agentbus tag --set skill=<what-you-do> --set team=<yours>"
        "   # be findable: peers route by tag:skill=... rather than by name",
        # The other half of being a good citizen, and the one an agent needs
        # BEFORE it is first interrupted rather than after: it is exempt from
        # nothing here because being reachable without being able to say "not
        # now" is what made #187 a ticket.
        "agentbus keys sign   # let peers PROVE you sent your messages, not just believe us (#173)",
        "agentbus status dnd --for 3600"
        "   # when you need to concentrate. Urgent still reaches you; the rest"
        " is held and delivered when it clears. `agentbus quickref` for the rest.",
    )
    return 0


def _git_remote_or_none() -> str | None:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


# The opencode plugin, referenced by its documented npm name (SPECS/0041:
# "SHALL be installed only through opencode's documented plugin mechanism
# (npm package + `plugin` array in `opencode.json`), NEVER by dropping the file
# into the global auto-load directory"). `agentbus setup opencode` writes it to
# the project's plugin array; `opencode plugin <module>` is how it lands.
OPENCODE_PLUGIN_NPM = "@rodmena/agentbus-opencode"


def _setup_opencode(args: argparse.Namespace) -> int:
    """Wire opencode: project-level identity, the plugin, and the wake.

    THE HARNESS-SPECIFIC ACTIVE-TRIGGER FACT: opencode has NO Stop+asyncRewake
    hook (that is Claude Code's mechanism). Its wake is the PLUGIN consuming
    the agent's SSE stream itself and calling promptAsync — the plugin IS the
    active trigger, so there is no re-waker script to install here. Everything
    setup writes is the identity the plugin reads.

    Per-project identity is a PROJECT-LEVEL opencode.json with an mcp.agentbus
    entry carrying this agent's own bound key. opencode merges it over the
    global config (verified), and the plugin reads the project level before
    the global (agentbus.ts resolveKey) — so a wired project is served by ITS
    OWN credential, which is the no-bypass leg #83 calls for. A session with
    no project-level entry falls back to the global config, which is the
    historical behaviour for unwired directories.
    """
    report: list[str] = []
    base_url = args.base_url

    # 1-3. Shared identity resolution, registration, and bound-key minting.
    name = _provision_project_agent(args, report, "opencode")
    if name is None:
        return 1

    # 4. Project-level opencode.json — merge, never replace. A foreign project
    #    config (MCP servers, provider blocks) must survive byte-for-byte, the
    #    same idempotency law that governs the claude harness. opencode itself
    #    is written as opencode.json; both spellings are read, .jsonc first.
    oc_path = Path.cwd() / "opencode.json"
    oc_data = _load_json(oc_path) if oc_path.exists() else {}
    key_for_mcp = _agent_key(name)
    mcp_url = f"{(base_url or 'https://agentbus.rodmena.co.uk').rstrip('/')}/mcp"

    existing = oc_data.get("mcp", {}).get("agentbus")
    changed = False
    if key_for_mcp:
        want_auth = f"Bearer {key_for_mcp}"
        current_auth = None
        if isinstance(existing, dict):
            headers = existing.get("headers") or {}
            current_auth = headers.get("Authorization") if isinstance(headers, dict) else None
        if current_auth != want_auth:
            oc_data.setdefault("mcp", {})["agentbus"] = {
                "type": "remote",
                "url": mcp_url,
                "enabled": True,
                "headers": {"Authorization": want_auth},
                "oauth": False,
            }
            changed = True
    if changed:
        _dump_json(oc_path, oc_data)
        report.append(f"project identity: {oc_path} mcp.agentbus (bound key for {name})")
    else:
        report.append(f"project identity: {oc_path} (already {name})")

    # 5. Plugin reference in the project's plugin array. The project file is
    #    merged over the global one, so a project-level plugin array ADDS to the
    #    global plugins — it does not replace them. Dedupe so re-runs converge.
    plugins = oc_data.setdefault("plugin", [])
    if not isinstance(plugins, list):
        plugins = [plugins] if isinstance(plugins, str) else []
        oc_data["plugin"] = plugins
    if OPENCODE_PLUGIN_NPM not in plugins:
        plugins.append(OPENCODE_PLUGIN_NPM)
        _dump_json(oc_path, oc_data)
        report.append(
            f"plugin: {oc_path} -> {OPENCODE_PLUGIN_NPM} "
            "(installed via `opencode plugin <name>` the first time)"
        )
    else:
        report.append(f"plugin: {OPENCODE_PLUGIN_NPM} (already referenced)")

    gitignore = Path.cwd() / ".gitignore"
    if (Path.cwd() / ".git").exists():
        lines = gitignore.read_text().splitlines() if gitignore.exists() else []
        if "opencode.json" not in lines and "opencode.local.json" not in lines:
            # The project identity carries a SECRET (the bound key bearer), so
            # it must never be committed. opencode reads opencode.json; a
            # project that already tracks one for non-secret settings should
            # keep it — but this one is ours, and it holds a key.
            with gitignore.open("a", encoding="utf-8") as fh:
                fh.write("opencode.json\n")
            report.append("gitignore: added opencode.json (holds the bound key)")

    # 6. The plugin binary/client. opencode plugins run in Bun and shell out to
    #    the shared `agentbus`/`agentbus-hook` client, so those must be on PATH
    #    (the same install `signin` and the claude harness rely on). Nothing to
    #    write here — the plugin is the package above; the client is the pip
    #    package already installed.
    report.append(
        "wake: provided by the opencode PLUGIN (SSE stream + "
        "promptAsync) — opencode has no Stop hook, so no re-waker"
    )

    # 7. The skill, served canonically — same managed-artifact rule as claude.
    skill_path = Path.home() / ".claude" / "skills" / "agentbus" / "SKILL.md"
    try:
        import httpx

        resp = httpx.get(
            f"{(base_url or 'https://agentbus.rodmena.co.uk').rstrip('/')}/skills/opencode.md",
            timeout=15,
        )
        if resp.status_code == 200 and len(resp.text) > 500:
            if not skill_path.exists() or skill_path.read_text() != resp.text:
                skill_path.parent.mkdir(parents=True, exist_ok=True)
                if skill_path.exists():
                    bak = skill_path.with_suffix(".md.bak")
                    bak.write_text(skill_path.read_text())
                    report.append(f"skill: previous copy saved to {bak}")
                skill_path.write_text(resp.text)
                report.append(f"skill: {skill_path} (installed — MANAGED artifact)")
            else:
                report.append(f"skill: {skill_path} (current)")
        else:
            report.append(f"skill: NOT installed (served {resp.status_code})")
    except Exception as exc:
        report.append(f"skill: NOT installed ({exc})")

    # 8. #64 credential-scope warning — same finding as claude: an inherited
    #    send-or-above fallback is exactly what this project's own identity is
    #    meant to stop relying on.
    try:
        findings = [ln for ln in doctor_credential_scope(base_url=base_url) if "FINDING" in ln]
        if findings:
            _say("WARNING — a send-or-above credential is reachable by inheritance:")
            for ln in findings:
                _say(f"  {ln}")
            _say(
                "  A fallback slot wants a READ-scope key, nothing above. Replace it "
                "with a read-scope key from the dashboard, then re-run `agentbus doctor`."
            )
            report.append("credential scope: WARNING (inherited send-or-above credential found)")
        else:
            report.append("credential scope: ok (no inherited send-or-above credential)")
    except Exception as exc:
        report.append(f"credential scope: not checked ({exc})")

    _say("agentbus setup opencode — everything wired, nothing foreign touched:")
    for line in report:
        _say(f"  {line}")
    _say("")
    _say(
        "Next: `opencode plugin @rodmena/agentbus-opencode` once (the "
        "package), then restart the opencode session."
    )
    return 0


def cmd_teardown(args: argparse.Namespace) -> int:
    """Remove ALL AgentBus wiring from this project, in one command (#118).

    Opting out used to require knowing three paths — `.agentbus/`, the
    settings.local.json identity, and the key file — and every partial deletion
    left a half-wired state whose symptoms (a monitor resolving a keyless
    identity, a guard failing on a dead credential) read as AgentBus taking the
    session hostage. Three separate incidents in one day, each a different
    missed file. Leaving must be as easy as joining: one command, everything,
    and it says exactly what it removed so a restarted session's silence is
    expected rather than suspicious.

    Local-only, deliberately: the server-side agent is NOT retired here. The
    row keeps the inbox and history for a re-wire (`agentbus setup` restores
    the same derived identity), and retiring is a separate, explicit act
    (`agentbus retire`) because it affects peers who hold the address.
    """
    removed: list[str] = []
    root = _git_root_or_none() or Path.cwd()

    agent: str | None = None
    agentbus_dir = root / ".agentbus"
    agent_file = agentbus_dir / "agent"
    if agent_file.is_file():
        with contextlib.suppress(OSError):
            agent = agent_file.read_text().strip() or None

    local_path = root / ".claude" / "settings.local.json"
    local = _load_json(local_path)
    if agent is None:
        agent = (local.get("env") or {}).get("AGENTBUS_AGENT")

    if agentbus_dir.is_dir():
        import shutil as _shutil

        _shutil.rmtree(agentbus_dir, ignore_errors=True)
        removed.append(f"{agentbus_dir}/")
    if (local.get("env") or {}).get("AGENTBUS_AGENT"):
        # Surgical: drop OUR key, keep every foreign setting in the file.
        del local["env"]["AGENTBUS_AGENT"]
        if not local["env"]:
            del local["env"]
        if local:
            _dump_json(local_path, local)
            removed.append(f"{local_path} (AGENTBUS_AGENT entry only)")
        else:
            with contextlib.suppress(OSError):
                local_path.unlink()
            removed.append(str(local_path))

    if getattr(args, "purge_key", False) and agent:
        for suffix in (".env", ".env.dead"):
            kf = _keys_dir() / f"{agent}{suffix}"
            if kf.is_file():
                with contextlib.suppress(OSError):
                    kf.unlink()
                removed.append(str(kf))

    if getattr(args, "machine", False):
        import shutil as _shutil

        cfg = _config_dir()
        if cfg.is_dir():
            _shutil.rmtree(cfg, ignore_errors=True)
            removed.append(f"{cfg}/ (machine-level: operator credential, device-id, all keys)")

    if not removed:
        _say("nothing to remove — this project carries no AgentBus wiring.")
        return 0
    _say("AgentBus wiring removed:")
    for item in removed:
        _say(f"  - {item}")
    _say("Restart the session; it will be silent — no monitor, no hooks, no bus.")
    if agent and not getattr(args, "machine", False):
        _say(f"(server-side agent '{agent}' is untouched; `agentbus setup` re-wires it,")
        _say(" `agentbus retire` stands it down for peers.)")
    return 0


def cmd_setup(args: argparse.Namespace) -> int:
    harness = args.harness
    if harness == "claude":
        return _setup_claude(args)
    if harness == "opencode":
        return _setup_opencode(args)

    # SPECS/0021: an unimplemented harness SHALL say so and name the ticket,
    # NEVER half-wire. The blocker is named per harness, because a refusal that
    # says only "not implemented" reads as a scheduling gap when it is a
    # harness-contract gap — and wiring half of the contract would be worse than
    # refusing, since it would present an AgentBus session that cannot be woken.
    if harness == "codex":
        _say("agentbus setup codex: refused, and not merely 'not implemented yet'.")
        _say("  codex's hook contract supports the GATE (PreToolUse) and the passive")
        _say("  hooks, but has no wake mechanism: the hook docs state that nothing")
        _say("  reactivates an idle session and SessionEnd is advisory-only. A")
        _say("  wired-but-undeaf agent is worse than an unwired one, because it")
        _say("  presents as reachable while nothing can ever start a turn for it.")
        _say("  There is also no per-project codex settings file for a declared")
        _say("  identity — config is global-only, which re-opens the #82/#83 gate")
        _say("  asymmetry. Tracked in #31. Nothing was changed.")
        return 1
    if harness == "agy":
        _say("agentbus setup agy: refused, and not merely 'not implemented yet'.")
        _say("  agy's CLI exposes no hook contract, no MCP server support, and no")
        _say("  wake path (no daemon, stream, or socket) — there is nothing setup")
        _say("  could wire that would give a session an inbox it can act on.")
        _say("  Tracked in #31. Nothing was changed.")
        return 1
    _say(f"unknown harness '{harness}' (choose from claude, opencode, codex, agy).")
    return 1


# ---------------------------------------------------------------- doctor --wake


def _monitor_pids(agent: str | None = None, session: str | None = None) -> list[str]:
    """PIDs of the plugin monitor FOR THIS AGENT, or [].

    Matching merely on the script name was E2, and it was a check that could
    not go red: on a multi-agent host every agent saw the same pid, so an agent
    whose own monitor had died still reported green as long as ANY other
    agent's monitor was alive. The streamer child carries `--agent <name>` and
    `--state monitor-<name>.json`, so scope to those.
    """
    try:
        # `-axwwo`, NOT `-eo` (#117). On FreeBSD, `ps -eo` without a controlling
        # terminal lists almost nothing (gettys only — 21 lines on a box running
        # a live monitor) and BSD ps truncates args without `-ww`, so doctor
        # reported "monitor is NOT running" while three matching processes ran.
        # A detection that cannot see the thing it detects is the vacuous-check
        # class again, this time varying by OS. `-axwwo pid,args` is accepted by
        # procps (Linux) and BSD ps alike and shows every process, full args —
        # verified on both before this change.
        out = subprocess.run(
            ["ps", "-axwwo", "pid,args"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for line in out.splitlines():
        if "grep" in line:
            continue
        if agent:
            # ONLY the monitor's own state file. `--agent <name>` was also
            # accepted here, and that was a false green of the same class as E2
            # one layer further out: a SUPERVISED WATCHER carries `--agent
            # <name>` too, so on a host running both, doctor reported "plugin
            # MONITOR, running" while pointing at the watcher's pid. The two do
            # not do the same job — a watcher only CAPTURES to a file and can
            # never start a turn — so if the monitor died the session was
            # genuinely deaf and doctor still said green. The check could not go
            # red on the one failure it exists to catch. `monitor-<agent>.json`
            # is the name the plugin picked precisely so the two would not
            # collide, so it discriminates exactly. Reported by david, who runs
            # both; that combination gets more common, not less, while the docs
            # still recommend a watcher for wake_channel.
            # TWO QUESTIONS, TWO SCOPES. `doctor` asks "is a monitor running
            # for me at all", which is per-AGENT. `session-end` asks "is THIS
            # session's monitor running", which is per-SESSION — and answering
            # the first when you meant the second is how a headless `claude -p`
            # came to reap an interactive session's monitor. Identity is
            # device+repo+path, so agent scope cannot separate two sessions on
            # one checkout.
            want = f"monitor-{agent}-{session}.json" if session else f"monitor-{agent}"
            if want in line:
                pids.append(line.split(None, 1)[0])
        elif "agentbus-monitor.sh" in line:
            pids.append(line.split(None, 1)[0])
    return pids


def _installed_version() -> str | None:
    """What is installed on disk — deliberately a different question from what a
    running process loaded, which is the whole point of the comparison below."""
    try:
        from . import __version__

        return str(__version__)
    except Exception:
        return None


def _running_watcher_version(agent: str) -> str | None:
    """The client version the LIVE watcher imported, or None if it cannot be told.

    Read from the watcher's own state file, which the process rewrites on every
    checkpoint — so it reflects what is RUNNING, not what is installed. Returns
    None for a watcher old enough not to stamp it, and None is not a pass: the
    caller must say "cannot confirm" rather than assume a match.
    """
    import glob

    newest, newest_mtime = None, -1.0
    for path in glob.glob(str(identity_config_dir() / f"monitor-{agent}-*.json")) + glob.glob(
        str(identity_config_dir() / f"watch-*-{agent}.json")
    ):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime > newest_mtime:
            newest, newest_mtime = path, mtime
    if newest is None:
        return None
    try:
        return json.loads(Path(newest).read_text()).get("client_version") or None
    except (OSError, ValueError):
        return None


def _finish_wake_report(
    failures: list[str], plugin_wake: bool = False, agent: str | None = None
) -> int:
    if failures:
        _say("")
        for f in failures:
            _say(f"  FAIL: {f}")
        return 1
    _say("")
    if plugin_wake:
        # DO NOT CERTIFY A BUILD THAT WAS NOT CONFIRMED.
        #
        # This said PROVEN on the strength of a process EXISTING. bob upgraded the
        # client, did not restart, and got a green wake report from a watcher still
        # running the pre-upgrade code — the diagnostic certifying the exact
        # configuration the upgrade existed to fix. "A process is running" and "the
        # running process has the fix" are different claims and only the first was
        # ever checked.
        loaded = _running_watcher_version(agent) if agent else None
        installed = _installed_version()
        # A SOURCE CHECKOUT IS NOT EVIDENCE OF A STALE WATCHER.
        #
        # Running from a working tree reports 0.0.0+source, so comparing it to a
        # properly installed watcher would fail this check on every developer
        # machine — a false "your wake path is broken" is how a real one stops
        # being believed. Only two real, differing versions are grounds to refuse.
        comparable = (
            loaded
            and installed
            and "source" not in loaded
            and "source" not in installed
            and loaded != "unknown"
            and installed != "unknown"
        )
        if comparable and loaded != installed:
            _say(
                f"wake chain NOT PROVEN: the running watcher loaded client "
                f"{loaded}, but {installed} is installed."
            )
            _say("A process that started before the upgrade is still serving this")
            _say("session's wake path, so any fix in the newer client is NOT active")
            _say("here. RESTART THIS SESSION, then re-run this check.")
            return 1
        _say("wake chain PROVEN as far as this host can see: the plugin monitor is")
        _say("running and holds a live stream. Measured on two hosts, a peer's")
        _say("message woke an idle session in 13-19s with no human input.")
        if loaded:
            _say(f"The running watcher reports client {loaded}, matching what is installed.")
        else:
            _say("NOTE: this watcher does not report which client build it loaded")
            _say("(pre-0.4.40), so 'running the current code' is assumed, not shown.")
    return 0


def doctor_wake(args: argparse.Namespace) -> int:
    """Prove the wake chain, link by link, ending with the honest sentence.

    `doctor` without --wake proves auth/quota/SMTP — all of which were green on
    a host that was structurally incapable of answering anyone. This half
    tests the one failure that actually costs outages.
    """
    failures: list[str] = []
    # SAY WHICH SOURCE CHOSE THE AGENT. `setup` has always explained itself;
    # doctor printed only the name, so an operator who exported AGENTBUS_AGENT
    # and got a different agent back had no way to see that this project's
    # settings.local.json had legitimately outranked it. david read that as
    # "the environment variable is dead" and filed it as a defect — a
    # reasonable conclusion from output that states a decision and hides its
    # reason. The precedence is correct and deliberate (project identity beats
    # a shell that happens to have something exported, which was E1); what was
    # missing is that it says so.
    #
    # The old `or os.environ.get("AGENTBUS_AGENT")` was dead code AND
    # misleading: it implied the environment is a last-resort fallback when it
    # is precedence 2 inside _resolve_agent_name.
    provenance: list[str] = []
    name = _resolve_agent_name(explain=provenance)
    if not name:
        _say(
            "wake: cannot tell which agent this project is (no AGENTBUS_AGENT, no setup). Run `agentbus setup claude` first."
        )
        return 1
    _say(f"wake: agent {name}")
    for line in provenance:
        _say(f"  ({line})")

    key = _agent_key(name)
    if key:
        _say(f"  [ok] credential: {_keys_dir()}/{name}.env")
    else:
        failures.append(f"no readable key file for {name}")

    settings = _load_json(Path.home() / ".claude" / "settings.json")
    hooks = settings.get("hooks", {})

    def _has(event: str, marker: str) -> bool:
        return any(
            marker in str(h.get("command", ""))
            for g in hooks.get(event, [])
            for h in g.get("hooks", [])
        )

    # THE PLUGIN CASE FIRST. When the plugin owns the wake, setup has correctly
    # REMOVED our hooks — so a doctor that only looks at settings.json reports
    # "PASSIVE ONLY" on the recommended configuration, and tells the user to run
    # the very command that produced it. That is a diagnostic looking for the
    # mechanism IT understands instead of the one that actually wakes the
    # session, on a host where the monitor had woken the agent 13 seconds
    # earlier. Consult the same predicate setup uses, then verify the monitor is
    # genuinely RUNNING rather than merely declared.
    if _plugin_provides_wake(settings):
        _say("  [ok] passive hooks: provided by the agentbus PLUGIN")
        running = _monitor_pids(name)
        if running:
            _say(
                f"  [ok] active trigger: plugin MONITOR, running (pid {running[0]}) "
                "— a stream held for the whole session"
            )
        else:
            failures.append(
                "the agentbus plugin is enabled but its monitor is NOT running. "
                "Monitors start at SESSION START — restart this session. If it "
                "still does not appear, check `claude plugin list`."
            )
        return _finish_wake_report(failures, plugin_wake=True, agent=name)

    passive = _has("SessionStart", _MARKER_HOOK) and _has("UserPromptSubmit", _MARKER_HOOK)
    active = _has("Stop", _MARKER_REWAKE)
    _say(f"  [{'ok' if passive else '!!'}] passive hooks (SessionStart + UserPromptSubmit)")
    _say(f"  [{'ok' if active else '!!'}] active trigger (Stop re-waker)")
    rewake_path = _config_dir() / "stop-rewake.sh"
    executable = rewake_path.exists() and os.access(rewake_path, os.X_OK)
    _say(f"  [{'ok' if executable else '!!'}] {rewake_path} executable")

    # D9: a client upgrade leaves the SCRIPT on disk stale, so the ledger-
    # isolation override silently no-ops and the doctor poisons the very ledger
    # it claims to protect while printing green. Refuse to trust a script that
    # predates the override rather than run a check that cannot go red.
    script_current = False
    if rewake_path.exists():
        body = rewake_path.read_text()
        marker = "agentbus-rewake-version:"
        ver = 0
        if marker in body:
            with contextlib.suppress(ValueError):
                ver = int(body.split(marker, 1)[1].split("\n", 1)[0].strip())
        script_current = ver >= 2
        if not script_current:
            failures.append(
                f"stop-rewake.sh is STALE (version {ver or 'unstamped'} < 2); a "
                "client upgrade did not refresh it, so its ledger isolation is a "
                "no-op. Run `agentbus setup claude` to reinstall it before trusting "
                "this check."
            )

    # D7 (david): the Stop chain passing is NOT the same as being notifiable.
    # Report the platform's own wake_channel fact so a green re-waker and a
    # send-time "no_wake_channel" warning can never contradict unnoticed.
    if key:
        try:
            probe = AgentBus(api_key=key, base_url=args.base_url, agent=name)
            roster = probe.phonebook()
            me = next((a for a in roster if a.get("name") == name), None)
            wc = me.get("wake_channel") if me else None
            # #49: wake_channel is now 4-state (live | stale | webhook | none),
            # replacing the boolean. Truthy means "something attached"; stale is
            # an attached subscriber with no recent sign of life — still not
            # "none", but worth saying so.
            if wc in (True, "live", "webhook"):
                _say(
                    "  [ok] wake_channel: a live subscriber/webhook is attached "
                    "(peers can be notified)"
                )
            elif wc == "stale":
                _say(
                    "  [--] wake_channel: STALE — a subscriber is attached but "
                    "nothing has moved in a while. It may be an orphaned stream. "
                    "The Stop re-waker still answers at turn boundaries, but "
                    "peers' send responses may warn 'no_wake_channel'."
                )
            elif wc in (False, "none", None):
                _say(
                    "  [--] wake_channel: NONE attached. The Stop re-waker makes "
                    "THIS session answer at turn boundaries, but peers' send "
                    "responses will warn 'no_wake_channel'. For always-attached "
                    "reachability run a supervised watcher: agentbus service."
                )
        except AgentBusError:
            pass

    if key and active and executable and script_current:
        # The live half: a self-addressed probe must surface through the
        # re-waker exactly once. A check that cannot go green cannot go red —
        # and a diagnostic must never CONSUME evidence: the first version of
        # this marked every listed delivery read, which silently ate three of
        # a peer's real messages (including a key-rotation confirmation) the
        # first time it ran on a host with a backlog. Now it touches exactly
        # one delivery — its own probe — runs the re-waker against an
        # ISOLATED seen-ledger so the production Stop hook still wakes for
        # everything else, and reports the foreign unread it deliberately
        # left alone.
        import tempfile

        prod_ledger = _config_dir() / f"rewake-seen-{name}.txt"
        prod_before = prod_ledger.read_text() if prod_ledger.exists() else ""

        bus = AgentBus(api_key=key, base_url=args.base_url, agent=name)
        try:
            sent = bus.send(
                name, subject="doctor --wake probe", text="Wake-chain probe; read and forget."
            )
            probe_delivery = None
            bystanders = 0
            for delivery in bus.inbox(limit=200, unread=True):
                if delivery.message_id == sent["id"]:
                    probe_delivery = delivery.delivery_id
                else:
                    bystanders += 1
            if probe_delivery is None:
                failures.append("probe message did not appear in the unread list")

            with tempfile.NamedTemporaryFile(prefix="rewake-doctor-") as ledger:
                # WINDOW=0 -> one deterministic pass, not a 10-minute hold; the
                # isolated ledger keeps the production seen-list untouched.
                env = dict(
                    os.environ,
                    AGENTBUS_AGENT=name,
                    AGENTBUS_REWAKE_STATE=ledger.name,
                    AGENTBUS_REWAKE_WINDOW="0",
                )
                run1 = subprocess.run(
                    [str(rewake_path)],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                if run1.returncode == 2 and probe_delivery and probe_delivery in run1.stdout:
                    _say("  [ok] probe surfaced by the re-waker (exit 2)")
                else:
                    failures.append(f"re-waker did not surface the probe (exit {run1.returncode})")
                if probe_delivery:
                    bus.read(probe_delivery)
                run2 = subprocess.run(
                    [str(rewake_path)],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            if run2.returncode == 0 and not run2.stdout:
                _say("  [ok] re-waker silent on second run (dedupe holds)")
            else:
                failures.append("re-waker fired twice for one probe — dedupe broken")
            # D9 verification: prove the override actually took, by confirming
            # the PRODUCTION ledger did not move. This is the by-hand check that
            # caught the stale-script no-op; now it runs every time.
            prod_after = prod_ledger.read_text() if prod_ledger.exists() else ""
            if prod_after == prod_before:
                _say("  [ok] production wake-ledger untouched (isolation verified)")
            else:
                failures.append(
                    "doctor POISONED the production wake-ledger — the "
                    "isolation override did not take (stale script?)"
                )
            if bystanders:
                _say(
                    f"  [ok] {bystanders} other unread message(s) UNTOUCHED — "
                    "read them: agentbus inbox --unread"
                )
        except (AgentBusError, OSError, subprocess.SubprocessError) as exc:
            failures.append(f"probe cycle failed: {exc}")

    if not active or not executable or not script_current:
        _say("")
        if not script_current and active:
            _say("STALE re-waker — reinstall with `agentbus setup claude`, then re-run.")
        else:
            _say("PASSIVE ONLY — this agent answers only when a human prompts it.")
            _say("Run `agentbus setup claude` to wire the active trigger.")
        return 1
    if failures:
        _say("")
        for f in failures:
            _say(f"  FAIL: {f}")
        return 1
    _say("")
    _say("wake chain PROVEN up to the harness boundary. Remaining assumption,")
    _say("stated rather than rounded up: your harness invokes Stop hooks with")
    _say("asyncRewake — exit 2 then pulls the session back into a turn.")
    return 0


# ------------------------------------------------------------------ identity
#
# DEPRECATED 2026-08-10 (operator directive): the sibling machinery was the
# wrong answer. Identity is env-var-driven — `AGENTBUS_AGENT` in the project's
# .env, or exported for one command — and a customer who wants two agents on
# one checkout should use a git worktree or a clone, which gives each its own
# directory and therefore its own identity for free. The `sibling add/list/as`
# verbs are retained only to say so, never to act.


def _mint_bound_key(name: str, operator: str | None, base_url: str | None) -> str | None:
    """A `send` key bound to `name` alone, or None with the reason printed."""
    if operator is None:
        _say(f"'{name}' has no key file and there is no operator credential to mint one.")
        _say("  Sign in with the workspace key once: agentbus signin <key>")
        return None
    try:
        minted = AgentBus(api_key=operator, base_url=base_url).mint_key(
            scope="send", agents=[name], label=f"sibling-{name}"
        )
    except AgentBusError as exc:
        _say(f"could not mint a bound key for '{name}': {exc}")
        return None
    secret = minted.get("key") or minted.get("api_key")
    if not secret:
        _say("mint succeeded but no secret in the response; refusing to continue.")
        return None
    return str(secret)


def cmd_sibling(args: argparse.Namespace) -> int:
    # DEPRECATED (operator directive, 2026-08-10): the sibling machinery is the
    # wrong answer. Identity is env-var-driven — AGENTBUS_AGENT in the project's
    # .env, or exported for one command — and a customer who wants two agents on
    # one checkout should use a git worktree or a clone, which gives each its own
    # directory and therefore its own identity for free. This verb is kept so old
    # muscle memory does not silently become a different command, but it says no.
    _say(
        "DEPRECATED: `agentbus sibling` is being retired. Give the agent its "
        "identity with an env var instead:"
    )
    _say("  AGENTBUS_AGENT=myrole agentbus setup claude --role myrole")
    _say("  # or put `AGENTBUS_AGENT=myrole` in the project's .env")
    _say(
        "For two agents on one checkout, use a git worktree or a separate "
        "clone — each directory has its own identity."
    )
    return 2


def cmd_as(args: argparse.Namespace) -> int:
    """Run a command AS a sibling, so every descendant inherits one identity.

    DEPRECATED (operator directive, 2026-08-10) with `sibling`: identity is
    env-var-driven. `AGENTBUS_AGENT=role agentbus ...` is the supported way to
    act as a different agent; a worktree or clone gives that role its own
    directory identity. Kept so old invocations get guidance, not a silent
    change of meaning.
    """
    if not args.command:
        _say("nothing to run. usage: agentbus as <role> -- <command> [args...]")
        return 2
    # DEPRECATED: do not execute. The env-var identity is the supported way —
    # `AGENTBUS_AGENT=role agentbus ...` — and a worktree/clone gives that role
    # its own directory identity. Running the command here would keep the old
    # machinery alive; guidance is the point of keeping this verb at all.
    _say(
        "DEPRECATED: `agentbus as` is being retired. Act as a different agent "
        "with an env var instead:"
    )
    _say(f"  AGENTBUS_AGENT={args.role} agentbus {args.command[0]} {' '.join(args.command[1:])}")
    _say("For a separate identity on this checkout, use a git worktree or clone.")
    return 2
