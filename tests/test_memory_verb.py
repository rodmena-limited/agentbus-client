"""`agentbus memory` — the notebook verb, and the two ways it can lie (#341).

THE SERVER CANNOT CATCH EITHER OF THESE, which is why they are tested here. On
an encrypted workspace it holds ciphertext by design, so whether the client
opened an entry, whether it told the truth when it could not, and whether it
addressed the right row are all decided entirely on this side.

  1. AN ENTRY THAT COULD NOT BE OPENED MUST NOT LOOK LIKE CONTENT. Handing back
     armor in the `text` field reads as a note; dropping the row reads as an
     empty notebook. Both are worse than saying so, and the house rule
     (`verifier_negatives_must_be_earned`) is explicit that a tool must
     distinguish "I could not check" from "there is nothing there".

  2. `seq` IS THE ADDRESS, `position` IS DECORATION. They are equal only until
     the first delete. A CLI that passed the position to `rm` would delete a
     different note than the one the reader pointed at — silently, and with a
     success message.

RESEAL GETS ITS OWN SECTION because it is the one operation that can destroy
data: sealing an unopened entry's CIPHERTEXT to a new key produces a
doubly-wrapped body nobody can ever read, converting "find the old key file"
into permanent loss.
"""

from __future__ import annotations

import argparse
from typing import Any

import pytest

from agentbus_client.cli import _memory
from agentbus_client.client.memory import _open_entries, _reseal_plan

# ------------------------------------------------------------------ opening


def _entry(seq: int, text: str, sealed: bool = True, **extra: Any) -> dict[str, Any]:
    return {
        "position": seq,
        "seq": seq,
        "text": text,
        "sealed": sealed,
        "bytes": len(text),
        **extra,
    }


def test_an_unsealed_entry_passes_through_marked_opened():
    """KNOWN-POSITIVE. Every 'could not open' assertion below is only evidence
    because this one passes: a helper that marked everything unopened would
    satisfy them all."""
    result = _open_entries({"memory": [_entry(1, "plain note", sealed=False)]}, "a")
    assert result["memory"][0]["opened"] is True
    assert result["memory"][0]["text"] == "plain note"
    assert "unopened_seqs" not in result


def test_a_sealed_entry_this_machine_cannot_open_is_flagged_not_rendered(monkeypatch):
    """The ciphertext stays in `text` and `opened` is False.

    It is NOT replaced with a friendly string: a caller that wants the raw bytes
    (to re-seal on another host, to diagnose) must still have them. The flag is
    what a renderer branches on.
    """
    from agentbus_client import sealing

    def _boom(_body, _agent=None):
        raise sealing.CannotDecrypt("no key on this machine opens this")

    monkeypatch.setattr(sealing, "unseal_with_any", _boom)
    armor = "-----BEGIN AGE ENCRYPTED FILE-----\nxxxx\n-----END AGE ENCRYPTED FILE-----"
    result = _open_entries({"memory": [_entry(7, armor)]}, "a")

    entry = result["memory"][0]
    assert entry["opened"] is False
    assert entry["text"] == armor, "the raw body must be preserved for recovery"
    assert "7" in entry["open_error"]
    assert result["unopened_seqs"] == [7], "the caller must be able to see it in one field"


def test_a_sealed_entry_that_opens_is_replaced_by_its_plaintext(monkeypatch):
    from agentbus_client import sealing

    monkeypatch.setattr(sealing, "unseal_with_any", lambda body, agent=None: "the real note")
    result = _open_entries({"memory": [_entry(2, "-----BEGIN AGE ENCRYPTED FILE-----")]}, "a")
    assert result["memory"][0]["text"] == "the real note"
    assert result["memory"][0]["opened"] is True


def test_an_empty_notebook_is_not_an_error():
    result = _open_entries({"memory": [], "entries": 0}, "a")
    assert result["memory"] == []
    assert "unopened_seqs" not in result


# ------------------------------------------------------------------- reseal


def test_reseal_never_rewrites_an_entry_it_could_not_open():
    """THE DATA-LOSS CASE. Re-sealing ciphertext produces a doubly-wrapped body
    that no key will ever open, turning a recoverable problem into a permanent
    one."""
    opened = {
        "memory": [
            _entry(1, "readable", opened=True),
            _entry(2, "-----BEGIN AGE...", opened=False),
        ]
    }
    todo, unrecoverable = _reseal_plan(opened)
    assert [e["seq"] for e in todo] == [1]
    assert unrecoverable == [2]


def test_reseal_skips_entries_that_were_never_sealed():
    """A plaintext workspace has nothing to re-seal, and sealing there would be
    refused by the server anyway ('one workspace, one answer')."""
    opened = {"memory": [_entry(1, "plain", sealed=False, opened=True)]}
    todo, unrecoverable = _reseal_plan(opened)
    assert todo == []
    assert unrecoverable == []


# -------------------------------------------------- the positional grammar


def _args(**kw: Any) -> argparse.Namespace:
    base = {
        "action": None,
        "text": None,
        "seq": None,
        "first": None,
        "agent": None,
        "json": False,
        "api_key": None,
        "base_url": None,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def test_rm_takes_a_seq_not_a_position(monkeypatch):
    """`rm 7` must delete SEQ 7, never 'the 7th row'.

    After any delete the two differ, so this is the difference between removing
    the note the reader pointed at and removing a different one — with a
    success message either way.
    """
    seen: dict[str, Any] = {}

    class _Bus:
        def memory_delete(self, seq, agent=None):
            seen["seq"] = seq
            return {"bytes_used": 0, "bytes_limit": 1, "entries": 0}

    monkeypatch.setattr(_memory, "_bus", lambda args: _Bus())
    assert _memory._dispatch(_args(action="rm", text="7")) == 0
    assert seen["seq"] == 7
    assert isinstance(seen["seq"], int), "the seq must reach the client as an int"


def test_a_bare_phrase_is_remembered_rather_than_treated_as_an_action(monkeypatch):
    """`agentbus memory "always check X"` is the common case and must WRITE."""
    seen: dict[str, Any] = {}

    class _Bus:
        def memory_add(self, text, agent=None):
            seen["text"] = text
            return {
                "seq": 1,
                "bytes": 10,
                "bytes_used": 10,
                "bytes_limit": 131072,
                "entries": 1,
                "entries_limit": 256,
            }

    monkeypatch.setattr(_memory, "_bus", lambda args: _Bus())
    assert _memory._dispatch(_args(action="always check X")) == 0
    assert seen["text"] == "always check X"


def test_an_unquoted_multi_word_phrase_is_refused_rather_than_half_stored(monkeypatch):
    """`agentbus memory always check X` — two positionals, rest dropped.

    argparse would hand us action="always", text="check", and the third word is
    GONE. Storing "check" would be a silently truncated note, which is the
    failure the server refuses empty bodies to avoid. Refuse and show the quoted
    form instead.
    """
    monkeypatch.setattr(_memory, "_bus", lambda args: pytest.fail("must not reach the bus"))
    assert _memory._dispatch(_args(action="always", text="check")) == 2


def test_rm_without_a_number_is_refused(monkeypatch):
    monkeypatch.setattr(_memory, "_bus", lambda args: pytest.fail("must not reach the bus"))
    assert _memory._dispatch(_args(action="rm")) == 2
    assert _memory._dispatch(_args(action="rm", text="seven")) == 2


def test_truncate_requires_a_count(monkeypatch):
    monkeypatch.setattr(_memory, "_bus", lambda args: pytest.fail("must not reach the bus"))
    assert _memory._dispatch(_args(action="truncate")) == 2


def test_truncate_accepts_the_count_positionally(monkeypatch):
    seen: dict[str, Any] = {}

    class _Bus:
        def memory_truncate(self, first=None, agent=None):
            seen["first"] = first
            return {"removed": [1], "bytes_used": 0, "bytes_limit": 1, "entries": 0}

    monkeypatch.setattr(_memory, "_bus", lambda args: _Bus())
    assert _memory._dispatch(_args(action="truncate", text="10")) == 0
    assert seen["first"] == 10


# ------------------------------------------------------------------ output


def test_the_renderer_never_prints_ciphertext_as_a_note(capsys):
    armor = "-----BEGIN AGE ENCRYPTED FILE-----\nQUJD\n-----END AGE ENCRYPTED FILE-----"
    _memory._render(
        {
            "memory": [
                {
                    "position": 1,
                    "seq": 9,
                    "text": armor,
                    "sealed": True,
                    "opened": False,
                    "open_error": "no key on this machine opens seq 9",
                }
            ],
            "bytes_used": 400,
            "bytes_limit": 131072,
            "entries": 1,
            "entries_limit": 256,
            "unopened_seqs": [9],
        }
    )
    out = capsys.readouterr().out
    assert "BEGIN AGE" not in out, "armor was rendered as if it were the note"
    assert "SEALED, NOT READABLE HERE" in out
    assert "seq 9" in out


def test_the_renderer_shows_seq_beside_position_so_they_cannot_be_confused(capsys):
    """After a delete these differ, and the reader must be able to see which is
    which — `rm` takes the seq."""
    _memory._render(
        {
            "memory": [
                {"position": 1, "seq": 4, "text": "kept", "sealed": False, "opened": True},
            ],
            "bytes_used": 4,
            "bytes_limit": 131072,
            "entries": 1,
            "entries_limit": 256,
        }
    )
    out = capsys.readouterr().out
    assert "1." in out and "seq 4" in out


def test_a_nearly_full_notebook_says_short_entries_are_the_expensive_part(capsys):
    """The advice has to appear where it can change behaviour. Deleting a few
    one-liners reclaims ~400 bytes each; consolidating them reclaims the
    ~355-byte seal header on every one."""
    _memory._render(
        {
            "memory": [{"position": 1, "seq": 1, "text": "x", "sealed": False, "opened": True}],
            "bytes_used": 125000,
            "bytes_limit": 131072,
            "entries": 200,
            "entries_limit": 256,
        }
    )
    out = capsys.readouterr().out
    assert "Short entries are expensive" in out


def test_an_empty_notebook_renders_as_empty_not_as_nothing(capsys):
    _memory._render(
        {"memory": [], "bytes_used": 0, "bytes_limit": 131072, "entries": 0, "entries_limit": 256}
    )
    assert "memory is empty" in capsys.readouterr().out
