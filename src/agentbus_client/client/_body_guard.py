from __future__ import annotations

from typing import Any

from .errors import EmptyBodyError


def _refuse_empty_body(
    text: str | None,
    *,
    html: str | None = None,
    attachments: Any = None,
    payload: Any = None,
    allow_empty: bool,
) -> None:
    if allow_empty or attachments or payload is not None:
        return
    if (text or "").strip() or (html or "").strip():
        return
    raise EmptyBodyError(
        "refusing to send an empty message: the body is empty or only whitespace and "
        "nothing is attached. Pass allow_empty=True (CLI: --allow-empty) to send it anyway."
    )
