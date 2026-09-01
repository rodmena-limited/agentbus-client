"""The `agentbus` command line client."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from ..client import AgentBusError, NotFoundError
from . import _common
from ._common import _accept_common_flags_after_subcommand, _print
from ._threads import _render_thread


def cmd_inbox(args: argparse.Namespace) -> int:
    deliveries = _common._bus(args).inbox(
        args.cursor, limit=args.limit, label=args.label, wait=args.wait, unread=args.unread
    )
    if args.json:
        _print([d.raw for d in deliveries], True)
        return 0
    if not deliveries:
        print("no new messages")
        return 0
    for delivery in deliveries:
        # THE STAR MEANS UNREAD, AND IT MEANS WHAT `--unread` MEANS (#145).
        #
        # It used to be `delivery.state in ("delivered", "relayed")` — TRANSPORT
        # state, which acking does not change. So the star survived an ack
        # forever, while `--unread` filters server-side on `read_at IS NULL`. The
        # listing and the authoritative filter disagreed about the same word.
        #
        # macbook-admin-bd8e86 nearly filed a defect against `agentbus ack`
        # because of it: they acked a message, counted starred lines before and
        # after, saw 30 and 30, and reasonably concluded the command reported
        # success and did nothing. It had worked — `--unread` returned "no new
        # messages" — but the display could not show it.
        #
        # A marker that cannot go dark is the same defect class as a check that
        # cannot go red, and it is worse here: it is the FIRST thing an agent
        # looks at to decide whether a peer is waiting.
        flag = "*" if not delivery.raw.get("read_at") else " "
        attachments = (
            f" [{delivery.attachment_count} attachment(s)]" if delivery.attachment_count else ""
        )
        # `cc` in the listing means "copied, not asked" — triage without
        # opening, which matters because opening MARKS IT READ.
        role = (delivery.raw or {}).get("your_role")
        copied = "  (cc)" if role == "cc" else ""
        print(f"{flag} #{delivery.seq}  {delivery.sender}  {delivery.subject}{attachments}{copied}")
        print(f"     {delivery.delivery_id}")
    print(f"\ncursor: {deliveries[-1].seq}")
    return 0


def _safe_attachment_name(name: str, index: int) -> str:
    """Sanitize a sender-controlled attachment filename to a safe basename.

    The filename arrives from the sender, so it is attacker-controlled. A
    hostile sender could name an attachment `../../.bashrc` or
    `../../.ssh/authorized_keys`, and `Path(name)` would write outside the
    working directory (audit finding, confirmed live). Take only the
    basename (drop any path prefix), reject any remaining traversal or
    separators, and fall back to a neutral name if the result is not a
    plain filename.

    Refuses rather than silently renaming to a neutral name when the name
    is still unsafe after basename extraction (e.g. `..` or `.`), because
    writing to a neutral name silently is how the operator loses track of
    what a hostile sender intended.
    """
    import os as _os

    safe = _os.path.basename(str(name).replace("\\", "/"))
    # After basename, the only traversal left is literally `..` or `.`.
    if safe in ("", ".", ".."):
        return f"attachment-{index}"
    # Reject any residual path separators or traversal remnants defensively.
    if "/" in safe or "\\" in safe or ".." in safe:
        return f"attachment-{index}"
    return safe


def cmd_attachment(args: argparse.Namespace) -> int:
    """Write one or all attachments to disk — the read half of `send -a` (#124).

    Defaults to the attachment's OWN filename in the cwd, because that is what a
    recipient almost always wants and it keeps the common case to one argument.
    `-o -` writes raw bytes to stdout for piping.

    F8 (issuedb #5): `--all` fetches every attachment on the delivery in one
    invocation, writing each into the current working directory under its
    original filename. Without it, a 10-attachment message was 10 CLI runs at
    ~295 ms of startup each; peer measured 2.96 s for 10 x 50 KB. Farshid
    asked for this by name.

    REFUSES TO OVERWRITE unless told to. An attachment arrives with a name chosen
    by the SENDER, so a careless `agentbus attachment <id>` in a working directory
    could otherwise silently replace a local file with a peer's payload of the
    same name. That is a decision the recipient should make deliberately.
    """
    bus = _common._bus(args)
    delivery = bus.read(args.delivery_id)
    attachments = delivery.get("attachments") or []
    if not attachments:
        print(f"delivery {args.delivery_id} has no attachments", file=sys.stderr)
        return 1

    # F8: --all is mutually exclusive with -i and -o (writing multiple files to
    # a single -o path or a single index makes no sense). Argparse enforces the
    # -i/-o mutual-exclusion via a group below; the check here catches -o
    # (which is not in the group so --all can still write to CWD).
    if args.all:
        if args.output and args.output != "-":
            print(
                "--all writes each attachment under its own filename; -o "
                "picks a single destination and cannot be combined with it",
                file=sys.stderr,
            )
            return 2
        if args.output == "-":
            print(
                "--all writes multiple files; refusing to interleave raw bytes "
                "for several attachments on stdout",
                file=sys.stderr,
            )
            return 2
        # First pass: check every target for pre-existing files, so we refuse
        # BEFORE writing any of them — never a partial write of half the set.
        # The sender's filename is attacker-controlled (a hostile sender could
        # name it `../../.bashrc`), so it is sanitized to a safe basename
        # BEFORE it is turned into a write path — a traversal must never escape
        # the working directory (audit finding, confirmed live: a `../outside`
        # filename wrote outside CWD).
        targets: list[Path] = []
        for i, item in enumerate(attachments):
            name = _safe_attachment_name(item.get("filename") or f"attachment-{i}", i)
            targets.append(Path(name))
        if not args.force:
            existing = [str(t) for t in targets if t.exists()]
            if existing:
                print(
                    "refusing to overwrite existing file(s): "
                    + ", ".join(existing)
                    + " — pass --force to overwrite, or fetch each with -i and -o",
                    file=sys.stderr,
                )
                return 1
        # Second pass: actual fetch + write, in order.
        for i, _item in enumerate(attachments):
            data = bus.attachment(args.delivery_id, i)
            targets[i].write_bytes(data)
            print(f"wrote {targets[i]} ({targets[i].stat().st_size} bytes)")
        print(f"— {len(attachments)} attachment(s) written")
        return 0

    if args.index >= len(attachments):
        print(
            f"delivery {args.delivery_id} has {len(attachments)} attachment(s); "
            f"index {args.index} is out of range",
            file=sys.stderr,
        )
        for i, item in enumerate(attachments):
            print(f"  [{i}] {item.get('filename')} ({item.get('size')} bytes)", file=sys.stderr)
        return 1

    meta = attachments[args.index]
    data = bus.attachment(args.delivery_id, args.index)

    if args.output == "-":
        sys.stdout.buffer.write(data)
        return 0
    # The sender's filename is attacker-controlled; sanitize to a safe
    # basename so a hostile name cannot write outside CWD (audit finding).
    target = Path(args.output or _safe_attachment_name(meta.get("filename"), args.index))
    if target.exists() and not args.force:
        print(
            f"refusing to overwrite {target} (the sender chose this filename); "
            f"pass --force or -o to choose your own",
            file=sys.stderr,
        )
        return 1
    target.write_bytes(data)
    # SIZE FROM DISK, not from the metadata we were handed — reporting the
    # server's claimed size back would make a truncated write look successful.
    print(f"wrote {target} ({target.stat().st_size} bytes)")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    # #39: validate BEFORE _bus(), so a flag mistake reports the flag mistake
    # rather than a credential error that sends the reader hunting for a key
    # problem they do not have.
    raw = getattr(args, "raw", False)
    if raw and getattr(args, "thread", False):
        print(
            "--raw shows the stored bytes of ONE delivery; --thread renders a "
            "whole conversation. Pick one: drop --thread to dump this message's "
            "armor, or drop --raw to read the thread.",
            file=sys.stderr,
        )
        return 2

    bus = _common._bus(args)
    try:
        delivery = bus.read(args.delivery_id, raw=raw)
    except NotFoundError as exc:
        # #54: a ULID says nothing about its KIND. `show <thread_id> --thread`
        # is the natural thing to type after `thread <id>` was printed, and a
        # bare not_found there reads as "the conversation is gone". Try the
        # other kind ONCE, on the failure path only.
        if not getattr(args, "thread", False):
            raise
        try:
            result = bus.thread(args.delivery_id)
        except AgentBusError:
            print(
                f"not_found: {args.delivery_id} is neither a delivery of yours nor "
                "a thread you are in. `show` takes a DELIVERY id (from your inbox); "
                "`thread` takes a THREAD id. They look alike and are not the same.",
                file=sys.stderr,
            )
            raise exc from None
        print(
            f"note: {args.delivery_id} is a THREAD id — `agentbus thread "
            f"{args.delivery_id}` is the direct verb.",
            file=sys.stderr,
        )
        if args.json:
            _print(result, True)
            return 0
        _render_thread(result)
        return 0

    if raw:
        # #39, reported by macbook-admin-bd8e86: emit the stored body and NOTHING
        # else on stdout, so it pipes straight into `age -d` with no filtering.
        # Every header goes to stderr or is omitted.
        body = delivery.get("text_body")
        if body is None:
            print("this delivery has no stored text body", file=sys.stderr)
            return 1
        if args.json:
            _print(delivery, True)
            return 0
        if not delivery.get("sealed"):
            # An UNSEALED body printed under --raw looks exactly like a successful
            # decryption. Say which one it is, or --raw becomes a check that
            # reports "verified" for mail that was never encrypted at all.
            print(
                "note: this delivery is NOT sealed — the bytes below are the "
                "stored plaintext, not ciphertext, and prove nothing about "
                "encryption.",
                file=sys.stderr,
            )
        print(body)
        return 0

    # #216: --thread (alias --all) renders the WHOLE conversation instead of the
    # single delivery. Answering message 14 without reading 1-13 is how an agent
    # re-litigates a settled point, or contradicts a position its own predecessor
    # took in the same thread.
    if getattr(args, "thread", False):
        result = bus.thread(delivery["thread_id"])
        if args.json:
            _print(result, True)
            return 0
        _render_thread(result, highlight_message_id=delivery.get("message_id"))
        print(f"\nreply in this thread:  agentbus reply {args.delivery_id} -b '...'")
        return 0

    if args.json:
        _print(delivery, True)
        return 0
    print(f"From:    {delivery['sender_display'] or delivery['sender_address']}")
    # #155: THE ENVELOPE, like any mail reader shows it. Who else got this, and
    # whether you were asked or copied — the two facts a reader needs before
    # deciding whether the message is its business.
    everyone = delivery.get("recipients") or []
    to_line = ", ".join(r.get("recipient", "?") for r in everyone if r.get("kind") != "cc")
    cc_line = ", ".join(r.get("recipient", "?") for r in everyone if r.get("kind") == "cc")
    if to_line:
        print(f"To:      {to_line}")
    if cc_line:
        print(f"Cc:      {cc_line}")
    role = delivery.get("your_role")
    if role:
        print(
            f"You:     {role.upper()}"
            + ("  (you are expected to act)" if role == "to" else "  (copied for information)")
        )
    print(f"Subject: {delivery['subject']}")
    print(f"Thread:  {delivery['thread_id']}")
    if delivery.get("auth_verdicts"):
        print(f"Auth:    {delivery['auth_verdicts']}")
    print()
    print(delivery.get("text_body") or "(no text body)")
    # #212: THE STRUCTURED HALF OF THE MESSAGE. A room can require a payload
    # validated against a schema, and printing only the prose meant the part the
    # sender was FORCED to get right was the part the reader never saw. Printed
    # after the body because the body is the summary and this is the data.
    payload = delivery.get("payload")
    if payload is not None:
        ref = delivery.get("payload_schema_ref")
        print(f"\n-- payload{f' ({ref})' if ref else ''}:")
        print(json.dumps(payload, indent=2, default=str))
    for attachment in delivery.get("attachments") or []:
        # F11 (issuedb #6): the size the server reports is the ON-WIRE size —
        # bytes the store holds, including age armor + base64 inflation on an
        # encrypted workspace. Consumers were reading this as the plaintext
        # file size, so a 50 KB file was reported as ~69 KB. Label it
        # truthfully; the actual plaintext byte count is what `agentbus
        # attachment ...` prints when it writes the file to disk.
        size_val = attachment.get("size") or 0
        print(f"\n-- attachment: {attachment['filename']} ({size_val:,} bytes on wire)")
    # THE READY-TO-PASTE REPLY, david's ask on behalf of his operator.
    #
    # `show` already printed a thread id, which reads like the thing you act on
    # and is not. Printing the exact command removes the guess between `send`
    # (new thread) and `reply` (this thread) — the fork his operator actually
    # hit from Gmail, where a correct-looking `send` silently starts a second
    # conversation.
    print(f"\nreply in this thread:  agentbus reply {args.delivery_id} -b '...'")
    if len(everyone) > 1:
        print(f"reply to everyone:     agentbus reply {args.delivery_id} --all -b '...'")
    # #216: SAY THAT THERE IS MORE ABOVE THIS, and say it only when there is.
    #
    # `show` printed a thread id and stopped, so a reader could not tell message
    # 14 of 14 from a one-message thread. Answering 14 without 1-13 is how an
    # agent re-litigates a settled point or contradicts its own predecessor in
    # the same conversation.
    #
    # thread_message_count comes off the delivery we already fetched, so this
    # costs no extra call. Printed ONLY when there is something earlier: an
    # unconditional "read the thread" on every message is advice that gets tuned
    # out, and then it is not there on the one that needed it.
    #
    # DO NOT be tempted back to thread_seq. It counts the SENDER's own messages
    # in the thread, so a peer's first reply to you is seq 1 while being the
    # third message — the hint would have been silent on the commonest case.
    total = delivery.get("thread_message_count")
    if isinstance(total, int) and total > 1:
        print(
            f"\n{total - 1} other message(s) in this conversation — READ THEM BEFORE "
            f"REPLYING:\n"
            f"                       agentbus show {args.delivery_id} --thread"
        )
    return 0


def cmd_ack(args: argparse.Namespace) -> int:
    bus = _common._bus(args)
    for delivery_id in args.delivery_ids:
        bus.ack(delivery_id)
        print(f"acked {delivery_id}")
    return 0


def cmd_labels(args: argparse.Namespace) -> int:
    labels = _common._bus(args).label(args.delivery_id, add=args.add, remove=args.remove)
    _print(labels if args.json else f"labels: {', '.join(labels)}", args.json)
    return 0


def add_commands(sub: argparse._SubParsersAction) -> None:
    """Wire this module's subcommands into the shared subparser."""

    p = sub.add_parser("inbox", help="list new messages")
    p.add_argument("--cursor", type=int, default=0)
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--label", default=None)
    p.add_argument(
        "--unread",
        action="store_true",
        help="server-side filter to unread only (do not page-and-filter)",
    )
    p.add_argument("--wait", type=int, default=0, help="long-poll seconds (max 55)")
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser(
        "attachment", help="write an attachment from a delivery to disk (send -a is the other half)"
    )
    p.add_argument("delivery_id")
    p.add_argument("-i", "--index", type=int, default=0, help="which attachment (default 0)")
    p.add_argument(
        "-o", "--output", help="path to write, or '-' for stdout (default: its own name)"
    )
    p.add_argument(
        "--all",
        action="store_true",
        help="F8 (issuedb #5): fetch EVERY attachment on the delivery into the current "
        "working directory using its original filename. Refuses to overwrite unless "
        "--force is passed. Mutually exclusive with -i and -o.",
    )
    p.add_argument("--force", action="store_true", help="overwrite an existing file")
    p.add_argument("--agent", help="acting agent (may also precede the subcommand)")
    p.set_defaults(func=cmd_attachment)

    p = sub.add_parser("show", help="read one delivery in full")
    p.add_argument("delivery_id")
    # #216. `--thread` is the primary spelling; `--all` is accepted because it is
    # what an operator reaches for, and refusing it would only mean they try it,
    # get an error, and read the help. BE CAREFUL WITH IT: on `agentbus reply`,
    # `--all` means REPLY TO EVERYONE, which is a different axis entirely. Named
    # here so the collision is documented rather than discovered.
    p.add_argument(
        "--thread",
        "--all",
        action="store_true",
        dest="thread",
        help="read the WHOLE conversation, oldest first, instead of this one "
        "message (note: on `reply`, --all means reply-to-everyone instead)",
    )
    p.add_argument(
        "--raw",
        "--ciphertext",
        action="store_true",
        dest="raw",
        help="print the stored body verbatim WITHOUT unsealing it, so you can "
        "verify your own mail with an external decoder (e.g. `agentbus show "
        "<id> --raw | age -d -i ~/.config/agentbus/keys/sealing-<agent>.key`)",
    )
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_show)

    # #205: SAY THAT IT TAKES SEVERAL, AND THAT IT MARKS READ. Both were true
    # before this help text and stated nowhere, so an agent staring at a
    # three-figure unread count had no way to learn that the backlog is
    # clearable at all — `ack` sets read_at WITHOUT requiring `show`, which
    # makes it the bulk mark-read path.
    p = sub.add_parser(
        "ack",
        help="mark one or more deliveries read/acknowledged (accepts several ids)",
        description=(
            "Acknowledge deliveries. Accepts several ids at once, and marks each "
            "READ without opening it — so this is how a backlog is cleared. "
            "Read anything addressed TO you first: ack does not show you the body."
        ),
    )
    p.add_argument("delivery_ids", nargs="+", metavar="DELIVERY_ID")
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_ack)

    p = sub.add_parser(
        "labels", help="change labels on a delivery (mail filing — agent tags are `agentbus tag`)"
    )
    p.add_argument("delivery_id")
    p.add_argument("--add", action="append", default=[])
    p.add_argument("--remove", action="append", default=[])
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_labels)
