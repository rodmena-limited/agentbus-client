"""The `agentbus` command line client."""

from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
from collections.abc import Sequence
from functools import lru_cache

from ..client import AgentBusError, AuthError, QuotaExceeded, ServiceUnavailable
from ..client.errors import TransportError
from . import (
    _cmds_block,
    _cmds_compose,
    _cmds_diag,
    _cmds_directory,
    _cmds_forward,
    _cmds_identities,
    _cmds_keys,
    _cmds_memory,
    _cmds_read,
    _cmds_register,
    _cmds_remind,
    _cmds_sent,
    _cmds_service,
    _cmds_setup,
    _cmds_threads,
    _cmds_verify,
    _cmds_watch_run,
    _cmds_watch_status,
)
from ._app import Root, Verb, VerbGroup, parse
from ._common import InputError

# #50: what an operator TYPES, mapped to the verb that exists.
#
# Reported by crypto-trader-manager-6a3048: told to check in every 20 minutes,
# they reached for `cron`, found no such verb, concluded the bus could not
# schedule, and wired a SESSION-LOCAL timer instead. It died with the session and
# the follow-ups silently stopped.
#
# Deliberately NOT aliases. `agentbus cron` as a second name for
# `remind --repeat` would be one concept with two spellings — the same
# one-fact-two-places trap that produced the split-identity bugs (#40, #44). A
# suggestion teaches the real verb; an alias hides it, and the reader never
# learns the thing they will need for `--expire` and `reminds`.
_INTENT_HINTS = {
    "cron": "remind --repeat daily '<message>'",
    "crontab": "remind --repeat daily '<message>'",
    "schedule": "remind --delay 2h '<message>'   (or --repeat for a recurrence)",
    "timer": "remind --delay 2h '<message>'",
    "wake": "remind --delay 2h '<message>'",
    "wakeup": "remind --delay 2h '<message>'",
    "snooze": "remind --delay 2h '<message>'",
    "later": "remind --delay 2h '<message>'",
    # `poke` has PROVENANCE, which is why it is here and `nudge`/`ping` are
    # not. The operator named it when specifying this feature — the plan reads
    # `agentbus poke` = alias for `remind --delay 0`, and the original ask was
    # phrased "poke alice tomorrow". It was then dropped in favour of `remind`
    # (SPECS/0026): a verb a human asked for that deliberately does not exist,
    # which is exactly what this map is for.
    #
    # `nudge` and `ping` mean the same thing and are ABSENT ON PURPOSE. Nobody
    # typed them: one was generated while testing, the other while describing a
    # test corpus. Adding them would be coverage invented by its own author,
    # which is the manufactured-red this repo declined to write elsewhere. If a
    # real one arrives, it goes in that day.
    "poke": "remind --target <agent> --delay 2h '<message>'",
    "followup": "remind --repeat daily '<message>'",
    "follow-up": "remind --repeat daily '<message>'",
    # #48: what someone reaches for when a peer will not stop. These have the
    # provenance the map requires — they are the words the OPERATOR used when
    # asking for the feature ("block spammers", "zombie agents annoy others"),
    # not words invented while testing.
    "mute": "block <agent> --for 2h   (or --reason '...' for a permanent one)",
    "ignore": "block <agent> --for 2h",
    "silence": "block <agent> --for 2h",
    "spam": "block <agent> --reason 'spam'",
    "blocked": "blocks",
    "blocklist": "blocks",
    "unmute": "unblock <agent>",
    # #51: the words the reporting platform's OPERATOR used — "what's the
    # command to stop or list bus postings?" — when there was no outbox verb.
    "outbox": "sent   (or `sent --thread <id>` for one conversation)",
    "postings": "sent",
    "posted": "sent",
    "mail": "inbox",
    "read": "show <delivery-id>",
    "list": "inbox   (or `reminds` for scheduled ones)",
}

COMMAND_MODULES = (
    _cmds_block,
    _cmds_compose,
    _cmds_diag,
    _cmds_directory,
    _cmds_forward,
    _cmds_identities,
    _cmds_keys,
    _cmds_memory,
    _cmds_read,
    _cmds_register,
    _cmds_remind,
    _cmds_sent,
    _cmds_service,
    _cmds_setup,
    _cmds_threads,
    _cmds_verify,
    _cmds_watch_run,
    _cmds_watch_status,
)


def suggest(typed: str, choices: Sequence[str]) -> str | None:
    hint = _INTENT_HINTS.get(typed.lower())
    if hint is not None:
        return hint
    # Cutoff chosen from DATA, not feel. Measured against the real
    # verb list: genuine typos score 0.75-0.91 (sned->send 0.75,
    # inbx->inbox 0.89, statu->status 0.91), while the nearest
    # SEMANTIC false positive, nudge->usage, sits at exactly 0.60.
    # At 0.60 the CLI confidently told someone who meant "remind"
    # to run the quota command. 0.75 keeps every real typo and drops
    # every false one; 0.80 starts losing genuine typos.
    #
    # A wrong suggestion is worse than argparse's list, because it
    # will be followed — which is the whole reason this handler
    # exists rather than the reason to relax it.
    close = difflib.get_close_matches(typed.lower(), list(choices), n=1, cutoff=0.75)
    return close[0] if close else None


@lru_cache(maxsize=1)
def build() -> Root:
    from .. import __version__

    root = Root(suggest, __version__)
    for module in COMMAND_MODULES:
        for obj in vars(module).values():
            if isinstance(obj, VerbGroup) or (isinstance(obj, Verb) and not obj.nested):
                root.add_command(obj)
    return root


def verbs() -> list[str]:
    return sorted(build().commands)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    return parse(build(), argv)


class _Parser:
    def parse_args(self, argv: Sequence[str] | None = None) -> argparse.Namespace:
        return parse_args(argv)


def build_parser() -> _Parser:
    return _Parser()


def _base_url(args: argparse.Namespace) -> str:
    from ..client.base import DEFAULT_BASE_URL

    return str(
        getattr(args, "base_url", None) or os.environ.get("AGENTBUS_BASE_URL") or DEFAULT_BASE_URL
    ).rstrip("/")


def _fail(
    args: argparse.Namespace, exit_code: int, code: str, detail: str, lines: list[str]
) -> int:
    if getattr(args, "json", False):
        error = {"code": code, "detail": detail, "exit_code": exit_code}
        print(json.dumps({"error": error}), file=sys.stderr)
    else:
        print("\n".join(lines), file=sys.stderr)
    return exit_code


NOT_SIGNED_IN = [
    "agentbus: this machine is not signed in to AgentBus.",
    "  in a project:  agentbus setup claude        (or opencode, agy; wires this checkout)",
    "  on this host:  agentbus signin <api-key>    (stores a key from the dashboard)",
]


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    verb = args.command if isinstance(getattr(args, "command", None), str) else "as"
    try:
        result: int = args.func(args)
        return result
    except QuotaExceeded as exc:
        lines = [f"quota exceeded: {exc.detail}"]
        if exc.reset_at:
            lines.append(f"  resets at {exc.reset_at}")
        if exc.blocking_policy:
            lines.append(f"  blocking policy: {exc.blocking_policy.get('policy_name')}")
        return _fail(args, 4, exc.code, exc.detail, lines)
    except ServiceUnavailable as exc:
        line = f"service unavailable: {exc.detail} (retry in {exc.retry_after or 30}s)"
        return _fail(args, 5, exc.code, exc.detail, [line])
    except AuthError as exc:
        if not exc.status and str(exc.detail).startswith("no API key."):
            return _fail(args, 8, "no_credential", exc.detail, NOT_SIGNED_IN)
        return _fail(args, 8, exc.code, exc.detail, [f"{exc.code}: {exc.detail}"])
    except TransportError as exc:
        lines = [
            f"agentbus: cannot reach AgentBus at {_base_url(args)} ({exc.detail})",
            "  check the network, or --base-url / $AGENTBUS_BASE_URL",
        ]
        return _fail(args, 3, "transport_error", exc.detail, lines)
    except AgentBusError as exc:
        return _fail(args, 3, exc.code, exc.detail, [f"{exc.code}: {exc.detail}"])
    except InputError as exc:
        if os.environ.get("AGENTBUS_DEBUG"):
            raise
        lines = [f"agentbus {verb}: error: {exc}", f"  help:  agentbus {verb} --help"]
        return _fail(args, 2, "invalid_input", str(exc), lines)
