"""#48: blocking a peer whose mail you no longer want.

Operator: "agents need to block spammers (even if trusted in workspace),
sometimes zombie agents annoy others."

TWO PROPERTIES THESE TESTS EXIST TO PIN:

1. SELF-BLOCK IS REFUSED WITHOUT A ROUND-TRIP. It is not hygiene: `agentbus
   remind` is SELF-ADDRESSED, so a self-block would silently break the agent's
   own scheduler. The server refuses it too (422); refusing locally means the
   reader is told WHO the mistake was about instead of being handed a
   credential error to chase.

2. THE SUPPRESSED COUNT IS SURFACED. Blocked mail is REFUSED at recipient
   resolution and never stored, so the counter is the only evidence a block is
   doing anything — climbing means that peer is alive and being refused, static
   means they stopped sending. A listing that omits it makes those two the same
   observation.
"""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stderr, redirect_stdout

import pytest

from agentbus_client.cli import _block


class _Bus:
    def __init__(self, rows=None, result=None):
        self.rows = rows or []
        self.result = result or {}
        self.calls: list[tuple] = []

    def block(self, name, *, reason=None, for_=None, agent=None):
        self.calls.append(("block", name, reason, for_))
        return self.result

    def unblock(self, name, *, agent=None):
        self.calls.append(("unblock", name))
        return self.result

    def blocks(self, *, agent=None):
        self.calls.append(("blocks",))
        return self.rows


def _run(monkeypatch, fn, bus, **flags):
    monkeypatch.setattr(_block._common, "_bus", lambda _a: bus)
    args = argparse.Namespace(
        name=flags.get("name"),
        reason=flags.get("reason"),
        for_=flags.get("for_"),
        json=flags.get("json", False),
        agent=flags.get("agent"),
    )
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = fn(args)
    return code, out.getvalue(), err.getvalue()


def test_blocking_a_peer_calls_the_server(monkeypatch):
    bus = _Bus(result={"agent": "spammer"})
    code, out, _ = _run(monkeypatch, _block.cmd_block, bus, name="spammer", agent="me")
    assert code == 0
    assert bus.calls == [("block", "spammer", None, None)]
    assert "spammer" in out


def test_self_block_is_refused_before_any_server_call(monkeypatch):
    """THE GUARD. `remind` is self-addressed, so this would break the scheduler."""
    bus = _Bus()
    code, _out, err = _run(monkeypatch, _block.cmd_block, bus, name="me", agent="me")
    assert code == 2
    assert bus.calls == [], "a self-block reached the server"
    assert "yourself" in err


def test_blocking_someone_else_is_not_refused(monkeypatch):
    """Known-negative: the self-check must be able to NOT fire, or it would
    block every call and 'refuses self-block' would pass vacuously."""
    bus = _Bus(result={"agent": "peer"})
    code, _out, err = _run(monkeypatch, _block.cmd_block, bus, name="peer", agent="me")
    assert code == 0
    assert "yourself" not in err
    assert bus.calls[0][0] == "block"


def test_a_duration_is_passed_through(monkeypatch):
    bus = _Bus(result={"agent": "zombie", "expires_at": "2026-09-01T00:00:00Z"})
    _code, out, _ = _run(monkeypatch, _block.cmd_block, bus, name="zombie", for_="2h", agent="me")
    assert bus.calls[0][3] == "2h"
    assert "expires" in out


def test_a_permanent_block_says_it_is_permanent(monkeypatch):
    """An unbounded block is the one that rots — it must not read as neutral."""
    bus = _Bus(result={"agent": "x"})
    _code, out, _ = _run(monkeypatch, _block.cmd_block, bus, name="x", agent="me")
    assert "never" in out


def test_the_listing_reports_the_suppressed_count(monkeypatch):
    """Without the count, 'blocking a live peer' and 'they went quiet' are the
    same observation — and the mail is refused, not stored, so nothing else
    records it."""
    bus = _Bus(rows=[{"agent": "spammer", "suppressed_count": 42, "reason": "loop"}])
    _code, out, _ = _run(monkeypatch, _block.cmd_blocks, bus)
    assert "spammer" in out
    assert "42" in out
    assert "loop" in out


def test_no_blocks_says_everyone_can_reach_you(monkeypatch):
    _code, out, _ = _run(monkeypatch, _block.cmd_blocks, _Bus(rows=[]))
    assert "no blocks" in out


def test_unblock_reports_what_was_missed(monkeypatch):
    bus = _Bus(result={"suppressed_count": 7})
    code, out, _ = _run(monkeypatch, _block.cmd_unblock, bus, name="peer")
    assert code == 0
    assert "7" in out


def test_json_mode_emits_the_raw_result(monkeypatch):
    import json

    bus = _Bus(rows=[{"agent": "a", "suppressed_count": 1}])
    _code, out, _ = _run(monkeypatch, _block.cmd_blocks, bus, json=True)
    assert json.loads(out)[0]["agent"] == "a"


@pytest.mark.parametrize("verb", ["block", "unblock", "blocks"])
def test_the_verbs_are_registered(verb):
    import argparse as _ap

    from agentbus_client.cli._parser import build_parser

    p = build_parser()
    choices = next(a.choices for a in p._actions if isinstance(a, _ap._SubParsersAction))
    assert verb in choices


def test_the_sync_and_async_sdks_agree():
    """This pair has drifted before — phonebook(label=) landed on one twin only,
    and async `read` once skipped unsealing entirely."""
    import inspect

    from agentbus_client.client import AgentBus
    from agentbus_client.client.async_client import AsyncAgentBus

    for name in ("block", "unblock", "blocks"):
        s = inspect.signature(getattr(AgentBus, name))
        a = inspect.signature(getattr(AsyncAgentBus, name))
        assert list(s.parameters) == list(a.parameters), f"{name} signatures differ"
        assert inspect.iscoroutinefunction(getattr(AsyncAgentBus, name))


# --- #48: `--for` promises a duration, so a typo is refused locally ----------
#
# Found by TESTING the flag rather than reading it. `_as_instant` deliberately
# passes any string through ("server validates"), which is correct for
# `remind --at`, where an ISO instant is what the caller means. It is wrong for
# a duration flag: `--for tomorrow` travelled to the server as
# expires_at="tomorrow", and the operator would get a schema error naming a
# field they never typed, for a word they did.


@pytest.mark.parametrize("good", ["2h", "90m", "3d", "45"])
def test_a_real_duration_reaches_the_server(monkeypatch, good):
    """KNOWN-POSITIVE. Without it, 'rejects bad durations' would also pass in a
    world where the flag rejected every value."""
    bus = _Bus(result={"agent": "peer"})
    code, _out, _err = _run(monkeypatch, _block.cmd_block, bus, name="peer", for_=good, agent="me")
    assert code == 0
    assert bus.calls[0][3] == good


@pytest.mark.parametrize("bad", ["tomorrow", "2 hours", "next tuesday", "soon"])
def test_a_typo_is_refused_before_any_server_call(monkeypatch, bad):
    bus = _Bus()
    code, _out, err = _run(monkeypatch, _block.cmd_block, bus, name="peer", for_=bad, agent="me")
    assert code == 2
    assert bus.calls == [], "a malformed duration reached the server"
    assert "duration" in err


# --- #48: a LAPSED block must not read as protection you still have ----------
#
# The server LISTS expired blocks rather than hiding them, and that is the right
# call: a block that quietly expired is how a recipient discovers weeks later
# that it has been reachable all along by someone it believed it had stopped.
# Listing them is only half the job — rendering a lapsed row identically to a
# live one makes the reader do date arithmetic to find out they are unprotected.


def test_lapsed_blocks_are_reported_separately_from_active_ones(monkeypatch):
    bus = _Bus(
        rows=[
            {"agent": "live-one", "suppressed_count": 9, "active": True},
            {"agent": "lapsed-one", "suppressed_count": 4, "active": False},
        ]
    )
    _code, out, _ = _run(monkeypatch, _block.cmd_blocks, bus)
    assert "1 active block" in out
    assert "EXPIRED" in out
    assert "can reach you again" in out


def test_all_active_says_nothing_about_expiry(monkeypatch):
    """KNOWN-NEGATIVE: the EXPIRED section must be able to stay absent, or the
    warning is noise that gets ignored by the time it matters."""
    bus = _Bus(rows=[{"agent": "live-one", "suppressed_count": 1, "active": True}])
    _code, out, _ = _run(monkeypatch, _block.cmd_blocks, bus)
    assert "EXPIRED" not in out


def test_a_row_without_the_active_field_is_treated_as_active(monkeypatch):
    """Forward/backward compatibility: an older server omitting `active` must
    not have every block silently rendered as expired — that would tell the
    reader they are unprotected when they are."""
    bus = _Bus(rows=[{"agent": "x", "suppressed_count": 1}])
    _code, out, _ = _run(monkeypatch, _block.cmd_blocks, bus)
    assert "EXPIRED" not in out
    assert "1 active block" in out
