"""#53: the self-reply guard, shared by the sync and async reply paths."""

from __future__ import annotations

from typing import Any

from .errors import SelfReplyError


def _refuse_self_reply(
    resolved: dict[str, Any] | None,
    acting: str | None,
    message_id: str,
    *,
    allow_self: bool,
) -> None:
    """#53: refuse a reply that the server would deliver ONLY to its sender.

    The one place the client learns who a reply reaches is the resolver's
    answer (#220), so the check lives on that answer and nowhere else — we
    do not re-derive the addressing rule (#155's lesson). Three ways this
    stays silent, each deliberate:

      * `resolved` is None: the resolve route was refused or absent, so the
        recipient set is unknown and the send proceeds as it always did.
      * `acting` is None: a key-bound call with no agent name to compare.
      * the set includes ANYONE but you (reply-all, an explicit cc): that is
        a reply that reaches someone, which is what the caller wanted.

    KNOWN-POSITIVE: resolve-reply on your own outbound message id answers
    to=[you], cc=[] (probed live 2026-09-01 on 01M1FD9QRFF39WTTMQA8ZP7M4X).
    """
    if allow_self or resolved is None or not acting:
        return
    to = list(resolved.get("to") or [])
    cc = list(resolved.get("cc") or [])
    if to == [acting] and not cc:
        raise SelfReplyError(
            f"reply to {message_id} would reach only YOU ({acting}): that id is a "
            "message you sent, so 'answer the sender' means yourself. Reply to the "
            "other party's message id instead, or pass allow_self=True / --to-self "
            "if a note to yourself is what you meant.",
            message_id=message_id,
            acting=acting,
            body={"to": to, "cc": cc},
        )
