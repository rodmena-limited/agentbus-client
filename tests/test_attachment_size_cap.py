"""Per-attachment size cap (REG-6, round-3 audit).

_encode_attachments used to buffer the whole file in RAM, then base64 it, then
put both into the JSON body — peak ~4-5x file size for a single attachment,
before the server even saw the request. A 500 MB video OOM'd small VMs. This
test proves the boundary check refuses oversize files without ever reading
them, and honours the AGENTBUS_MAX_ATTACHMENT_BYTES override.

F7 (issuedb #4) added a SECOND, tighter check for the server's 10 MiB per-
attachment cap. That check fires BEFORE the client RAM cap when both would
refuse, so these tests raise the server cap out of the way to keep exercising
the RAM cap specifically. The server cap has its own dedicated tests in
test_f7_server_cap_check.py.
"""

from __future__ import annotations

import pytest

from agentbus_client import client as client_module
from agentbus_client import sealing as _sealing
from agentbus_client.client import AgentBusError

_AGE_PUBLIC_KEY = _sealing.generate_keypair()[1]  # a real, valid age public key


def _write(path, size: int) -> None:
    """A file of `size` bytes on disk WITHOUT reading it into Python RAM."""
    with open(path, "wb") as f:
        f.seek(size - 1)
        f.write(b"\0")


# The RAM-cap tests below need the server cap out of the way so a 60 MB file
# refusal proves the RAM cap fired, not the server cap.
@pytest.fixture(autouse=True)
def _server_cap_out_of_the_way(monkeypatch):
    monkeypatch.setenv("AGENTBUS_SERVER_MAX_ATTACHMENT_BYTES", str(1024 * 1024 * 1024))


def test_default_50mb_cap_refuses_a_60mb_file(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AGENTBUS_MAX_ATTACHMENT_BYTES", raising=False)
    big = tmp_path / "big.bin"
    _write(big, 60 * 1024 * 1024)  # 60 MB, over the 50 MB default

    with pytest.raises(AgentBusError) as exc:
        client_module._encode_attachments([str(big)])
    msg = str(exc.value)
    assert "big.bin" in msg
    assert "cap" in msg.lower() or "MB" in msg
    # The error must NAME the override so a legitimate large-file caller can act.
    assert "AGENTBUS_MAX_ATTACHMENT_BYTES" in msg


def test_a_file_under_the_cap_is_encoded(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("AGENTBUS_MAX_ATTACHMENT_BYTES", raising=False)
    small = tmp_path / "small.txt"
    small.write_bytes(b"hello world")

    out = client_module._encode_attachments([str(small)])
    assert len(out) == 1
    assert out[0]["filename"] == "small.txt"
    import base64

    assert base64.b64decode(out[0]["content_base64"]) == b"hello world"


def test_env_override_raises_the_cap(tmp_path, monkeypatch) -> None:
    # Cap raised to 100 MB — a 60 MB file now passes.
    monkeypatch.setenv("AGENTBUS_MAX_ATTACHMENT_BYTES", str(100 * 1024 * 1024))
    big = tmp_path / "big.bin"
    _write(big, 60 * 1024 * 1024)

    # This does actually buffer 60MB into RAM — the whole point of the cap
    # is that this trade is explicit and the caller opted into it. Runs on a
    # dev laptop; if a CI runner is memory-constrained, it needs the cap.
    out = client_module._encode_attachments([str(big)])
    assert len(out) == 1


def test_env_override_can_shrink_the_cap(tmp_path, monkeypatch) -> None:
    # Cap SHRUNK to 1 KB — a 2 KB file is now refused.
    monkeypatch.setenv("AGENTBUS_MAX_ATTACHMENT_BYTES", "1024")
    f = tmp_path / "just-over-1kb.bin"
    _write(f, 2048)

    with pytest.raises(AgentBusError):
        client_module._encode_attachments([str(f)])


def test_missing_file_names_the_path(tmp_path) -> None:
    missing = tmp_path / "does-not-exist"
    with pytest.raises(AgentBusError) as exc:
        client_module._encode_attachments([str(missing)])
    assert str(missing) in str(exc.value)


def test_the_oversize_check_does_not_open_the_file(tmp_path, monkeypatch) -> None:
    """The check must be os.stat, not open+read. Otherwise the very memory
    problem the cap exists to prevent would happen inside the check itself."""
    monkeypatch.delenv("AGENTBUS_MAX_ATTACHMENT_BYTES", raising=False)
    big = tmp_path / "big.bin"
    _write(big, 60 * 1024 * 1024)

    opened: list[str] = []
    real_open = open

    def tracking_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    # Patch builtins.open only within client_module's namespace so the test
    # framework itself keeps using the real open. Since _encode_attachments
    # calls `open(...)` as a bare builtin, we patch the module's __builtins__.
    monkeypatch.setattr(client_module.resilience, "open", tracking_open, raising=False)

    with pytest.raises(AgentBusError):
        client_module._encode_attachments([str(big)])
    # The refusal must have happened BEFORE any open() call touched the file.
    assert str(big) not in opened, f"cap check opened the file it was about to refuse: {opened}"


# ------------------------------------------------------ post-seal inflation (F7 follow-up)


# ------------------------------------------------------ encrypted-seal inflation (F7 follow-up)


# A real, valid age public key for the seal tests. Generated at module load
# (age keygen is fast). Tests that don't patch seal_for_bytes need it.
from agentbus_client import sealing as _sealing

_AGE_PUBLIC_KEY = _sealing.generate_keypair()[1]


def test_encrypted_seal_inflation_is_caught_before_upload(monkeypatch):
    """On an ENCRYPTED workspace, the server sees the SEALED base64, which is
    larger than the raw file. The pre-seal check in _encode_attachments
    compares RAW size; this post-seal check compares the WIRE base64 length —
    exactly what the server sees.

    Live AT-CAP finding: raw 7 MiB passed, 8.5 MiB was rejected because
    sealing (1.806x) inflated it past the 10 MiB server cap."""
    import base64 as _b64

    from agentbus_client.client import AgentBus, AgentBusError

    # Use the REAL default cap (the autouse fixture raises it to 1 GiB).
    monkeypatch.delenv("AGENTBUS_SERVER_MAX_ATTACHMENT_BYTES", raising=False)
    bus = AgentBus(api_key="ab_sk_test_test", agent="me")

    # A raw file just under the real 10 MiB cap, sealed at 1.806x -> over.
    raw_payload = {
        "attachments": [
            {
                "filename": "near-cap.bin",
                "content_base64": _b64.b64encode(b"X" * (6 * 1024 * 1024)).decode(),
                "content_type": "application/octet-stream",
            }
        ],
    }
    resolved = {
        "encrypted": True,
        "keys": {"me": [{"public_key": _AGE_PUBLIC_KEY}]},
        "external": False,
        "missing_keys": [],
    }

    with pytest.raises(AgentBusError) as exc:
        bus._apply_seal(raw_payload, resolved)

    msg = str(exc.value)
    assert "ENCRYPTED" in msg
    assert "huge" in msg.lower() or "inflates" in msg
    assert "near-cap.bin" in msg


def test_encrypted_fast_pre_seal_reject_skips_the_seal(monkeypatch):
    """A raw file CLEARLY over the encrypted cap must reject BEFORE the
    expensive seal runs. Proved by a seal that raises if called — if the
    pre-seal estimate fires first, the seal never runs."""
    import base64 as _b64

    from agentbus_client.client import AgentBus, AgentBusError

    monkeypatch.delenv("AGENTBUS_SERVER_MAX_ATTACHMENT_BYTES", raising=False)
    bus = AgentBus(api_key="ab_sk_test_test", agent="me")

    # 8 MiB raw * 1.806 = ~14.5 MiB wire, well over the real 10 MiB cap.
    raw_payload = {
        "attachments": [
            {
                "filename": "huge.bin",
                "content_base64": _b64.b64encode(b"X" * (8 * 1024 * 1024)).decode(),
                "content_type": "application/octet-stream",
            }
        ],
    }
    resolved = {
        "encrypted": True,
        "keys": {"me": [{"public_key": _AGE_PUBLIC_KEY}]},
        "external": False,
        "missing_keys": [],
    }

    def seal_should_never_run(raw, keys):
        raise AssertionError("the expensive seal ran despite the fast pre-seal reject")

    with monkeypatch.context() as m:
        m.setattr("agentbus_client.sealing.seal_for_bytes", seal_should_never_run)

        with pytest.raises(AgentBusError) as exc:
            bus._apply_seal(raw_payload, resolved)

    assert "huge.bin" in str(exc.value)
