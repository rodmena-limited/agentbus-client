"""Rotating a sealing key must not strand yesterday's mail.

`agentbus keys rotate` keeps the old private key beside the new one and tells
the user so. That advice is empty if nothing ever reads the old key again —
a message sealed to it is still that agent's mail, and only the key changed.

The live proof is in #191: sealed send → rotate → both the pre-rotation and
post-rotation messages open on the deployed system, and REMOVING the superseded
file makes the pre-rotation one unreadable again. That last step is the control:
without it, "it opened" is equally consistent with the message never having been
sealed to the old key at all.

These tests are the same shape without the network.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentbus_client import sealing


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(sealing, "config_dir", lambda: tmp_path)
    # A sealing key now belongs to ONE agent, so a test must say which it is.
    # Without this the helpers cannot resolve a path at all, which is the point:
    # a key with no owner is the shared key the per-agent split removed.
    monkeypatch.setenv("AGENTBUS_AGENT", "a")
    (tmp_path / "keys").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _rotate() -> tuple[str, str]:
    """Drive the REAL rotate code path, not a re-implementation of it.

    This function used to copy the file itself, which is how it agreed with a
    bug for two releases: the CLI wrote every retired key to ONE fixed
    `.key.superseded`, so a second rotation destroyed the first — and a test
    that mimics the writer cannot see that, because it mimics it correctly.
    Measured on 0.5.4 from PyPI by macbook-admin-bd8e86.
    """
    import argparse

    from agentbus_client import cli

    class _NoServer:
        agent = "a"

        def _request(self, *_a: object, **_k: object) -> dict[str, bool]:
            return {"ok": True}

    old = sealing.load_private_key() or ""
    cli._keys_rotate(
        _NoServer(),
        argparse.Namespace(keys_action="rotate", label="t", yes=True, json=False, agent="a"),
        "a",
        cli._this_machines_fingerprint(),
    )
    return old, sealing.load_private_key() or ""


@pytest.mark.usefixtures("store")
def test_a_message_sealed_before_rotation_still_opens_after() -> None:
    _old_private, old_public = sealing.ensure_keypair()
    sealed = sealing.seal_for("yesterday's mail", [old_public])

    _old, _new = _rotate()

    assert sealing.unseal_with_any(sealed) == "yesterday's mail"


@pytest.mark.usefixtures("store")
def test_and_stops_opening_once_the_superseded_key_is_gone() -> None:
    """THE CONTROL. Without this, the test above passes just as happily if the
    body were never sealed to the old key — or if unseal_with_any silently
    returned the input."""
    _old_private, old_public = sealing.ensure_keypair()
    sealed = sealing.seal_for("yesterday's mail", [old_public])
    _rotate()
    # Retired keys are named per fingerprint now, so the control deletes
    # whatever was actually retired rather than a filename it assumes.
    for retired in sealing.key_path("a").parent.glob("sealing-a*.superseded"):
        retired.unlink()

    with pytest.raises(sealing.CannotDecrypt):
        sealing.unseal_with_any(sealed)


@pytest.mark.usefixtures("store")
def test_mail_sealed_to_the_new_key_opens_too() -> None:
    """Both directions: rotation must not break the key it just installed."""
    sealing.ensure_keypair()
    _old, new_private = _rotate()
    new_public = sealing.public_from_private(new_private)

    sealed = sealing.seal_for("today's mail", [new_public])
    assert sealing.unseal_with_any(sealed) == "today's mail"


@pytest.mark.usefixtures("store")
def test_the_current_key_is_tried_first() -> None:
    """Ordering is not cosmetic: almost every message uses the current key, so
    the common path should do one attempt and stop rather than walking a
    growing pile of retired keys."""
    _private, _public = sealing.ensure_keypair()
    old, new = _rotate()
    assert sealing.load_private_keys()[0] == new
    assert old in sealing.load_private_keys()


@pytest.mark.usefixtures("store")
def test_attachments_follow_the_same_rule() -> None:
    _old_private, old_public = sealing.ensure_keypair()
    blob = bytes(range(256)) * 300  # multi-chunk, per the boundary lesson
    sealed = sealing.seal_for_bytes(blob, [old_public])
    _rotate()
    assert sealing.unseal_bytes_with_any(sealed) == blob


@pytest.mark.usefixtures("store")
def test_no_keys_at_all_is_distinguishable_from_a_wrong_key() -> None:
    """ "This machine has no key" and "no key here opens this" need different
    remedies — signin versus find the old key — so they must not collapse into
    one message."""
    assert sealing.load_private_keys() == []
    _private, public = sealing.generate_keypair()
    sealed = sealing.seal_for("not for this machine", [public])
    with pytest.raises(sealing.CannotDecrypt):
        sealing.unseal_with_any(sealed)


@pytest.mark.usefixtures("store")
def test_rotating_TWICE_keeps_all_three_keys() -> None:
    """DATA LOSS, found on 0.5.4 from PyPI by macbook-admin-bd8e86.

    `.key.superseded` was a fixed filename, so the second rotation overwrote the
    first retired key in place. Every message sealed to it became unreadable by
    that agent forever — silently, irreversibly, and worst for the operator who
    rotates most often, which is the behaviour we want to encourage.

    `keys held: 2` after two rotations looks exactly like `keys held: 2` after
    one, which is why nothing reported it. Retired keys are now named by
    fingerprint, one file each.
    """
    _private, public_a = sealing.ensure_keypair()
    mail_a = sealing.seal_for("A-mail (oldest)", [public_a])

    _rotate()
    public_b = sealing.public_from_private(sealing.load_private_key() or "")
    mail_b = sealing.seal_for("B-mail", [public_b])

    _rotate()
    public_c = sealing.public_from_private(sealing.load_private_key() or "")
    mail_c = sealing.seal_for("C-mail (current)", [public_c])

    assert len(sealing.load_private_keys()) == 3
    assert sealing.unseal_with_any(mail_a) == "A-mail (oldest)"
    assert sealing.unseal_with_any(mail_b) == "B-mail"
    assert sealing.unseal_with_any(mail_c) == "C-mail (current)"


@pytest.mark.usefixtures("store")
def test_a_retired_key_is_readable_only_by_its_owner() -> None:
    """0600, like the live one. A retired key opens the same mail the current
    one did, so leaving it world-readable would make rotation a downgrade in
    secrecy."""
    import stat

    sealing.ensure_keypair()
    _rotate()
    retired = list(sealing.key_path("a").parent.glob("sealing-a*.superseded"))
    assert retired, "nothing was retired — the fixture proves nothing"
    for path in retired:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path.name


@pytest.mark.usefixtures("store")
def test_a_key_retired_by_an_OLDER_client_is_still_read() -> None:
    """0.5.2-0.5.4 wrote one fixed `sealing.key.superseded`, and machines that
    rotated under those still have it. Reading only the new per-fingerprint
    shape would strand exactly the mail this whole mechanism exists to keep."""
    old_private, old_public = sealing.generate_keypair()
    sealed = sealing.seal_for("mail from before the fix", [old_public])
    directory = sealing.key_path("a").parent
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "sealing-a-legacy.key.superseded").write_text(old_private)
    sealing.ensure_keypair()

    assert sealing.unseal_with_any(sealed) == "mail from before the fix"
