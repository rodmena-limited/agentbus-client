"""#65: `--raw` must emit the stored body and not one byte more.

Reported by agentbus-8dc08d after ledger-ae6b91 hit it building send-verification
on `body_sha256`. The raw branch ended in `print(body)`, which appends `\\n`, so
the output was the stored body plus one byte and its sha256 could never equal
`body_sha256` — the one comparison `--raw` exists to make possible.

Reproduced here 2026-09-22 against agentbus.rodmena.co.uk, delivery
01M33CVNJMHDEND4PS603456R2:

    sha256(--raw)          53f15f5481148a5f…  != body_sha256
    sha256(--raw[:-1])     7ccef3a6ddee083d…  == body_sha256   (exact)
    sha256(rstrip("\\n"))   773599b67795cac8…  != body_sha256

The third line is why this survived: the age armour's own last byte is a
newline, so `rstrip` removes two and the obvious workaround also fails. Any
assertion here that strips whitespace cannot see this bug, which is how
`test_raw_emits_only_the_armor_so_it_pipes_to_age` passed throughout.
"""

from __future__ import annotations

import argparse
import hashlib
import io
from contextlib import redirect_stderr

from agentbus_client import cli

ARMOR = (
    "-----BEGIN AGE ENCRYPTED FILE-----\n"
    "YWdlLWVuY3J5cHRpb24ub3JnL3Yx\n"
    "-----END AGE ENCRYPTED FILE-----\n"
)


class _ByteStdout(io.TextIOWrapper):
    """A stdout with a real `.buffer`, as a terminal or a pipe has."""

    def __init__(self) -> None:
        self._raw = io.BytesIO()
        super().__init__(self._raw, encoding="utf-8", newline="")

    def collected(self) -> bytes:
        self.flush()
        return self._raw.getvalue()


class FakeBus:
    def __init__(self, body: str) -> None:
        self.body = body

    def read(self, delivery_id: str, raw: bool = False) -> dict:
        return {
            "message_id": "msg_1",
            "thread_id": "th_1",
            "subject": "s",
            "sender_display": "peer",
            "sender_address": "peer@example.test",
            "text_body": self.body,
            "sealed": True,
            "recipients": [{"recipient": "me", "kind": "to"}],
            "your_role": "to",
        }

    def thread(self, thread_id: str) -> dict:
        raise AssertionError("--raw must never fetch a thread")


def _raw_bytes(monkeypatch, body: str) -> bytes:
    monkeypatch.setattr(cli._common, "_bus", lambda _args: FakeBus(body))
    args = argparse.Namespace(delivery_id="del_1", json=False, thread=False, raw=True, agent=None)
    stdout = _ByteStdout()
    monkeypatch.setattr("sys.stdout", stdout)
    with redirect_stderr(io.StringIO()):
        assert cli.cmd_show(args) == 0
    return stdout.collected()


def test_raw_emits_the_stored_bytes_and_nothing_more(monkeypatch):
    assert _raw_bytes(monkeypatch, ARMOR) == ARMOR.encode()


def test_raw_does_not_append_a_newline(monkeypatch):
    """THE REGRESSION. One byte, and it is the byte that breaks the digest."""
    out = _raw_bytes(monkeypatch, ARMOR)
    assert not out.endswith(b"\n\n")
    assert len(out) == len(ARMOR.encode())


def test_the_digest_of_raw_output_equals_the_digest_of_the_stored_body(monkeypatch):
    """The comparison `--raw` exists to make possible, and could not."""
    out = _raw_bytes(monkeypatch, ARMOR)
    assert hashlib.sha256(out).hexdigest() == hashlib.sha256(ARMOR.encode()).hexdigest()


def test_a_body_that_does_not_end_in_a_newline_is_not_given_one(monkeypatch):
    """Known-positive for the other direction: nothing is added, ever."""
    out = _raw_bytes(monkeypatch, "no trailing newline here")
    assert out == b"no trailing newline here"


def test_this_check_can_go_red(monkeypatch):
    """The guard's own known-positive: an appended byte MUST fail the assertions
    above. A byte-exactness test that cannot see an extra byte is the reason the
    bug shipped."""
    out = _raw_bytes(monkeypatch, ARMOR) + b"\n"
    assert out != ARMOR.encode()
    assert hashlib.sha256(out).hexdigest() != hashlib.sha256(ARMOR.encode()).hexdigest()
    assert ARMOR.encode().strip() == out.strip(), "and .strip() cannot tell them apart"
