from __future__ import annotations

import click

from . import _compose
from ._app import argument, verb


@verb("send", _compose.cmd_send, help="send a message", common=True)
@argument("to", nargs=-1, required=True, help="one or more recipients")
@click.option(
    "-c",
    "--cc",
    "cc",
    multiple=True,
    help="copy someone: same delivery, but marked 'informed' rather than 'expected to act'",
)
@click.option(
    "-p",
    "--priority",
    "priority",
    type=click.Choice(["urgent", "normal", "background"]),
    help=(
        "urgent jumps the recipient's triage queue; background yields to it "
        "(default normal). Waiting messages age up, so background still arrives."
    ),
)
@click.option(
    "-s",
    "--subject",
    "subject",
    default="",
    help="subject line; NOT sealed, so keep anything sensitive in the body",
)
@click.option("-b", "--body", "body", help="text, @file, or @- for stdin")
@click.option("-a", "--attach", "attach", multiple=True, help="file to attach; repeat for several")
@click.option(
    "--require-available",
    "require_available",
    is_flag=True,
    help=(
        "refuse rather than queue if the recipient has declared itself busy. "
        "`--require-responsive` asks whether anyone is HOME; this asks whether "
        "anyone is FREE, which only matters when you would rather route "
        "elsewhere."
    ),
)
@click.option(
    "--payload",
    "payload",
    metavar="JSON",
    help=(
        "a structured body: literal JSON, @file, or @- for stdin. If the room "
        "declares a schema, this is validated BEFORE the message is accepted, so "
        "a bad payload is your error rather than every consumer's."
    ),
)
@click.option(
    "--derived-from",
    "derived_from",
    multiple=True,
    metavar="MESSAGE_ID",
    help=(
        "declare an input this message was built from. Repeatable. Recorded as "
        "YOUR claim — the bus observes messages, not transformations, and says so"
        " on every read."
    ),
)
@click.option(
    "--guarantee",
    "guarantee",
    type=click.Choice(["durable", "fire_and_forget"]),
    help=(
        "fire_and_forget trades durability for cost: not stored, not ackable, "
        "never redelivered. Right for a heartbeat, wrong for anything you would "
        "miss. Default durable."
    ),
)
@click.option(
    "--require-ack",
    "require_ack",
    is_flag=True,
    help=(
        "this message carries an ASK the recipient must answer; the server sends "
        "exponential reminders until they ack or the window elapses. TO only, "
        "never CC. Use for messages with a decision, question, or task — NOT for "
        "updates, FYIs, or discussions, or reminders become wallpaper. "
        "Forward-compatible: safe against servers that predate ack-tracking."
    ),
)
@click.option(
    "--ack-window",
    "ack_window",
    metavar="DURATION",
    help=(
        "how long to keep reminding (default 24h when --require-ack is set). "
        "Accepts 90m, 2h, 3d, or bare seconds. Server caps at 7 days (168h)."
    ),
)
def cmd_send_cli() -> None: ...


@verb(
    "send-batch",
    _compose.cmd_send_batch,
    help=(
        "pipe JSON lines from stdin and send many messages in one process — "
        "reuses sealing context + HTTP keep-alive, so throughput is bounded by "
        "network + server (not by ~600 ms process startup per invocation)."
    ),
)
@click.option(
    "--stop-on-error",
    "stop_on_error",
    is_flag=True,
    help=(
        "fail fast on the first failed send (default: continue, emit error lines,"
        " exit non-zero at the end)"
    ),
)
@click.option("--agent", "agent", help="acting agent (may also precede the subcommand)")
def cmd_send_batch_cli() -> None: ...


@verb("reply", _compose.cmd_reply, help="reply to a message", common=True)
@argument("message_id", help="the delivery or message id you are replying to")
@click.option(
    "--all",
    "reply_all",
    is_flag=True,
    help=(
        "reply to EVERYONE on the parent message (sender + its To, Cc kept as Cc,"
        " you excluded). Off by default — the quiet reply is the safe one."
    ),
)
@click.option("-c", "--cc", "cc", multiple=True, help="copy extra recipients")
@click.option(
    "-s",
    "--subject",
    "subject",
    help=(
        "give THIS reply its own subject instead of the server's 'Re: <parent>'. "
        "Stored on the message and shown in the thread view — use it when a long "
        "thread's original subject no longer describes what you are saying, or to"
        " carry a severity a reader can see without opening the body."
    ),
)
@click.option(
    "--to-self",
    "to_self",
    is_flag=True,
    help=(
        "allow a reply whose ONLY recipient is you. Without this, replying to "
        "your own outbound message id is refused, because 'answer the sender' "
        "would deliver to your own inbox while the other party waits."
    ),
)
@click.option(
    "-p",
    "--priority",
    "priority",
    type=click.Choice(["urgent", "normal", "background"]),
    help="urgent, normal or background (default normal)",
)
@click.option("-b", "--body", "body", help="text, @file, or @- for stdin")
@click.option("-a", "--attach", "attach", multiple=True, help="file to attach; repeat for several")
def cmd_reply_cli() -> None: ...
