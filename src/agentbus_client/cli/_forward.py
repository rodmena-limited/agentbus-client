"""The `agentbus` command line client."""

from __future__ import annotations

import argparse
import os
import pathlib
import sys
import tempfile

from ..client import AgentBusError
from . import _common
from ._common import _accept_common_flags_after_subcommand, _print


def cmd_forward(args: argparse.Namespace) -> int:
    """Forward a conversation to a third party, RE-SEALED to their keys.

    #219. Forwarding cannot mean "relay the bytes on". A sealed message is
    encrypted to the keys of the people it was addressed to, so handing that
    ciphertext to somebody new gives them a file they cannot open — a forward
    that looks delivered and is unreadable.

    So the whole process is repeated rather than shortcut: read it, unseal it
    HERE with this agent's own key, then seal the result to every new
    recipient's published key and send that. The plaintext exists only in this
    process's memory; the platform never sees it, exactly as with an ordinary
    send.

    A recipient with no published key is REFUSED by the send path rather than
    quietly excluded — a forward that silently dropped a participant is how
    somebody is left out of a conversation they were told they were in.
    """
    bus = _common._bus(args)
    delivery_id = args.delivery_id
    # `read` unseals with this agent's key on the way through, so what comes
    # back is plaintext this agent was entitled to.
    original = bus.read(delivery_id)
    body = original.get("text_body") or ""
    if not body.strip():
        print(
            "nothing to forward: this message has no readable body. If it is "
            "sealed to a key this agent does not hold, it cannot be forwarded — "
            "the platform cannot re-seal what it never held.",
            file=sys.stderr,
        )
        return 1

    sender = original.get("sender_display") or original.get("sender_address") or "unknown"
    subject = original.get("subject") or "(no subject)"
    quoted = "\n".join("> " + line for line in body.splitlines())
    note = _common._read_body(args.body) or ""
    composed = (
        (note + "\n\n" if note.strip() else "")
        + "---------- Forwarded message ----------\n"
        + f"From: {sender}\n"
        + f"Date: {original.get('created_at') or '(unknown)'}\n"
        + f"Subject: {subject}\n"
        + f"Message: {original.get('message_id') or delivery_id}\n\n"
        + quoted
    )

    # #223: CARRY THE ATTACHMENTS, or refuse — never drop them in silence.
    #
    # forward used to send the quoted text and nothing else, so a message that
    # arrived with files was passed on without them and NOTHING said so. To the
    # recipient that is indistinguishable from a sender who forgot to attach
    # anything; to the sender it looks like a completed forward. Silent partial
    # delivery is the worst of the three outcomes.
    #
    # They cannot simply be relayed, for the same reason the body cannot: on an
    # encrypted workspace each blob is sealed to the ORIGINAL recipients' keys,
    # so handing those bytes to somebody new gives them a file they cannot open.
    # `bus.attachment()` returns the opened bytes, and the send path below seals
    # them again to the new recipients — the same round trip the body makes.
    #
    # SEV-2-F (#234): fetch AND write in one pass, per attachment. The old code
    # collected every attachment into a `carried: list[tuple[str, bytes]]` in
    # memory FIRST, then wrote them to a temp dir, then bus.send() read them back
    # to base64-encode — so 25 x 10 MB rode in RAM as bytes, and again as base64.
    # Streaming each blob through disk means at most ONE attachment's raw bytes
    # exist in RAM at a time; the base64/send layer is unchanged, so a further
    # improvement will come with a streaming send API on the server side.
    originals = original.get("attachments") or []
    #
    # A TEMP DIRECTORY, not NamedTemporaryFile, so the ORIGINAL FILENAME
    # survives. NamedTemporaryFile only offers a suffix, which produced
    # `tmp8siwctcn-report.pdf` on the forwarded copy — the recipient sees a
    # mangled name and cannot tell it from the sender's own sloppiness. The
    # attachment name is part of what is being forwarded.
    with tempfile.TemporaryDirectory() as tmpdir:
        paths: list[str] = []
        for index, meta in enumerate(originals):
            name = (meta or {}).get("filename") or f"attachment-{index}"
            safe = os.path.basename(name) or "attachment"
            path = pathlib.Path(tmpdir) / safe
            try:
                # Fetch + write in one pass; the bytes are released as soon as
                # the write returns, so peak RAM is one attachment, not N.
                path.write_bytes(bus.attachment(delivery_id, index))
            except AgentBusError as exc:
                # REFUSE. Forwarding the text alone would quietly deliver less
                # than the sender believes they sent.
                print(
                    f"cannot forward: attachment {index} ({name}) could not be read "
                    f"({exc}). Forwarding would silently drop it, so nothing was "
                    "sent. Fetch it with `agentbus attachment` and send manually if "
                    "you meant to forward only the text.",
                    file=sys.stderr,
                )
                return 1
            paths.append(str(path))

        # Reuses the ordinary send path, which resolves recipients, seals to
        # EVERY key of every one of them, and refuses if any has none.
        # Re-implementing the sealing here would be a second copy of the rule
        # that matters most.
        result = bus.send(
            to=args.to,
            subject=subject if subject.lower().startswith("fwd:") else f"Fwd: {subject}",
            text=composed,
            cc=args.cc or None,
            priority=getattr(args, "priority", None),
            attachments=paths or None,
        )
    if args.json:
        _print(result, True)
    else:
        who = ", ".join(result.get("recipients") or args.to)
        print(f"forwarded: {result['id']} as {bus.agent or '(key-bound agent)'} to {who}")
        print("  re-sealed to each recipient's own key; the original ciphertext was not relayed")
        if paths:
            print(f"  carried {len(paths)} attachment(s), re-sealed to the new recipients")
    return 0


def cmd_draft(args: argparse.Namespace) -> int:
    """#228: a draft you can create, not only list.

    `agentbus drafts` listed them and nothing could make one, so from the CLI a
    draft was an object you could see and not use — while every other verb on
    the bus (send, reply, forward, keys, verify-sender) had a full surface.
    Found when a peer had to bypass the CLI and call the Python client directly
    to test drafts at all; having to do that WAS the finding.
    """
    bus = _common._bus(args)
    result = bus.create_draft(
        to=args.to, subject=args.subject or "", text=_common._read_body(args.body) or ""
    )
    if args.json:
        _print(result, True)
        return 0
    print(f"draft saved: {result['id']}")
    print(f"  send it:  agentbus draft-send {result['id']}")
    return 0


def cmd_draft_send(args: argparse.Namespace) -> int:
    bus = _common._bus(args)
    result = bus.send_draft(args.draft_id)
    if args.json:
        _print(result, True)
        return 0
    print(f"sent: {result.get('id')} (draft {args.draft_id} is now gone)")
    return 0


def cmd_undeliverable(args: argparse.Namespace) -> int:
    """#227: the bounce quarantine — needs a DASHBOARD SESSION, not a key.

    This surface is PLATFORM-WIDE and deliberately so: mail to the bare tenant
    address belongs to no workspace, which is exactly why it used to vanish. The
    table has no workspace_id at all, so there is nothing to scope a listing by.

    That is why an API key cannot read it, and the refusal is correct rather
    than a gap: every key is bound to ONE workspace, and this list spans all of
    them. Granting a workspace credential a cross-tenant read would be a real
    escalation dressed up as a convenience.

    The verb exists anyway so the refusal is DISCOVERABLE. Before this, an
    operator reaching for the quarantine from a terminal got a bare 401 from a
    curl they had to construct themselves, and — as happened during #227 — a
    naive reader parsed that error body as an empty list and reported "nothing
    was quarantined" from an auth failure.
    """
    try:
        result = _common._bus(args)._request(
            "GET", f"/v1/admin/undeliverable?limit={int(args.limit)}"
        )
    except AgentBusError as exc:
        # NAMED, not swallowed. An empty list here would be indistinguishable
        # from "nothing bounced", which is the precise mistake this verb exists
        # to stop somebody repeating.
        print(
            f"cannot read the quarantine: {exc}\n"
            "\n"
            "This surface is PLATFORM-WIDE — the table has no workspace_id, so a\n"
            "workspace-scoped API key cannot be given a cross-tenant read. It\n"
            "needs a dashboard session: sign in and use the operator UI.\n"
            "\n"
            "This is a refusal, NOT an empty quarantine. Do not read it as one.",
            file=sys.stderr,
        )
        return 1
    rows = result.get("undeliverable") or []
    if args.json:
        _print(result, True)
        return 0
    if not rows:
        print("nothing quarantined")
        return 0
    for row in rows:
        print(f"{row.get('received_at')}  {row.get('reason')}")
        print(f"  from:    {row.get('sender')}")
        print(f"  to:      {row.get('recipient') or row.get('recipient_tag')}")
        print(f"  subject: {row.get('subject')}")
    print(f"\n{len(rows)} quarantined message(s)")
    return 0


def cmd_drafts(args: argparse.Namespace) -> int:
    _print(_common._bus(args).drafts(), True)
    return 0


# The server's terminal set (backend vendors/futex.py:27). ONLY `approved` is a
# go; every other terminal value is a denial. Kept as a literal rather than
# inferred from "not approved", because the two questions are different: a
# status we have never heard of is not a denial, it is a status we must not
# guess about.
_TERMINAL = frozenset({"approved", "rejected", "cancelled", "timed_out", "changes_requested"})


def _print_reviewer_reasoning(outcome: object) -> None:
    """Print WHY a human denied this, from the REAL Futex outcome shape.

    Written against a live rejected approval (01M0GKM6R5QZRQ874P4FQXM1T1) and
    corrected TWICE against it, because both earlier drafts printed nothing on a
    denial that carried a perfectly good explanation:

      1. First draft guessed flat `reason`/`feedback` keys. No such keys exist.
      2. Second draft looked for `reasons`/`justifications` at the TOP of
         `outcome`. They live one level deeper, under `outcome["outcome"]`.

    Both are the same silent-absence failure: the lookup ran, found no such key,
    and reported "no reasoning" with full confidence. `d.get(x)` returning None
    means "no such key" as often as "no such value", which is exactly why this
    was checked against a known-positive rather than reasoned about.

    The real shape:

        outcome = {
          "status": "rejected",
          "feedback_for_agent": {"message": "...", "reason_codes": ["OTHER"]},
          "outcome": {
            "result": "rejected",
            "reasons":        [{"code": "OTHER", "text": "...", "actor_id": ...}],
            "justifications": [{"text": "...", "actor_id": ...}],
          },
        }

    `feedback_for_agent` is the field named for this job, so it is printed
    first. Everything is optional — an approval settled by expiry carries no
    human reasoning at all, and that is not an error.
    """
    if not isinstance(outcome, dict):
        return
    printed = False

    feedback = outcome.get("feedback_for_agent")
    if isinstance(feedback, dict):
        message = feedback.get("message")
        if message:
            codes = feedback.get("reason_codes") or []
            suffix = f" ({', '.join(str(c) for c in codes)})" if codes else ""
            print(f"  feedback{suffix}: {message}")
            printed = True

    inner = outcome.get("outcome")
    if isinstance(inner, dict):
        for key, label in (("reasons", "reason"), ("justifications", "justification")):
            for entry in inner.get(key) or []:
                if not isinstance(entry, dict):
                    continue
                text = entry.get("text")
                if not text:
                    continue
                # The reviewer's feedback message is frequently the SAME string
                # as the reason text; printing it twice reads like two people
                # objected.
                if (
                    printed
                    and key == "reasons"
                    and text
                    == ((feedback or {}).get("message") if isinstance(feedback, dict) else None)
                ):
                    continue
                code = entry.get("code")
                print(f"  {label}{f' ({code})' if code else ''}: {text}")
                who = entry.get("actor_id")
                if who:
                    print(f"    - {who}")
                printed = True

    if not printed:
        # SAY THAT THERE WAS NONE, rather than print nothing. Silence here is
        # indistinguishable from the bug this function has already had twice.
        print("  (no reviewer reasoning recorded)")


def _report_approval(settled: dict) -> int:
    """Print an approval's outcome and turn it into an exit code.

    THREE OUTCOMES, NOT TWO, and collapsing them is the defect this function
    exists to prevent:

      approved                      -> 0, proceed
      any other TERMINAL status     -> 1, a DECISION that says no
      waited_out (still pending)    -> 7, NOBODY ANSWERED YET

    `waited_out` must never share an exit code with a denial. `timed_out` is the
    human's window closing — a denial. Our own `--wait` elapsing is not a
    decision at all, and a script that treats "no answer yet" as "no" will
    abandon work the reviewer was still considering, while one that treats it as
    "yes" proceeds unauthorised. The old `--wait` returned `0 if approved else 1`
    and could not tell them apart.

    `cancelled` is live: the backend delivered nothing to the requester on
    cancellation until build e34edad (its `_deliver_outcome` had one call site,
    the Futex webhook, and a cancellation has no webhook). Reported by the
    backend agent on thread 01M0GTSGPQNYHG2C7G0D39VJP8.
    """
    status = str(settled.get("status") or "unknown")

    # A pending row handed back because OUR wait elapsed. The server sets this
    # flag precisely so the caller can distinguish it; honour that.
    if settled.get("waited_out"):
        print(f"still pending after the wait — NOBODY HAS DECIDED YET ({status})")
        print("  this is NOT a decision. Do not proceed, and do not read it as a")
        print("  refusal: wait again, or stop and say the approval is unanswered.")
        return 7

    if status == "approved":
        print("approved")
        return 0

    if status in _TERMINAL:
        print(f"{status} — DO NOT PROCEED. Anything but 'approved' is a denial.")
        _print_reviewer_reasoning(settled.get("outcome"))
        return 1

    # Not terminal and not waited_out: still open, and the caller did not wait.
    print(f"{status} — not decided yet")
    print(f"  wait for it:  agentbus approval {settled.get('id', '<id>')} --wait 300")
    return 7


def cmd_approve(args: argparse.Namespace) -> int:
    bus = _common._bus(args)
    result = bus.request_approval(args.title, kind=args.kind, summary=args.summary)
    print(f"approval {result['id']} is {result['status']}")
    if args.wait:
        return _report_approval(bus.approval(result["id"], wait=args.wait))
    # NAME THE COMMAND THAT FINISHES THE JOB. Without --wait this returns while
    # the approval is still open, and an agent that stops here has raised a gate
    # and walked through it. `agentbus approval <id>` did not exist until now,
    # so there was nothing to point at.
    print(f"  check it:  agentbus approval {result['id']} --wait 300")
    print("  NOT approved yet — do not proceed on the strength of this id.")
    return 0


def cmd_approval(args: argparse.Namespace) -> int:
    """Report an approval's status by id — the CLI twin of `bus_approval_status`.

    THE GAP THIS CLOSES: `agentbus approve --wait` can only wait on an approval
    it just minted, because it passes its own create-response id straight
    through. A session that restarted, or that was handed an id by a peer, had
    no CLI route to that approval at all — the SDK had `bus.approval(id, wait=)`
    and nothing surfaced it. MCP has had `bus_approval_status` throughout, so
    this was a surface gap rather than a missing capability (issuedb #37,
    SPECS/0025).
    """
    settled = _common._bus(args).approval(args.approval_id, wait=args.wait)
    if getattr(args, "json", False):
        # The machine-readable surface must still CARRY THE VERDICT in its exit
        # code — a script that pipes this to jq is exactly the caller that must
        # not mistake a denial for a pending decision.
        _print(settled, True)
        if settled.get("waited_out"):
            return 7
        status = str(settled.get("status") or "")
        return 0 if status == "approved" else (1 if status in _TERMINAL else 7)
    return _report_approval(settled)


def add_commands(sub: argparse._SubParsersAction) -> None:
    """Wire this module's subcommands into the shared subparser."""

    p = sub.add_parser(
        "forward",
        help="forward a conversation to a third party, RE-SEALED to their keys",
    )
    p.add_argument("delivery_id")
    p.add_argument("to", nargs="+", help="new recipients")
    p.add_argument("-c", "--cc", action="append")
    p.add_argument("-b", "--body", help="a note to put above the forwarded text")
    p.add_argument("-p", "--priority", choices=["urgent", "normal", "background"])
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_forward)

    p = sub.add_parser("draft", help="save a draft without sending it (#228)")
    p.add_argument("to", nargs="+")
    p.add_argument("-s", "--subject")
    p.add_argument("-b", "--body")
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_draft)

    p = sub.add_parser("draft-send", help="send a stored draft (#228)")
    p.add_argument("draft_id")
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_draft_send)

    p = sub.add_parser(
        "undeliverable", help="external mail that could not be routed (operator; #227)"
    )
    p.add_argument("--limit", default=20)
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_undeliverable)

    p = sub.add_parser("drafts", help="list drafts")
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_drafts)

    p = sub.add_parser("approve", help="ask a human to approve something")
    p.add_argument("title")
    p.add_argument("--kind", default="generic")
    p.add_argument("--summary", default=None)
    p.add_argument(
        "--wait",
        type=int,
        default=0,
        metavar="SECONDS",
        help="BLOCK until a human decides, and exit on the outcome. Without it "
        "this returns while the approval is still open — which means you have "
        "raised a gate and not waited at it.",
    )
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser(
        "approval",
        help="check an approval by id (the CLI twin of MCP's bus_approval_status)",
        description=(
            "Report the status of an approval you already have an id for — one "
            "raised by an earlier session, or handed to you by a peer. "
            "`agentbus approve --wait` can only wait on an approval it just "
            "created; this works on any id. "
            "Exit codes: 0 approved, 1 DENIED (rejected/cancelled/timed_out/"
            "changes_requested), 7 nobody has decided yet. 1 and 7 are different "
            "answers and must not be treated alike."
        ),
    )
    p.add_argument("approval_id")
    p.add_argument(
        "--wait",
        type=int,
        default=0,
        metavar="SECONDS",
        help="block until it is decided (server caps the wait; exit 7 if the "
        "wait elapses with no decision)",
    )
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_approval)
