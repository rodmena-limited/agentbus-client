"""The `agentbus` command line client."""

from __future__ import annotations

import argparse
import sys

from ..client import AgentBusError, AuthError, QuotaExceeded, ServiceUnavailable
from . import (
    _compose,
    _diag,
    _directory,
    _forward,
    _identities,
    _keys,
    _read,
    _register,
    _remind,
    _service,
    _setup,
    _threads,
    _verify,
    _watch_run,
    _watch_status,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentbus", description="AgentBus — a real inbox for every agent"
    )
    from .. import __version__

    parser.add_argument("--version", action="version", version=f"agentbus {__version__}")
    parser.add_argument("--api-key", default=None, help="defaults to $AGENTBUS_API_KEY")
    parser.add_argument("--base-url", default=None, help="defaults to $AGENTBUS_BASE_URL")
    parser.add_argument("--agent", default=None, help="acting agent; defaults to $AGENTBUS_AGENT")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)


    # One module per command family; each wires its own subcommands.
    _register.add_commands(sub)
    _directory.add_commands(sub)
    _identities.add_commands(sub)
    _compose.add_commands(sub)
    _forward.add_commands(sub)
    _read.add_commands(sub)
    _remind.add_commands(sub)
    _threads.add_commands(sub)
    _keys.add_commands(sub)
    _verify.add_commands(sub)
    _watch_status.add_commands(sub)
    _watch_run.add_commands(sub)
    _service.add_commands(sub)
    _diag.add_commands(sub)
    _setup.add_commands(sub)
    return parser




def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # (parser handles --version via argparse's version action)
    try:
        result: int = args.func(args)
        return result
    except QuotaExceeded as exc:
        print(f"quota exceeded: {exc.detail}", file=sys.stderr)
        if exc.reset_at:
            print(f"  resets at {exc.reset_at}", file=sys.stderr)
        if exc.blocking_policy:
            print(f"  blocking policy: {exc.blocking_policy.get('policy_name')}", file=sys.stderr)
        return 4
    except ServiceUnavailable as exc:
        print(
            f"service unavailable: {exc.detail} (retry in {exc.retry_after or 30}s)",
            file=sys.stderr,
        )
        return 5
    except AuthError as exc:
        # A REJECTED CREDENTIAL GETS ITS OWN EXIT CODE (8), because the monitor
        # must treat it as TERMINAL — retrying a revoked key is hammering the
        # bus with a credential that will never work — while every other
        # AgentBusError (including TransportError: bus down, DNS, refused) is
        # transient and stays retryable on 3. The two were conflated on 3, and
        # the monitor's terminal branch silenced legitimate reconnect loops.
        print(f"{exc.code}: {exc.detail}", file=sys.stderr)
        return 8
    except AgentBusError as exc:
        print(f"{exc.code}: {exc.detail}", file=sys.stderr)
        return 3
