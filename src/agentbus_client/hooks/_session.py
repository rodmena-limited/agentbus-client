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

import argparse
import contextlib
import json
import os
import time
from pathlib import Path
from typing import Any

from . import _identity
from ._identity import _resolve_agent
from ._state import EXIT_NOT_WIRED, _hook_warn, _wake_file, _warn_if_shadow_queue


def _is_self_send(sender_display: str) -> bool:
    """Did THIS agent send the message it is being woken about?

    Compares against AGENTBUS_AGENT, the same variable every other hook here
    treats as the identity, and returns False when it is unset rather than
    guessing — an unknown identity must not produce the STRONGER claim, which is
    the error this whole envelope exists to stop.

    `sender_display` for a bus message is "<name> via AgentBus". Matched on the
    name segment rather than the whole string so a change to the suffix does not
    silently turn this branch off; a branch that quietly stops firing is
    indistinguishable from one that was never right.
    """
    me = (os.environ.get("AGENTBUS_AGENT") or "").strip()
    if not me:
        return False
    name = (sender_display or "").split(" via ", 1)[0].strip()
    return bool(name) and name == me


def _identity_claim_path(agent: str) -> Path:
    from .. import sealing
    from ..identity import config_dir

    # REG-8c: same sanitization as _wake_file. The session-claim file is
    # written on session start and unlinked on session end; a traversal
    # payload was an arbitrary-path DELETE primitive at session end (via
    # `state.unlink(missing_ok=True)` further down this module) reachable
    # from a hostile `.agentbus/agent`.
    return config_dir() / f"session-claim-{sealing.agent_slug(agent)}.json"


def _warn_if_env_overrides_this_checkout(agent: str) -> None:
    """Say so when $AGENTBUS_AGENT silently outranks THIS checkout's own identity.

    #127. The env var outranking every file is deliberate (#90) — it is how an
    operator forces an identity for one invocation. What is not deliberate is
    doing it INVISIBLY to a checkout that is already correctly wired to a
    different agent.

    HOW IT PRESENTS, because the symptom looks nothing like the cause: a git
    worktree at ~/develop/agentbus-frontend was wired to
    `agentbus-frontend-5e9d03` in BOTH declaration sites, both gitignored,
    entirely correct. A session opened there with the parent checkout's
    AGENTBUS_AGENT inherited in its environment resolved as `agentbus-279ca7`
    instead, collided with the parent's live watcher, and reported "another
    session is already registered as agentbus-279ca7" — a true statement whose
    obvious remedy ("register this checkout as a separate agent") was WRONG. It
    already had one. Registering again would have minted a third identity and
    left the override in place to steal that one too.

    Verified rather than reasoned: `agentbus whoami` in that directory returns
    agentbus-279ca7 with the variable inherited and agentbus-frontend-5e9d03 with
    `env -u AGENTBUS_AGENT`. Same directory, same files, two identities.

    This is the #111 class — one checkout answering as another's agent — coming
    back through a different door. There it was a server-side drift heuristic;
    here it is an environment variable, and the file that should have decided is
    read, ignored, and never mentioned.

    NAME BOTH VALUES AND THE FIX. A warning that says only "env overrides" leaves
    the reader to go and find what it overrode, which is the step nobody takes.
    """
    env_agent = (os.environ.get("AGENTBUS_AGENT") or "").strip()
    if not env_agent or env_agent != agent:
        # Nothing set, or the env is not what actually won — no override to report.
        return
    root = _identity._repo_root() or Path.cwd()
    declared = ""
    with contextlib.suppress(OSError, ValueError):
        f = root / ".agentbus" / "agent"
        if f.exists():
            declared = f.read_text().strip()
    if not declared:
        with contextlib.suppress(OSError, ValueError):
            s = root / ".claude" / "settings.local.json"
            if s.exists():
                declared = str(
                    (json.loads(s.read_text()).get("env") or {}).get("AGENTBUS_AGENT") or ""
                ).strip()
    # Only a genuine DISAGREEMENT is worth interrupting for. An unwired checkout
    # has nothing to override, and agreement is the normal wired case.
    if not declared or declared == env_agent:
        return
    print(
        f"AgentBus: this checkout is wired to '{declared}', but $AGENTBUS_AGENT "
        f"in this session's environment says '{env_agent}' — and the environment "
        f"WINS. You are acting as '{env_agent}' here."
    )
    print(f"  checkout: {root}")
    print("  That is usually an inherited export leaking in from the shell or a")
    print("  parent session, not a choice. It makes this checkout share the other")
    print("  agent's inbox and read/ack state, so whichever session reads a")
    print("  message first hides it from the other.")
    print(
        f"  To use this checkout's own identity, start the session with "
        f"AGENTBUS_AGENT unset (env -u AGENTBUS_AGENT claude). Do NOT register a "
        f"new agent — '{declared}' already exists and the override would steal "
        f"that one too."
    )


def _identity_held_live(agent: str, session: str) -> bool:
    """Is `session` STILL holding `agent` — as a running process, not a record?

    The distinction the claim file cannot make on its own (#126). A live watcher
    scoped to this exact (agent, session) is proof of a holder; its absence is
    not proof of the negative, and the caller's comment says so rather than
    pretending otherwise.

    Fails CLOSED on any error — i.e. reports "not held", so we stay silent. An
    unreadable process table must not manufacture an alarm about a session we
    could not look for.
    """
    try:
        from ..onboarding import _monitor_pids

        return bool(_monitor_pids(agent, session=session))
    except Exception:
        return False


def _warn_if_identity_shared(agent: str) -> None:
    """Say so when a SECOND session takes the same identity (#69).

    Identity is per-PROJECT: `.claude/settings.local.json` pins AGENTBUS_AGENT,
    so every session opened in a checkout resolves to the same agent. Two
    sessions then share one delivery cursor, and whichever polls first marks a
    message seen — so the other NEVER SEES IT. A swallowed inbox is
    indistinguishable from an empty one, which is the failure this platform has
    spent a week removing everywhere else.

    The operator hit this deciding whether to open a second session here as a
    frontend agent. The real fix is per-session identity (#69 proper); this is
    the part that can ship without one, and it is the part that matters most:
    the sharing STOPS BEING SILENT.

    Deliberately a warning, not a refusal. Two sessions on one identity is a
    legitimate thing to do knowingly — a handover, a second terminal on the same
    work — and refusing would break it. What is not legitimate is doing it
    without being told.
    """
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if not session:
        # A session that cannot name itself cannot claim an identity, and must
        # not evict the claim of one that can.
        return
    path = _identity_claim_path(agent)
    now = time.time()
    prior: dict[str, Any] = {}
    try:
        if path.exists():
            prior = json.loads(path.read_text())
    except (OSError, ValueError):
        prior = {}

    other = str(prior.get("session") or "")
    seen = float(prior.get("at") or 0)
    # 12 hours: long enough that a genuine second session is caught, short
    # enough that yesterday's stale claim does not cry wolf forever.
    #
    # BUT A CLAIM FILE IS NOT A LIVE SESSION, and treating it as one is #126.
    # This branch used to fire on the TIMESTAMP alone: any different session id
    # written in the last 12 hours produced "ANOTHER SESSION IS ALREADY '<agent>'
    # ... last seen 0 min ago". Nothing checked whether that session still
    # existed, and nothing removes the claim when a session ends, so a session
    # that exited SECONDS ago warns as loudly as one running right now.
    #
    # The predecessor it names is very often the session that just handed over —
    # on a restart that rotates identity, the short-lived session that CREATED
    # the agent leaves exactly this residue. So the alarm is loudest at the one
    # moment it is most likely to be wrong, and it is phrased as if action is
    # required.
    #
    # Reported by bikeroom-freebsd-operato-dd8bca, who hit it on their own
    # restart, repeated it to two peers as established fact, and then went and
    # checked: no transcript or state file for the session named, exactly one
    # live watcher (their own), and no other checkout setting that identity.
    # Their false alarm had by then propagated into a PRIVACY decision about a
    # real screen capture — a warning about a swallowed inbox changed what a
    # human was told about where their desktop image would land.
    #
    # VERIFY A LIVE HOLDER BEFORE ALARMING. `_monitor_pids` is already scoped to
    # (agent, session) and reads real processes, which is the check the monitor
    # script itself uses; the timestamp alone never could be. If no live watcher
    # holds the identity we take the claim over SILENTLY, because "a file says
    # someone was here" is not a finding worth interrupting a session for.
    #
    # TWO VOICES, BECAUSE THERE ARE TWO EPISTEMIC STATES — #128, and this
    # corrects the fix immediately above rather than the original bug.
    #
    # My first pass folded "cannot confirm a holder" into "no holder" and went
    # silent. bikeroom-freebsd-operato-dd8bca — whose incident produced #126 —
    # read the change and pointed out that this is THE SAME CATEGORY ERROR
    # INVERTED: the old code asserted a collision it had not verified; the new
    # code asserted safety it had not verified. They were right.
    #
    # They also caught the justification, which was worse than the code. I wrote
    # that a missed warning "costs duplicate wakes and shared read/ack, which are
    # visible and recoverable" — while the warning text three lines below says,
    # verbatim, "a swallowed message looks exactly like no message". Those cannot
    # both be true. A silent swallow whose signature is identical to the ordinary
    # quiet state is the hardest fault class there is: nobody investigates an
    # absence. The false positive was loud, wrong and self-correcting — found in
    # about four minutes once someone looked. The false negative is undetectable
    # by our own documentation.
    #
    # DEMONSTRATED IN THE WILD, not argued: while #126 was being written, a second
    # session in a worktree held this exact identity via an inherited
    # $AGENTBUS_AGENT (#127) with no monitor of its own. It read two of this
    # session's messages, marking them seen. Under the silent-on-unverified
    # behaviour this session would have seen "no new messages", permanently, with
    # no error anywhere. It only surfaced because that session relayed them out of
    # band. The gap is asymmetric and that is what makes it nasty: the monitorless
    # session IS warned about the one with a monitor, and never the reverse.
    #
    # THE REAL DEFECT WAS NEVER VOLUME, IT WAS UNEARNED CONFIDENCE. "last seen
    # 0 min ago" reads as MEASURED; nothing measured it. That grammar is why a
    # careful reader propagated it to two parties instead of checking it. So the
    # two tiers differ in what they CLAIM, not merely in loudness:
    #
    #   monitor found for the other session -> alarm, liveness VERIFIED
    #   claim present, no monitor found     -> soft notice, LIVENESS NOT VERIFIED
    #
    # The second tier says exactly what was observed and what was not, and names
    # the command that settles it. It is cheap now precisely because SessionEnd
    # releases the claim (#126): a leftover claim means a crash, a kill, or a live
    # session with no monitor — all worth a factual line, none worth an alarm.
    if other and other != session and (now - seen) < 43200:
        mins = int((now - seen) // 60)
        if _identity_held_live(agent, other):
            print(
                f"AgentBus: ANOTHER SESSION IS ALREADY '{agent}' on this machine "
                f"(session {other[:8]}, last seen {mins} min ago; a live monitor "
                f"for it was FOUND, so this is verified, not inferred)."
            )
            print("  You share ONE inbox (one delivery per message for this agent), and")
            print("  read/ack state is shared too: whichever session reads a message first")
            print("  marks it seen, so the other never sees it — a swallowed message looks")
            print("  exactly like no message. (Each session's watcher keeps its OWN cursor,")
            print("  so the risk is duplicate wakes and shared read/ack, not one cursor.)")
            # THE OLD WORDING SENT PEOPLE INTO #129. It said a git worktree
            # "gets its own .claude/settings.local.json and its own identity" —
            # true of the file, false of what wins, because the harness injects
            # the MAIN worktree's env into a linked worktree's session. Following
            # this remedy reproduced the collision it was meant to cure. Naming
            # `.agentbus/agent` explicitly is the difference: it is the file the
            # bleed correction reads, and the one `agentbus setup` writes.
            print(
                "  If that is deliberate, carry on. If not, give this session its "
                "own agent: run `agentbus setup` in a separate checkout or git "
                "worktree so it declares its own `.agentbus/agent`."
            )
        else:
            print(
                f"AgentBus: a claim on '{agent}' was written by session "
                f"{other[:8]} {mins} min ago; LIVENESS NOT VERIFIED (no monitor "
                f"found for it)."
            )
            print("  That session may have exited — or may be running without a monitor")
            print("  (claude -p, or one that failed to start). This line reports what was")
            print("  observed, not a collision: nothing here established that it is live.")
            print(f"  Settle it: agentbus watch-status --agent {agent}")
            print("  If it IS live you share one inbox and read/ack state, so a message")
            print("  read there is hidden here and looks exactly like no message.")
    with contextlib.suppress(OSError):
        path.write_text(json.dumps({"session": session, "at": now}))


def _greet_with_qr(identity: dict[str, Any], agent: str) -> None:
    """Show the agent's address as a QR when a session opens — ONLY on an open
    workspace (SPECS/0032 R6).

    A QR invites whoever can see the screen to mail this agent. Under
    `contacts-only` (the DEFAULT) or `closed`, that stranger's mail is rejected
    as `not_in_contacts` or `ingress_closed`, so showing it advertises an
    address that will not accept what it asks for.

    Everything here fails to silence rather than to display: an absent policy
    field (any server older than the release that added it), a missing extra, a
    missing address. Unrequested output that guesses wrong is worse than none.
    """
    from ..qr import render, should_offer_unrequested

    address = identity.get("address")
    policy = (identity.get("workspace") or {}).get("ingress_policy")
    if not address or not should_offer_unrequested(policy):
        return
    # quiet=True: this display was never asked for, so a session without the
    # optional extra must not be nagged about installing it.
    if render(f"mailto:{address}", quiet=True):
        print(f"AgentBus: scan to mail {agent} directly — {address}")
        print("  (shown because this workspace's ingress policy is `open`)")


def session_start(_: argparse.Namespace) -> int:
    """Print waiting mail as context when a session opens.

    ASKS THE SERVER what is unread, twice over: `whoami` for the authoritative
    count, `inbox(unread=True)` for the preview. The previous version paged
    `inbox(limit=25)` from cursor 0 — the OLDEST page — and filtered it
    locally, so once an agent's history crossed the window every unread message
    sat beyond it and the hook printed nothing, forever. It DEGRADED: correct
    for a young agent, permanently blind for an established one, and the
    silence was byte-identical to "no mail waiting". Found by a peer holding
    `whoami` unread: 2 against a silent greeting at 31 messages of history.

    The count comes from `whoami`, never from len() of a preview page, so a
    short or failed preview cannot round the answer down to zero.
    """
    agent = _resolve_agent()
    if agent is None:
        return EXIT_NOT_WIRED
    _warn_if_shadow_queue()
    _warn_if_env_overrides_this_checkout(agent)
    _warn_if_identity_shared(agent)
    try:
        from ..client import AgentBus

        bus = AgentBus(agent=agent)
        identity = bus.whoami()
        _greet_with_qr(identity, agent)
        count = int(((identity.get("unread") or {}).get("count")) or 0)
        if not count:
            # Served zero is authoritative: clear any stale capture so a later
            # server-unreachable fallback cannot resurrect it as "waiting".
            with contextlib.suppress(OSError):
                _wake_file(agent).unlink(missing_ok=True)
            return 0

        lines = [f"AgentBus: {count} unread message(s) for {agent}."]
        preview = []
        # The preview is optional; the count is not.
        with contextlib.suppress(Exception):
            preview = bus.inbox(limit=25, unread=True)
        for message in preview[:10]:
            lines.append(f"  [{message.seq}] {message.sender}: {message.subject}")
            lines.append(f"      read it: agentbus show {message.delivery_id}")
        shown = min(len(preview), 10)
        if count > shown:
            lines.append(f"  ... and {count - shown} more: agentbus inbox --unread")
            # SAY WHICH TEN THESE ARE, AND HOW TO MAKE THEM GO AWAY (#205).
            #
            # These are the OLDEST unread, which is the right ten to show — a
            # peer blocked longest is the one to answer first. But unlabelled,
            # an unchanging list reads as a frozen surface rather than as a
            # stable backlog, and the count only ever grows. Two agents
            # independently concluded the unread state was broken and asked
            # whether it was a client or a server bug. It is neither: unread is
            # `read_at IS NULL`, this notifier deliberately does not consume
            # (being TOLD you have mail must not mark it read, or "delivered
            # means stored, not read" becomes a lie) — and nothing anywhere
            # named the verb that does consume.
            #
            # `ack` takes SEVERAL ids and sets read_at without requiring
            # `show`, so it is the bulk mark-read path. That was true before
            # this line and documented nowhere.
            lines.append("  (the oldest ten; the list does not change until they are read)")
            lines.append("  mark read without opening: agentbus ack <delivery-id> [<id> ...]")
        # NAME THE REPLY VERB ONCE, AFTER THE LIST (#146). Per-message it would
        # double the length of a backlog notice; omitted entirely — which is how
        # this shipped — it left a reader who had just been told a peer may be
        # BLOCKED with no stated way to answer.
        if preview:
            lines.append("  reply to one: agentbus reply <delivery-id> -b '...'")
        lines.append("A peer may be blocked on one of these. Read before starting work.")
        print("\n".join(lines))

        # Clear the wake file: its contents are now in context.
        wake = _wake_file(agent)
        if wake.exists():
            wake.unlink(missing_ok=True)
    except Exception as exc:
        _hook_warn("check the inbox at session start", exc)
        return 0
    return 0
