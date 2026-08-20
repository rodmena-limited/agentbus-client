"""Attachment encoding for the send paths (moved out of resilience.py, review #23 file-size cap)."""

from __future__ import annotations

import base64
import mimetypes
import os
from collections.abc import Sequence

from .errors import AgentBusError
from .models import _max_attachment_bytes, _server_max_attachment_bytes


def _encode_attachments(paths: Sequence[str] | None) -> list[dict[str, str]]:
    """Read files and declare the type the bytes actually are.

    REG-6: every file is size-checked BEFORE it is opened; a file over the client
    cap (default 50 MB) or the server cap (10 MiB, F7) is refused without being
    read — peak memory is ~4-5x file size otherwise.
    """
    limit = _max_attachment_bytes()
    server_limit = _server_max_attachment_bytes()
    payload = []
    for path in paths or []:
        try:
            size = os.stat(path).st_size
        except OSError as exc:
            raise AgentBusError(f"cannot read attachment '{path}': {exc}") from exc
        if size > server_limit:
            raise AgentBusError(
                f"attachment '{os.path.basename(path)}' is {size:,} bytes; the "
                f"AgentBus server rejects attachments over {server_limit:,} bytes "
                f"(~{server_limit // (1024 * 1024)} MiB). Failing fast here — "
                "the client would otherwise upload the whole file and wait for "
                "the server to return 413. Split the file, or set "
                "AGENTBUS_SERVER_MAX_ATTACHMENT_BYTES if your server was "
                "reconfigured with a higher ceiling."
            )
        if size > limit:
            raise AgentBusError(
                f"attachment '{os.path.basename(path)}' is {size:,} bytes; the "
                f"client cap is {limit:,} bytes (~{limit // (1024 * 1024)} MB). "
                "The client buffers the whole file in RAM, then again as base64, "
                "then again in the JSON body and the HTTP buffer, so a large "
                "attachment can OOM the sending host well before the server sees "
                "the request. Raise AGENTBUS_MAX_ATTACHMENT_BYTES if this machine "
                "has the RAM budget, or split the file. A streaming upload API "
                "is planned."
            )
        with open(path, "rb") as handle:
            data = handle.read()
        guessed, _ = mimetypes.guess_type(path)
        payload.append(
            {
                "filename": os.path.basename(path),
                "content_base64": base64.b64encode(data).decode(),
                "content_type": guessed or "application/octet-stream",
            }
        )
    return payload
