"""Attachment encoding for the send paths (moved out of resilience.py, review #23 file-size cap)."""

from __future__ import annotations

import base64
import mimetypes
import os
from typing import TYPE_CHECKING

from .errors import AgentBusError
from .models import _SEAL_INFLATION_FACTOR, _max_attachment_bytes, _server_max_attachment_bytes

if TYPE_CHECKING:
    from collections.abc import Sequence


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
            msg = f"cannot read attachment '{path}': {exc}"
            raise AgentBusError(msg) from exc
        if size > server_limit:
            # ENCRYPTION CONTEXT BELONGS HERE TOO, AND USED NOT TO BE.
            #
            # This raw-size check runs BEFORE sealing, so on an encrypted
            # workspace it fired first and the caller never reached the
            # encryption-aware message in base.py. The result was a message that
            # got MORE useful as the file got smaller: a 5.9 MB file was told the
            # real effective limit (~5.5 MiB raw), while an 11 MB file on the
            # same workspace was told "the limit is ~10 MiB" — true of the RAW
            # cap and silent about the number they actually need to plan around.
            # The bigger the overage, the less the message helped.
            #
            # Reported by macbook-admin-bd8e86, who walked the boundary at four
            # sizes rather than testing one and generalising.
            #
            # `sealed` is not known here (this runs before the workspace is
            # resolved), so the note is phrased conditionally rather than
            # asserting an encryption state we cannot see from this function.
            effective = int(server_limit / _SEAL_INFLATION_FACTOR)
            msg = (
                f"attachment '{os.path.basename(path)}' is {size:,} bytes; the "
                f"AgentBus server rejects attachments over {server_limit:,} bytes "
                f"(~{server_limit // (1024 * 1024)} MiB). Failing fast here — "
                "the client would otherwise upload the whole file and wait for "
                "the server to return 413. Split the file, or set "
                "AGENTBUS_SERVER_MAX_ATTACHMENT_BYTES if your server was "
                "reconfigured with a higher ceiling.\n"
                f"  On an ENCRYPTED workspace the effective limit is lower still: "
                f"sealing inflates by x{_SEAL_INFLATION_FACTOR:.3f}, so the largest "
                f"raw file that fits is ~{effective:,} bytes "
                f"(~{effective // (1024 * 1024)} MiB), not {server_limit // (1024 * 1024)} MiB."
            )
            raise AgentBusError(msg)
        if size > limit:
            msg = (
                f"attachment '{os.path.basename(path)}' is {size:,} bytes; the "
                f"client cap is {limit:,} bytes (~{limit // (1024 * 1024)} MB). "
                "The client buffers the whole file in RAM, then again as base64, "
                "then again in the JSON body and the HTTP buffer, so a large "
                "attachment can OOM the sending host well before the server sees "
                "the request. Raise AGENTBUS_MAX_ATTACHMENT_BYTES if this machine "
                "has the RAM budget, or split the file. A streaming upload API "
                "is planned."
            )
            raise AgentBusError(msg)
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
