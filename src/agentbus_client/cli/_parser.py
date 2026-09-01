"""The `agentbus` command line client."""

from __future__ import annotations

import argparse
import sys

from ..client import AgentBusError, AuthError, QuotaExceeded, ServiceUnavailable
from . import (
    _block,
    _compose,
    _diag,
    _directory,
    _forward,
    _identities,
    _keys,
    _memory,
    _read,
    _register,
    _remind,
    _sent,
    _service,
    _setup,
    _threads,
    _verify,
    _watch_run,
    _watch_status,
)

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


class _SuggestingParser(argparse.ArgumentParser):
    """An unknown verb should point at the right one, not print 52 choices.

    argparse's default lists every choice, which is a wall an agent skims and
    concludes from. That is how a real session decided self-scheduling did not
    exist while `remind` was sitting in the list it had just been shown.
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        import difflib
        import re as _re

        m = _re.search(r"invalid choice: '([^']+)'", message)
        if m:
            typed = m.group(1)
            hint = _INTENT_HINTS.get(typed.lower())
            if hint is None:
                choices = self._subparser_choices()
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
                close = difflib.get_close_matches(typed.lower(), choices, n=1, cutoff=0.75)
                hint = close[0] if close else None
            if hint:
                self.exit(
                    2,
                    f"agentbus: there is no `{typed}` command.\n\n"
                    f"  You probably want:  agentbus {hint}\n\n"
                    f"  `agentbus quickref` lists the common flows.\n",
                )
        super().error(message)

    def _subparser_choices(self) -> list[str]:
        for action in self._actions:
            if isinstance(action, argparse._SubParsersAction):
                return list(action.choices)
        return []


def build_parser() -> argparse.ArgumentParser:
    parser = _SuggestingParser(
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
    _memory.add_commands(sub)
    _remind.add_commands(sub)
    _threads.add_commands(sub)
    _sent.add_commands(sub)
    _keys.add_commands(sub)
    _verify.add_commands(sub)
    _watch_status.add_commands(sub)
    _watch_run.add_commands(sub)
    _service.add_commands(sub)
    _block.add_commands(sub)
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
