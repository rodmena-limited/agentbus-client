"""`agentbus identity` — who is this project, and which source said so.

Split out of `_register.py` (#46): adding the resolution reporting in #41 took
that file from 496 to 569 lines, past the 550 hard cap. The identity QUESTION and
the registration ACTION are separate concerns that happened to share a file, so
the cap pointed at a seam that was already there.
"""

from __future__ import annotations

import argparse

from ._common import _print


def cmd_identity(args: argparse.Namespace) -> int:
    """Print the derived session identity.

    Exists because an agent driving the bus over MCP cannot see this machine —
    the MCP server is remote. It runs this once and passes the values to
    bus_register.
    """
    from .. import identity

    env = identity.describe(getattr(args, "workdir", None))
    # #41: ANSWER THE QUESTION THE COMMAND IS NAMED AFTER. This used to print
    # only the INPUTS to derivation (device_id, workdir, session_key) and never
    # the resolved agent — so in a directory where two sources declare different
    # identities it reported neither, and the one command an operator reaches
    # for when confused about identity could not tell them what was winning.
    #
    # That is how #40's split identity survived 2026-08-17 -> 2026-08-22 across
    # 195 silent gate failures: nothing showed WHICH source answered. A peer
    # ended up reading the identity out of `whoami`'s 404 text, because the
    # error message named it and the diagnostic did not.
    resolved, sources, conflict = _identity_resolution()
    env = dict(env)
    env["resolved_agent"] = resolved
    env["identity_sources"] = sources
    if args.json:
        _print(env, True)
        return 0

    if resolved:
        print(f"{'agent:':<18} {resolved}")
        if sources:
            print(f"{'declared by:':<18} {sources[0]}")
    else:
        print(f"{'agent:':<18} (none — this project has not declared an identity)")
    # A LOSING SOURCE IS THE WHOLE POINT. Two sources that agree are invisible
    # and harmless; two that disagree are a split identity, and until now the
    # only way to see one was to notice the symptom days later.
    for other in sources[1:]:
        print(f"{'  also declares:':<18} {other}  <- IGNORED (lower precedence)")
    if conflict:
        print(
            "  ^ sources DISAGREE. Precedence: $AGENTBUS_AGENT > .agentbus/agent"
            " > settings.local.json"
        )
    print()
    for key in (
        "device_id",
        "workdir",
        "repo_remote",
        "repo_fingerprint",
        "session_key",
        "ephemeral",
    ):
        print(f"{key + ':':<18} {env[key]}")
    print("\nPass device_id, workdir and repo_remote to bus_register(role=...).")
    print("workdir is hashed server-side and never stored or published raw.")
    return 0


def _identity_resolution() -> tuple[str | None, list[str], bool]:
    """(resolved name, every source that declares one winner-first, do they conflict).

    Reads the sources directly rather than only asking the resolver, because the
    resolver SHORT-CIRCUITS at the winner by design — and a source that is never
    consulted is exactly the one an operator needs to see when two disagree.
    """
    import json as _json
    import os as _os

    from ..onboarding import _identity as _ident

    sources: list[str] = []
    names: list[str] = []
    env_name = _os.environ.get("AGENTBUS_AGENT")
    if env_name:
        sources.append(f"$AGENTBUS_AGENT ({env_name})")
        names.append(str(env_name))
    worktree = _ident._agent_from_worktree()
    if worktree:
        sources.append(f".agentbus/agent ({worktree})")
        names.append(worktree)
    try:
        local = _ident._project_claude_dir() / "settings.local.json"
        name = None
        if local.is_file():
            name = (_json.loads(local.read_text()).get("env") or {}).get("AGENTBUS_AGENT")
        if name:
            sources.append(f"settings.local.json ({name})")
            names.append(str(name))
    except (OSError, ValueError):
        pass
    # CONFLICT IS A DISAGREEMENT ABOUT THE NAME, not a count of files. Two
    # sources that agree are normal — `setup` writes both from one value on
    # purpose — and warning on them would make the banner fire on the commonest
    # healthy layout, which is how a warning stops being read before it reaches
    # the operator who needed it.
    conflict = len(set(names)) > 1
    return _ident._resolve_agent_name(), sources, conflict
