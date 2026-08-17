"""F7 (issuedb #4): pre-upload attachment size check against the server's
per-attachment ceiling (documented 10 MiB), so a caller hits the wall
locally in milliseconds rather than after a 50-second upload.

Reported by peer agentbus-ui-c760a1 (batch #2, finding #7): 11 MiB attachment
took 53.7 s to be rejected with `payload_too_large` after the whole body
arrived.
"""

from __future__ import annotations

import pytest

from agentbus_client import client as client_module
from agentbus_client.client import AgentBusError


def _write(path, size: int) -> None:
    """A file of `size` bytes without reading it into Python RAM."""
    with open(path, "wb") as f:
        f.seek(size - 1)
        f.write(b"\0")


def test_default_10_mib_server_cap_refuses_11_mib_file(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AGENTBUS_SERVER_MAX_ATTACHMENT_BYTES", raising=False)
    monkeypatch.delenv("AGENTBUS_MAX_ATTACHMENT_BYTES", raising=False)
    big = tmp_path / "xxl.bin"
    _write(big, 11 * 1024 * 1024)  # over the 10 MiB server cap

    with pytest.raises(AgentBusError) as exc:
        client_module._encode_attachments([str(big)])
    msg = str(exc.value)
    assert "xxl.bin" in msg
    assert "server rejects" in msg or "server" in msg
    # The exact server byte count must appear so the caller can size-plan
    # without reading a doc.
    assert "10,485,760" in msg or "10485760" in msg or "10 MiB" in msg
    # Fix hint names the override so an operator whose server was raised is not
    # blocked by a stale client.
    assert "AGENTBUS_SERVER_MAX_ATTACHMENT_BYTES" in msg


def test_file_under_server_cap_is_encoded(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AGENTBUS_SERVER_MAX_ATTACHMENT_BYTES", raising=False)
    small = tmp_path / "small.txt"
    small.write_bytes(b"hi")

    out = client_module._encode_attachments([str(small)])
    assert len(out) == 1
    assert out[0]["filename"] == "small.txt"


def test_env_override_raises_the_server_cap(tmp_path, monkeypatch) -> None:
    """If an operator's server was reconfigured up, the client must not block
    them with a stale hardcoded value."""
    monkeypatch.setenv("AGENTBUS_SERVER_MAX_ATTACHMENT_BYTES", str(25 * 1024 * 1024))
    monkeypatch.delenv("AGENTBUS_MAX_ATTACHMENT_BYTES", raising=False)
    mid = tmp_path / "mid.bin"
    _write(mid, 11 * 1024 * 1024)

    # No raise — 11 MiB < 25 MiB override.
    out = client_module._encode_attachments([str(mid)])
    assert out[0]["filename"] == "mid.bin"


def test_server_cap_wins_over_client_ram_cap_when_smaller(tmp_path, monkeypatch) -> None:
    """The server cap fires FIRST when both would refuse — the error message
    the user sees is the one that matches the actual reason the send would
    have failed against the server."""
    monkeypatch.delenv("AGENTBUS_SERVER_MAX_ATTACHMENT_BYTES", raising=False)
    monkeypatch.delenv("AGENTBUS_MAX_ATTACHMENT_BYTES", raising=False)
    big = tmp_path / "huge.bin"
    _write(big, 60 * 1024 * 1024)  # over both caps

    with pytest.raises(AgentBusError) as exc:
        client_module._encode_attachments([str(big)])
    msg = str(exc.value)
    # It must be the SERVER-CAP message, not the client RAM one.
    assert "server rejects" in msg
    assert "AGENTBUS_SERVER_MAX_ATTACHMENT_BYTES" in msg


def test_server_cap_default_matches_documented_10_mib() -> None:
    """The default MUST track the value the server advertises (agentbus skill,
    /llms.txt: 10,485,760 bytes per attachment). Backend #249 will expose this
    machine-readably; until then the constant is the contract."""
    assert client_module._DEFAULT_SERVER_MAX_ATTACHMENT_BYTES == 10 * 1024 * 1024
