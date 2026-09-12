"""The `agentbus` command line client."""

from __future__ import annotations

import argparse
import difflib
import sys
from collections.abc import Sequence
from functools import lru_cache

from ..client import AgentBusError, AuthError, QuotaExceeded, ServiceUnavailable
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


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
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
