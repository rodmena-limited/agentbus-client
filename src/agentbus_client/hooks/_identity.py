#!/usr/bin/env python3
"""Claude Code hooks for AgentBus.

Two jobs, deliberately separate:

  session-start   surface anything already waiting when a session opens, so an
                  agent never begins work unaware that a peer is blocked on it
  notify          called by `agentbus watch --exec`, writes a wake file the
                  session picks up on its next turn

Why both: a hook only fires on session lifecycle events, so on its own it cannot
notice a message that arrives mid-session. `agentbus watch` runs outside the
turn and can. Neither is sufficient alone, which is the whole reason idle agents
were missing messages.

Install BOTH hooks (project or user settings.json) — session-start without
pending means mid-session arrivals surface only on the next restart:

    {
      "hooks": {
        "SessionStart": [{"hooks": [{"type": "command",
          "command": "agentbus-hook session-start"}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command",
          "command": "agentbus-hook pending"}]}]
      }
    }

Both need `AGENTBUS_API_KEY` and `AGENTBUS_AGENT` in the environment. Put them
in per-project env (a `.envrc`, or the project's own settings), NEVER inline in
the hook command: an inlined key outlives every rotation, and an inlined —
or guessed — agent name makes the hook act as someone who does not exist.

AGENTBUS_AGENT IS THE KILL SWITCH. These hooks are installed globally and run
in every project on the machine. A project that declares no identity — no
`AGENTBUS_AGENT`, no `.agentbus/agent` — gets NOTHING: no output, no network
call, no files touched, exit 0. Not a warning, not a suggestion to run setup.
Silence is the correct behaviour for a project that never asked for a bus.

A watcher is NOT part of this setup. Its one remaining job is real-time
`--exec` side effects (e.g. notify-send to a human):

    agentbus watch --agent <name> \\
      --exec 'agentbus-hook notify --subject {subject} --sender {sender} --delivery {delivery_id}'

Every failure path here is silent-and-zero. A hook that breaks a session because
the bus is unreachable is worse than one that says nothing.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
from pathlib import Path


def _resolve_agent() -> str | None:
    """The acting agent, or None — NEVER an invented one.

    This used to default to the literal string "unknown", so a hook invoked
    without AGENTBUS_AGENT in its environment silently used a DIFFERENT FILE,
    `wake-unknown.jsonl`, and exited 0. Writes went to one queue and reads came
    from another, and both halves reported success.

    A peer hit it for real: `agentbus-hook pending` returned empty while three
    messages sat in the wake file, and empty is indistinguishable from "nothing
    arrived". In his words — "I only caught it because empty was the answer I
    did not expect. If it had been the answer I expected, I would have filed
    'wake works, inbox was just quiet' and been wrong."

    Exit 0 always is still right: a hook must never break a session. But
    silent-and-zero PLUS an invented identity means a MISCONFIGURED wake channel
    and an IDLE one produce byte-identical output, and a check that cannot
    report failure cannot report success either. So: say so on stderr, touch
    nothing, and still exit 0.

    NO IDENTITY IS NOT AN ERROR. IT IS THE OFF SWITCH.

    That is the 2026-08-13 operator directive and it reverses what this function
    used to assume. These hooks are installed GLOBALLY, so they run in every
    project on the machine, including every project that never asked for a bus.
    The old code treated "no AGENTBUS_AGENT" as a misconfiguration and said so
    on stderr, advising `agentbus setup claude`. In a session that had
    deliberately not opted in, that advice was read by the assistant as a task,
    which wired the project and dumped 242 unread messages into an unrelated
    piece of work. A tool that recommends its own activation is not neutral, and
    an agent reading its own diagnostics cannot tell a notice from an
    instruction.

    So: no identity -> return None, print NOTHING, and the caller exits 0 having
    read and written nothing.

    RESOLUTION MATCHES THE MONITOR (agentbus-monitor.sh) EXACTLY, in the same
    order, because #90 was these two disagreeing: the operator's env export
    differed from the file, the hooks followed the file Claude Code injected,
    the monitor followed the env, and one session held two identities with each
    half looking correct on its own.

        1. $AGENTBUS_AGENT              — the operator's word for this session
        2. .agentbus/agent in the repo  — the worktree's own declaration
        3. .claude/settings.local.json  — legacy, Claude Code only

    Nothing else. The machine-global signin default is deliberately NOT
    consulted by either component any more; it attached unwired directories to
    whoever last signed in on the box.
    """
    agent = os.environ.get("AGENTBUS_AGENT")
    if agent:
        # ONE EXCEPTION, AND ONLY ONE: a LINKED GIT WORKTREE being handed the
        # MAIN worktree's identity by the harness rather than by the operator.
        # See `_worktree_identity_bleed`. Everything else about #90 stands —
        # the environment is still the operator's word for this session.
        own = _worktree_identity_bleed(agent)
        if own:
            # AND THE CREDENTIAL WITH IT (#131). Reversing the identity alone
            # left #129 half-delivered: hooks.json sources
            # keys/${AGENTBUS_AGENT}.env BEFORE calling us, so the environment
            # already holds the MAIN worktree's agent-BOUND key by the time we
            # decide we are somebody else. The session then resolved the right
            # agent and could not authenticate as it — "this key may act only
            # as <main>" — trading a wrong-inbox read for no mail at all.
            #
            # Identity and credential were being resolved by two components in
            # two directions, which is #90's shape one layer down. Fixing it in
            # hooks.json would need the shell to redo the bleed detection; the
            # side that KNOWS the resolved identity is this one, so it owns the
            # credential too.
            _adopt_credential_for(own)
        return own or agent
    agent = _agent_from_worktree()
    if agent:
        return agent
    # Legacy: projects wired before .agentbus/agent existed. Claude Code injects
    # this file's `env` block for HOOKS but not for MONITORS, which is why the
    # file is read directly rather than trusted to arrive in the environment.
    return _agent_from_project_settings()


def _adopt_credential_for(agent: str) -> None:
    """Point os.environ['AGENTBUS_API_KEY'] at `agent`'s own stored key (#131).

    SIDE EFFECT: MUTATES os.environ. Named `_adopt_credential_for` and NOT
    `_get_credential_for` because the mutation is the whole point — the
    pre-tool-use gate below (~line 1676) reads AGENTBUS_API_KEY from os.environ,
    and so do any subprocesses this hook spawns. If you are grep-ing for what
    changes AGENTBUS_API_KEY at runtime: THIS IS IT (#234 SEV-3). Callers that
    need a pure return value should read the file themselves; see
    client._key_from_disk.

    Called only when the identity was reversed out of the environment, i.e. we
    have already established that whatever key the environment holds was sourced
    for a DIFFERENT agent. Agent-bound keys are the norm, so leaving it in place
    guarantees a 'this key may act only as X' failure.

    SILENT AND BEST-EFFORT. No key file for this agent is a real state — a
    worktree wired but never signed in — and the caller already reports an
    unreachable bus in a way that does not pretend the inbox was empty. Making
    noise here would put it in front of a reader who cannot act on it mid-hook.
    """
    with contextlib.suppress(OSError, ValueError):
        from .. import sealing
        from ..identity import config_dir

        # REG-8b (round-3.5 re-audit): sanitize the filename. This is THE most
        # dangerous of the round-3.5 sites because (a) `agent` traces back
        # through _worktree_identity_bleed → _agent_from_worktree →
        # _read_declared_agent, whose FIRST source is `.agentbus/agent` in the
        # repo — the exact attacker-controllable file the REG-8 threat model
        # names — and (b) this function is documented as SILENT AND
        # BEST-EFFORT and MUTATES os.environ["AGENTBUS_API_KEY"] mid-hook. A
        # hostile checkout with .agentbus/agent containing "../operator" used
        # to swap a Claude Code hook session onto the operator credential
        # with NO visible failure. bound_env_filename closes that side door.
        f = config_dir() / "keys" / sealing.bound_env_filename(agent)
        if not f.exists():
            return
        for raw in f.read_text().splitlines():
            entry = raw.strip()
            entry = entry.removeprefix("export ")
            key, _, value = entry.partition("=")
            # ONLY the key. The file also exports AGENTBUS_AGENT, and writing
            # that back would re-assert the identity we just corrected.
            if key.strip() == "AGENTBUS_API_KEY" and value.strip():
                os.environ["AGENTBUS_API_KEY"] = value.strip().strip("'\"")
                return


def _read_declared_agent(root: Path) -> str:
    """The agent a checkout declares on disk, `.agentbus/agent` first."""
    with contextlib.suppress(OSError, ValueError):
        f = root / ".agentbus" / "agent"
        if f.exists():
            v = f.read_text().strip()
            if v:
                return v
    with contextlib.suppress(OSError, ValueError):
        s = root / ".claude" / "settings.local.json"
        if s.exists():
            return str((json.loads(s.read_text()).get("env") or {}).get("AGENTBUS_AGENT") or "")
    return ""


def _worktree_identity_bleed(env_agent: str) -> str | None:
    """This checkout's own agent, when $AGENTBUS_AGENT is the MAIN worktree's (#129).

    A Claude Code session opened in a LINKED GIT WORKTREE is handed the env block
    from the MAIN worktree's `.claude/settings.local.json`, not the worktree's own.
    Since the environment outranks the files by design (#90), the worktree's own
    declaration never gets a chance and the session silently acts as another LIVE
    agent.

    NOT THEORETICAL, and the damage is the invisible kind. A worktree session on
    this machine, acting as its parent, READ a message from the parent's inbox and
    marked it seen. Verified rather than accepted: delivery 01KZZ32B4G1... was
    absent from the parent's unread list having never been opened there, while the
    very next probe, which that session deliberately did not open, was still
    unread. A prediction that held in both directions.

    Compounding it, the SessionStart advice for a shared identity was "run it from
    a separate checkout or GIT WORKTREE, which gets its own settings.local.json".
    The worktree does get its own — it is simply not the one that wins — so
    following our own remedy reproduced the collision it was meant to cure.

    THE SIGNATURE IS NARROW ON PURPOSE. All of these must hold:

      * this checkout is a linked worktree (its git common dir is elsewhere)
      * it declares its own agent
      * that differs from $AGENTBUS_AGENT
      * $AGENTBUS_AGENT EQUALS the MAIN worktree's declared agent

    The last condition is what keeps #90 intact. An operator who deliberately
    exports an identity in a worktree exports something that is NOT the main
    worktree's agent, so their override still wins — this only reverses the case
    where the value provably came from the main worktree's file rather than from a
    person. Returns None (leave the environment alone) whenever git is unavailable
    or anything is ambiguous.
    """
    try:
        root = _repo_root()
        if root is None:
            return None
        # #44: disk first, for the same reason as _repo_root above — GIT_DIR
        # makes this subprocess answer confidently about a different repo.
        common_path = _common_dir_from_filesystem(root)
        if common_path is not None:
            return _bleed_verdict(root, common_path, env_agent)
        common = ""
        try:
            common = subprocess.run(
                ["git", "rev-parse", "--git-common-dir"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                cwd=str(root),
            ).stdout.strip()
        except Exception:
            common = ""
        if not common:
            # #42: git could not answer. The FILESYSTEM can: `.git` is a
            # directory in a main worktree and a FILE ("gitdir: <path>") in a
            # linked one. Falling through to `return None` here meant "could not
            # tell" was indistinguishable from "verified not a linked worktree",
            # and the injected identity won silently.
            common_path = _common_dir_from_filesystem(root)
            if common_path is None:
                return None
        else:
            common_path = Path(common)
            if not common_path.is_absolute():
                common_path = (root / common_path).resolve()
        return _bleed_verdict(root, common_path, env_agent)
    except Exception:
        return None


def _bleed_verdict(root: Path, common_path: Path, env_agent: str) -> str | None:
    """This checkout's own agent when the env holds the MAIN worktree's, else None.

    Extracted (#44) so the disk-derived and git-derived common dirs reach the
    SAME decision — two copies of this would be the one-fact-two-places trap in
    the code that decides who a message is from.

    In the MAIN worktree the common dir IS this checkout's .git; only a linked
    worktree points elsewhere. Every clause is required, and the LAST one is what
    keeps #90 intact: if the environment is not the main worktree's declared
    value then a person put it there, and they win. Short-circuiting also means
    the main worktree's files are only read when this really is a linked checkout.
    """
    own = _read_declared_agent(root)
    bleed = (
        common_path != (root / ".git").resolve()
        and bool(own)
        and own != env_agent
        and _read_declared_agent(common_path.parent) == env_agent
    )
    return own if bleed else None


def _agent_from_worktree() -> str | None:
    """Read the identity this CHECKOUT declares, from `.agentbus/agent`.

    Worktree-style identity, and the reason it exists rather than another entry
    in a harness config file: it is harness-neutral (Claude Code, opencode and
    codex all resolve it the same way), it sits at a path somebody can find
    without knowing which JSON file to open, and it is per-checkout, so two
    worktrees of one repo are two agents without any machine-global state
    deciding that for them.

    Resolved from the REPO ROOT, not the cwd, so a hook firing in a subdirectory
    finds the same identity as one firing at the top.
    """
    try:
        root = _repo_root()
        if root is None:
            return None
        declared = root / ".agentbus" / "agent"
        if not declared.is_file():
            return None
        name = declared.read_text().strip()
        return name or None
    except Exception:
        return None


# SEV-3 (#234): process-lifetime cache for the git-root lookup.
#
# Every hook invocation runs _resolve_agent, which fans out to _repo_root and
# git rev-parse — 100-500 ms on a slow FUSE mount or a large repo, paid before
# Claude even considers whether to allow the tool call. A 20-tool-call turn used
# to burn 20 x that in git alone. cwd is immutable within a hook process (no
# `os.chdir` happens in these paths), so a per-cwd cache is safe and correct.
_REPO_ROOT_CACHE: dict[str, Path | None] = {}


def _repo_root() -> Path | None:
    """The top of this git worktree, or None outside one. Cached per-cwd."""
    cwd = os.getcwd()
    if cwd in _REPO_ROOT_CACHE:
        return _REPO_ROOT_CACHE[cwd]
    # #44: THE FILESYSTEM IS THE PRIMARY SOURCE, not the fallback. #42 fixed
    # ABSENT git by falling back to disk; this is MISDIRECTED git — under
    # GIT_DIR/GIT_WORK_TREE the subprocess SUCCEEDS and answers about ANOTHER
    # repository, so the fallback was never reached and the resolver got a
    # confident wrong answer instead of no answer. Same silent signature:
    # wrong sender, no banner.
    #
    # Which checkout am I standing in is a property of THIS DIRECTORY, and
    # `.git` on disk cannot be redirected by an environment variable. Git is
    # kept only for the cases disk cannot answer.
    #
    # Not contrived: git EXPORTS these itself inside hooks, and CI runners and
    # wrapper tooling set them, so any agent invoked from a pre-commit hook is
    # in this environment.
    from_disk = _repo_root_from_filesystem()
    if from_disk is not None:
        _REPO_ROOT_CACHE[cwd] = from_disk
        return from_disk
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if out.returncode != 0:
            _REPO_ROOT_CACHE[cwd] = None
            return None
        top = out.stdout.strip()
        result = Path(top) if top else None
        _REPO_ROOT_CACHE[cwd] = result
        return result
    except Exception:
        # #42: DO NOT CACHE A SUBPROCESS FAILURE AS "not a repo". Fall back to
        # the filesystem, which can answer this without git running at all.
        result = _repo_root_from_filesystem()
        _REPO_ROOT_CACHE[cwd] = result
        return result


def _common_dir_from_filesystem(root: Path) -> Path | None:
    """`.git`'s common dir for `root`, read from disk instead of asked of git (#42).

    A main worktree has a `.git` DIRECTORY; a linked worktree has a `.git` FILE
    whose contents are `gitdir: <path to main>/.git/worktrees/<name>`. The common
    dir is that path with the trailing `worktrees/<name>` removed.
    """
    try:
        dot_git = root / ".git"
        if dot_git.is_dir():
            return dot_git.resolve()
        if dot_git.is_file():
            text = dot_git.read_text().strip()
            if not text.startswith("gitdir:"):
                return None
            target = Path(text.split(":", 1)[1].strip())
            if not target.is_absolute():
                target = (root / target).resolve()
            # <main>/.git/worktrees/<name> -> <main>/.git
            if target.parent.name == "worktrees":
                return target.parent.parent.resolve()
            return target.resolve()
    except OSError:
        return None
    return None


def _repo_root_from_filesystem() -> Path | None:
    """The worktree top found by walking up for `.git`, with NO subprocess.

    #42, and this is a correctness fix rather than an optimisation. `git
    rev-parse` returning non-zero was treated as "not a repository", so a git
    that was missing, slow past the 5s timeout, or transiently failing made
    `_worktree_identity_bleed` return None — which `_resolve_env_agent` reads as
    "no bleed, trust the environment". The injected MAIN worktree identity then
    won SILENTLY, with no banner, and the message went out under another agent's
    name.

    Measured in a real linked worktree: git available -> correct identity plus
    the banner; PATH stripped -> the main worktree's name, no banner, no error.
    Same directory, same environment, different sender, non-deterministic — one
    wrong From cost three agents an hour and produced four wrong conclusions,
    because the failure is invisible from both ends.

    `.git` is a DIRECTORY in a main worktree and a FILE ("gitdir: ...") in a
    linked one, so the distinction this module needs is on disk and never
    required a subprocess.
    """
    try:
        here = Path(os.getcwd()).resolve()
        for candidate in (here, *here.parents):
            if (candidate / ".git").exists():
                return candidate
    except OSError:
        return None
    return None


def _agent_from_project_settings() -> str | None:
    """Read AGENTBUS_AGENT from this project's .claude/settings.local.json.

    The SAME source the monitor reads, so the two components cannot disagree on
    which agent a project is. Returns None if the file is absent, unreadable, or
    carries no AGENTBUS_AGENT — never an invented name.

    RESOLVED FROM THE REPO ROOT, like `_agent_from_worktree` above. It used to
    read `Path.cwd()`, so a hook firing in a subdirectory found no identity
    while one firing at the top found it — the two resolvers in this same file
    disagreeing about which directory "the project" means. Since the kill switch
    (#95) a missed identity means AgentBus goes quiet rather than erroring, so
    the subdirectory case failed invisibly.
    """
    try:
        root = _repo_root()
        local = (root or Path.cwd()) / ".claude" / "settings.local.json"
        if not local.is_file():
            return None
        data = json.loads(local.read_text())
        name = (data.get("env") or {}).get("AGENTBUS_AGENT")
        return str(name) if name else None
    except Exception:
        return None
