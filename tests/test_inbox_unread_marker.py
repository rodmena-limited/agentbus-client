"""#145 — the inbox star tracked transport state, not read state.

    flag = "*" if delivery.state in ("delivered", "relayed") else " "

`state` is TRANSPORT state and acking does not change it, so the star survived an
ack forever. `--unread` meanwhile filters server-side on `read_at IS NULL`. The
listing and the authoritative filter disagreed about the same word.

macbook-admin-bd8e86 nearly filed a defect against `agentbus ack` over it: they
acked a message, counted starred lines before and after, got 30 and 30, and
reasonably concluded the command reported success and did nothing. The acks had
worked — `--unread` returned "no new messages" — but the display could not show
it.

A marker that cannot go dark is the same class as a check that cannot go red, and
it is worse here: the inbox listing is the FIRST thing an agent looks at to decide
whether a peer is waiting on it.

BOTH DIRECTIONS: a star that never appears is as broken as one that never clears.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentbus_client import cli
from agentbus_client.client import Delivery


def _delivery(seq: int, *, read: bool, state: str = "delivered") -> Delivery:
    return Delivery.from_api(
        {
            "delivery_id": f"01D{seq}",
            "agent_seq": seq,
            "state": state,
            "subject": f"subject {seq}",
            "sender_display": "peer via AgentBus",
            "read_at": "2026-08-14T05:00:00Z" if read else None,
        }
    )


def _render(monkeypatch, capsys, deliveries):
    import argparse

    class _Bus:
        def inbox(self, *_a, **_k):
            return deliveries

    monkeypatch.setattr(cli._common, "_bus", lambda _a: _Bus())
    args = argparse.Namespace(
        cursor=None, limit=50, label=None, wait=0, unread=False, json=False, agent=None
    )
    cli.cmd_inbox(args)
    return capsys.readouterr().out


def test_an_unread_delivery_is_starred(monkeypatch, capsys):
    out = _render(monkeypatch, capsys, [_delivery(1, read=False)])
    assert "* #1" in out, "an unread message is not marked, so nothing signals a waiting peer"


def test_a_read_delivery_is_not_starred(monkeypatch, capsys):
    """THE FIX. Previously the star came from transport state, which acking and
    reading both leave untouched, so it never cleared."""
    out = _render(monkeypatch, capsys, [_delivery(2, read=True)])
    assert "* #2" not in out, "a read message is still starred; the marker cannot go dark"
    assert "#2" in out, "the message vanished entirely — it should render, just unstarred"


def test_transport_state_no_longer_decides_the_marker(monkeypatch, capsys):
    """A read message in the 'delivered' transport state — the exact combination
    that produced the phantom-bug report — must NOT be starred."""
    out = _render(monkeypatch, capsys, [_delivery(3, read=True, state="delivered")])
    assert "* #3" not in out


def test_an_unread_message_in_any_state_is_still_starred(monkeypatch, capsys):
    """Guards the over-correction: keying on read_at must not lose the star for
    states the old code did not list."""
    out = _render(monkeypatch, capsys, [_delivery(4, read=False, state="read")])
    assert "* #4" in out
