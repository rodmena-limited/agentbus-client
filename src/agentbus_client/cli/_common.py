"""The `agentbus` command line client."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from ..client import AgentBus, AgentBusError


def _parse_duration(value: str) -> Any:
    """Parse a human duration like `24h`, `90m`, `2d`, or bare seconds into
    a timedelta.

    Accepts an integer (seconds), or a string of `<number><unit>` where unit
    is s/m/h/d (seconds/minutes/hours/days). Used for --ack-window. Raises a
    clear error on anything unparseable so a typo is caught locally, not by a
    confusing server 422.
    """
    import datetime as _dt
    import re as _re

    if value is None:
        raise ValueError("empty duration")
    v = str(value).strip().lower()
    m = _re.fullmatch(r"(\d+)(s|m|h|d)?", v)
    if not m:
        raise ValueError(
            f"invalid duration '{value}' — use seconds, or <number><unit> "
            "where unit is s/m/h/d (e.g. 90m, 2h, 3d, 3600)"
        )
    num = int(m.group(1))
    unit = m.group(2) or "s"
    seconds = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit] * num
    return _dt.timedelta(seconds=seconds)


def _read_body(value: str | None) -> str | None:
    """Support `@file` and `@-` bodies.

    A body that is *exactly* a path to a readable file is refused: sending the
    path string instead of the file contents is a mistake that silently destroys
    the message, and it has happened often enough to be worth blocking.
    """
    if value is None:
        return None
    if value == "@-":
        return sys.stdin.read()
    if value.startswith("@"):
        with open(value[1:], encoding="utf-8") as handle:
            return handle.read()
    if os.path.isfile(value) and len(value) < 4096 and "\n" not in value:
        raise SystemExit(
            f"refusing to send the literal path '{value}'. "
            f"Use '@{value}' to send the file's contents."
        )
    return value


def _git_remote() -> str | None:
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


def _print(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))
        return
    if isinstance(data, list):
        for item in data:
            print(item)
    else:
        print(data)


def _resolve_env_agent() -> str | None:
    """$AGENTBUS_AGENT, with the worktree misinjection REVERSED (#161).

    The harness injects the MAIN worktree's identity into a linked worktree's
    session env. Hooks (#130), the monitor (#129) and credential adoption
    (#131) all reverse that exact signature — but the bare CLI kept trusting
    the env as the operator's word, which is how one agent spent an hour
    sending as another (three messages under the bus owner's name, one a false
    defect report against the bus itself). Right doctrine for a TYPED export;
    wrong for an injected one — and the signature distinguishes them: the
    reversal fires ONLY when this is a linked worktree declaring its own agent
    while the env holds the MAIN worktree's declared agent. A deliberate
    export of anything else still wins (#90 intact).

    An `--agent` flag never reaches here at all: explicit always outranks.
    """
    env_agent = os.environ.get("AGENTBUS_AGENT")
    if not env_agent:
        return None
    from ..hooks import _identity as _hook_identity

    own = _hook_identity._worktree_identity_bleed(env_agent)
    if own:
        print(
            f"identity: env named '{env_agent}' (the MAIN worktree's, injected by "
            f"the harness); acting as '{own}', declared by THIS worktree (#161). "
            f"Pass --agent to override deliberately.",
            file=sys.stderr,
        )
        return own
    return env_agent


def _key_for_agent(agent: str) -> str | None:
    """The agent's own stored key, if one exists (keys/<agent>.env).

    REG-8b (round-3.5 re-audit): the agent name is sanitized through
    sealing.bound_env_filename before path join, so `../operator` cannot
    reach into <config>/operator.env from an attacker-controllable source
    ($AGENTBUS_AGENT, .agentbus/agent). This function is called from _bus()
    on the env-reversal path — exactly the code that consumes a hostile
    checkout's declared identity.
    """
    from .. import sealing

    with contextlib.suppress(OSError, ValueError):
        key_file = Path.home() / ".config" / "agentbus" / "keys" / sealing.bound_env_filename(agent)
        if not key_file.exists():
            return None
        for raw in key_file.read_text().splitlines():
            entry = raw.strip().removeprefix("export ")
            key, _, value = entry.partition("=")
            if key.strip() == "AGENTBUS_API_KEY" and value.strip():
                return value.strip().strip("'\"")
    return None


def _bus(args: argparse.Namespace) -> AgentBus:
    api_key = args.api_key or os.environ.get("AGENTBUS_API_KEY")
    explicit_agent = args.agent
    agent = explicit_agent or _resolve_env_agent()
    # #161's credential half: when the identity was reversed out of the env,
    # the env's key is the MAIN agent's BOUND key — using it guarantees "this
    # key may act only as <main>". The reversed agent's own key file wins over
    # the injected env key (an explicit --api-key still outranks everything).
    if (
        not args.api_key
        and agent
        and not explicit_agent
        and agent != os.environ.get("AGENTBUS_AGENT")
    ):
        own_key = _key_for_agent(agent)
        if own_key:
            api_key = own_key
    if not api_key:
        # signin stored it once; every verb honours that, not just setup.
        from ..onboarding import resolve_credentials

        stored_key, stored_agent = resolve_credentials(preferred_agent=agent)
        if stored_key:
            api_key = stored_key
            agent = agent or stored_agent
        else:
            from ..onboarding import explain_refusal

            why = explain_refusal(agent)
            if why:
                raise SystemExit(f"error: {why}")
    return AgentBus(api_key=api_key, base_url=args.base_url, agent=agent)


def _print_qr(payload: str) -> bool:
    """Render a QR to the terminal.

    LAZY IMPORT, and it is not a style choice. david measured `import segno` at
    49ms — more than this entire client's startup — and every `agentbus send`,
    `inbox` and hook invocation would have paid it for a feature almost nobody
    calls. It is imported here, inside the one command that needs it.
    """
    from ..qr import render

    return render(payload)


def _cfg_dir() -> Path:
    """The one config dir. See onboarding._config_dir for why this is central."""
    from ..identity import config_dir

    return config_dir()


def _as_message_id(bus: AgentBus, ident: str) -> str:
    """Accept a DELIVERY id where a MESSAGE id is required.

    Every surface an agent reads — the inbox listing, the session-start
    greeting, the re-waker output — prints `agentbus show <DELIVERY_ID>`, so
    pasting that into `reply` is the natural move and used to fail with a bare
    `not_found` that gave no hint the id was the wrong KIND. Resolution happens
    HERE rather than server-side: the reply route is guarded by a
    `message_participant` rule that runs in a dependency and rejects an
    unknown message id before any handler code, deliberately, so that the
    write path is no more of an existence oracle than the read path.
    """
    try:
        delivery = bus.read(ident)
    except AgentBusError:
        return ident  # not a delivery of ours; let the server speak
    resolved = delivery.get("message_id")
    return str(resolved) if resolved else ident


def _client_version() -> str:
    from .. import __version__

    return str(__version__)


def _harden_if_possible(path: Any) -> None:
    """0600 on a retired private key, exactly as on the live one.

    A superseded key opens the same mail the current one did; leaving it
    world-readable would make rotation a downgrade in secrecy, which is the
    opposite of the point.
    """
    import contextlib
    import os
    import stat

    with contextlib.suppress(OSError):
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def _accept_common_flags_after_subcommand(sub_parser: argparse.ArgumentParser) -> None:
    """Let --agent and --json appear on either side of the subcommand.

    A global-only flag that must precede the subcommand is a documented footgun
    that already cost the previous bus real time: `agentbus watch --agent x`
    reads perfectly and fails with 'unrecognized arguments'. SUPPRESS means an
    omitted flag leaves the global value alone instead of overwriting it with
    None.

    `--json` HAD THAT FIX AND DID NOT GET IT, which is worse than never having
    fixed either: the reasoning above was written down, applied to one flag, and
    the other kept failing in exactly the way the docstring describes. Every
    modern CLI accepts `cmd sub --json`; ours answered "unrecognized arguments"
    without saying where the flag belonged.

    And the footer of `phonebook` printed `agentbus phonebook --json` as the
    remedy for its own elision — a printed instruction that lands on a usage
    error, the same class as an advisory naming a deleted script.
    """
    sub_parser.add_argument(
        "--agent", default=argparse.SUPPRESS, help="acting agent (may also precede the subcommand)"
    )
    sub_parser.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="machine-readable output (may also precede the subcommand)",
    )
