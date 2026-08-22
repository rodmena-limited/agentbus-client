"""`keys revoke` must tell the truth about whether the private half is here.

BUG: the warning said "Its private half is NOT on this machine ... every message
sealed to it is ALREADY unreadable" while the .superseded file sat in that very
directory and the mail decoded fine.

ROOT CAUSE: rotation writes `sealing-<agent>-<fingerprint>.key.superseded`, and
the agent name contains hyphens. The parser did
`removeprefix("sealing-").removesuffix(".key.superseded")`, which leaves
`agent-fingerprint` — never equal to a bare fingerprint. So the set was ALWAYS
empty and `known_locally` was ALWAYS False.

WHY IT MATTERS MORE THAN A WRONG STRING: it fires on the one path whose entire
purpose is telling the caller what they are about to lose. Reported by
bikeroom-freebsd-operato-b124c2, who checked with `ls` and a real decode instead
of believing the warning — and who declined to trust the check in EITHER
direction until fixed. A false "present" would be worse: revoking while
believing old mail is safe.

THESE TESTS USE REAL FILENAMES, which is the whole point. The parser was
plausible against an imagined `sealing-<fp>.key.superseded` and wrong against
what rotation actually writes.
"""

from __future__ import annotations

import pytest

from agentbus_client.cli._keys import _superseded_fingerprints


@pytest.fixture
def keys_dir(tmp_path, monkeypatch):
    from agentbus_client import sealing

    monkeypatch.setattr(sealing, "key_path", lambda *a, **k: tmp_path / "sealing-x.key")
    return tmp_path


def test_a_hyphenated_agent_name_still_yields_the_bare_fingerprint(keys_dir):
    """THE REPORTED CASE, byte for byte."""
    (keys_dir / "sealing-bikeroom-freebsd-operato-b124c2-e3da2fdd83562a70.key.superseded").touch()
    assert _superseded_fingerprints() == {"e3da2fdd83562a70"}


def test_a_simple_agent_name_works_too(keys_dir):
    (keys_dir / "sealing-alice-76a412e56b8b7d3b.key.superseded").touch()
    assert _superseded_fingerprints() == {"76a412e56b8b7d3b"}


def test_several_superseded_keys_are_all_found(keys_dir):
    """An agent that has rotated more than once holds several old keys, and
    every one of them can still open mail sealed to it."""
    for fp in ("aaaa1111bbbb2222", "cccc3333dddd4444"):
        (keys_dir / f"sealing-agent-with-hyphens-{fp}.key.superseded").touch()
    assert _superseded_fingerprints() == {"aaaa1111bbbb2222", "cccc3333dddd4444"}


def test_an_empty_directory_is_empty_not_an_error(keys_dir):
    """KNOWN-NEGATIVE. If this returned something, the positives above would be
    meaningless — a parser that reports fingerprints for files that do not exist
    would make `known_locally` always True, which is the dangerous direction."""
    assert _superseded_fingerprints() == set()


def test_unrelated_files_are_ignored(keys_dir):
    """The CURRENT key and signing keys must not be mistaken for superseded
    ones — the warning turns on exactly that distinction."""
    (keys_dir / "sealing-alice-ffff0000ffff0000.key").touch()  # current
    (keys_dir / "signing-alice.key").touch()  # wrong algorithm
    (keys_dir / "alice.env").touch()  # credential
    assert _superseded_fingerprints() == set()


def test_the_old_parser_would_fail_these(keys_dir):
    """Documents the exact regression, so a future 'simplification' back to
    removeprefix-only is caught rather than looking tidier."""
    name = "sealing-bikeroom-freebsd-operato-b124c2-e3da2fdd83562a70.key.superseded"
    old = name.removeprefix("sealing-").removesuffix(".key.superseded")
    assert old != "e3da2fdd83562a70", "if this passes, the bug never existed"
    assert old.rsplit("-", 1)[-1] == "e3da2fdd83562a70"
