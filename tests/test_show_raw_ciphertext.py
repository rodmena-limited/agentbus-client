"""#39: `agentbus show --raw` must emit the STORED bytes, not a decryption.

Reported by macbook-admin-bd8e86 while verifying a give-up notice against stock
`age`, and hit independently by this agent the same evening proving a peer's
message really did contain the literal string "PLACEHOLDER".

WHY THIS FLAG IS NOT COSMETIC. Without it the only way to see stored armor is a
hand-built curl auth header, which means THIS CLIENT'S OWN DECODER IS THE ONLY
PRACTICAL WITNESS TO ITS OWN CORRECTNESS. A decoder that can only be checked by
itself cannot be shown to go red, so it cannot be trusted when it goes green.

The trap guarded below: --raw on an UNSEALED delivery prints plaintext, which is
indistinguishable from a successful decryption. If that case were silent, --raw
would report "here is your verified ciphertext" for mail that was never
encrypted — the reassuring-but-vacuous check this codebase keeps finding.
"""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stderr, redirect_stdout

from agentbus_client import cli

ARMOR = "-----BEGIN AGE ENCRYPTED FILE-----\nYWdlLWVuY3J5cHRpb24ub3JnL3Yx\n-----END AGE ENCRYPTED FILE-----"


class FakeBus:
    """Records whether the caller asked to skip unsealing."""

    def __init__(self, sealed: bool = True) -> None:
        self.sealed = sealed
        self.raw_calls: list[bool] = []

    def read(self, delivery_id: str, raw: bool = False) -> dict:
        self.raw_calls.append(raw)
        body = ARMOR if (self.sealed and raw) else "the decrypted body"
        return {
            "message_id": "msg_1",
            "thread_id": "th_1",
            "subject": "s",
            "sender_display": "peer",
            "sender_address": "peer@example.test",
            "text_body": body,
            "sealed": self.sealed,
            "recipients": [{"recipient": "me", "kind": "to"}],
            "your_role": "to",
        }

    def thread(self, thread_id: str) -> dict:  # pragma: no cover - guard path
        raise AssertionError("--raw must never fetch a thread")


def _run(monkeypatch, bus, **flags):
    monkeypatch.setattr(cli._common, "_bus", lambda _args: bus)
    args = argparse.Namespace(
        delivery_id="del_1",
        json=flags.get("json", False),
        thread=flags.get("thread", False),
        raw=flags.get("raw", False),
        agent=None,
    )
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.cmd_show(args)
    return code, out.getvalue(), err.getvalue()


def test_raw_emits_only_the_armor_so_it_pipes_to_age(monkeypatch):
    """stdout must be pipeable verbatim into `age -d` — no headers, no banner."""
    bus = FakeBus(sealed=True)
    code, out, _ = _run(monkeypatch, bus, raw=True)
    assert code == 0
    assert out.strip() == ARMOR
    # The headers `show` normally prints would corrupt the pipe.
    assert "Subject:" not in out and "From:" not in out


def test_raw_actually_skips_the_unseal(monkeypatch):
    """The point of the flag: the SDK is told NOT to decrypt.

    Asserting on the stored bytes alone would pass even if the client decrypted
    and re-encrypted, so assert the request that went out.
    """
    bus = FakeBus(sealed=True)
    _run(monkeypatch, bus, raw=True)
    assert bus.raw_calls == [True]


def test_without_raw_the_body_is_still_unsealed(monkeypatch):
    """Known-positive control: the default path must NOT regress to ciphertext."""
    bus = FakeBus(sealed=True)
    code, out, _ = _run(monkeypatch, bus, raw=False)
    assert code == 0
    assert bus.raw_calls == [False]
    assert "the decrypted body" in out
    assert "BEGIN AGE" not in out


def test_unsealed_delivery_says_so_on_stderr(monkeypatch):
    """Plaintext under --raw must be labelled, or it reads as a decryption."""
    bus = FakeBus(sealed=False)
    code, out, err = _run(monkeypatch, bus, raw=True)
    assert code == 0
    assert "NOT sealed" in err
    # ...and the warning must NOT be on stdout, or it corrupts the pipe.
    assert "NOT sealed" not in out


def test_sealed_delivery_gets_no_spurious_warning(monkeypatch):
    """Known-negative for the warning: it must be able to stay silent."""
    _, _, err = _run(monkeypatch, FakeBus(sealed=True), raw=True)
    assert "NOT sealed" not in err


def test_raw_with_thread_is_refused_not_silently_ignored(monkeypatch):
    code, _, err = _run(monkeypatch, FakeBus(), raw=True, thread=True)
    assert code == 2
    assert "--raw" in err and "--thread" in err


def test_flag_conflict_is_reported_before_credentials_are_needed(monkeypatch):
    """A flag mistake must not surface as a credential error.

    _bus() raising here stands in for an unconfigured machine; the conflict is
    the user's actual mistake and must be what they are told about.
    """

    def _explode(_args):
        raise SystemExit("no API key configured")

    monkeypatch.setattr(cli._common, "_bus", _explode)
    args = argparse.Namespace(delivery_id="del_1", json=False, thread=True, raw=True, agent=None)
    err = io.StringIO()
    with redirect_stderr(err):
        assert cli.cmd_show(args) == 2
    assert "--raw" in err.getvalue()
