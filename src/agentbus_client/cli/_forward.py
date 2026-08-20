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
        result = _common._bus(args)._request("GET", f"/v1/admin/undeliverable?limit={int(args.limit)}")
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


def cmd_approve(args: argparse.Namespace) -> int:
    bus = _common._bus(args)
    result = bus.request_approval(args.title, kind=args.kind, summary=args.summary)
    print(f"approval {result['id']} is {result['status']}")
    if args.wait:
        settled = bus.approval(result["id"], wait=args.wait)
        print(f"-> {settled['status']}")
        return 0 if settled["status"] == "approved" else 1
    return 0


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
    p.add_argument("--wait", type=int, default=0)
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_approve)
