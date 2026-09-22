"""#66: the wake notice must not say "verified" about a message it never checked.

Reported by ledger-ae6b91, who had read the banner as the signature verdict:

    "I HAVE NEVER READ THE SIGNATURE VERDICT ON A SINGLE INCOMING MESSAGE...
     My terminal banner says verified by AgentBus on every one of them, and I
     had been reading that as the verdict — it is transport authentication, not
     a signature state."

They sampled four messages they had acted on — two signed and valid, two
unsigned — and had changed CI configuration and deploy settings on the strength
of the unsigned ones.

The notice is emitted by `inject`, whose arguments are subject, sender,
delivery, seq, direction, inbound-source, lane and my-lane. There is no
signature field: the layer printing the word "verified" holds no signature data
at all, so the claim could never have been true of the message.

Third instance of this class in this module. The two already fixed are recorded
in its own comments — "the old text claimed a transport and a verification model
it had not established", and the 3b fix where "the data said one thing and the
sentence injected into the session said the old thing".
"""

from __future__ import annotations

import argparse
import json as _json
import socket as _socket
from unittest.mock import patch

from agentbus_client.hooks import _inject
from agentbus_client.hooks import claude_code as hooks


class _FakeSock:
    def __init__(self, sink: list[bytes]) -> None:
        self._sink = sink

    def settimeout(self, _s):
        pass

    def connect(self, _addr):
        pass

    def sendall(self, data):
        self._sink.append(data)

    def close(self):
        pass


def _notice(direction: str = "bus", sender: str = "peer-1234") -> str:
    args = argparse.Namespace(
        subject="a subject",
        sender=sender,
        delivery="01ABC",
        seq="7",
        direction=direction,
        inbound_source=None,
        lane=None,
        my_lane=None,
    )
    captured: list[bytes] = []
    with (
        patch.dict("os.environ", {"CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/fake"}),
        patch.object(_socket, "socket", return_value=_FakeSock(captured)),
    ):
        hooks.inject(args)
    assert captured, "no payload was sent to the socket"
    return _json.loads(captured[0])["message"]["content"]


def test_the_harness_reaches_the_bus_branch_at_all():
    """KNOWN-POSITIVE for every assertion below. If `inject` changed shape and
    this returned an empty string or another branch's text, the four negative
    assertions would all pass vacuously — absence of a word in nothing."""
    text = _notice()
    assert "colleague agent in your own workspace" in text
    assert "AgentBus: peer-1234 sent" in text


def test_a_bus_message_notice_does_not_use_the_word_verified_unqualified():
    """THE REGRESSION. `verified` is the word `verify-sender` owns (#64), because
    it is the only thing on this machine that checks a signature."""
    assert "verified by AgentBus" not in _notice()


def test_it_still_says_what_agentbus_DID_check():
    """Stripping the false claim must not strip the true one. A notice that says
    nothing about provenance is not an improvement."""
    assert "authenticated the SENDER" in _notice()


def test_it_names_the_distinction_the_reader_got_wrong():
    assert "not a check of the message's signature" in _notice()


def test_it_points_at_the_two_commands_that_do_answer_it():
    """ledger's failure was not knowing there was a verdict to read. Both the
    display and the local check are named."""
    text = _notice()
    assert "agentbus show" in text
    assert "agentbus verify-sender" in text


def test_the_reword_does_not_leak_into_the_sibling_session_branch():
    """Known-negative: a self-send makes a different and correct claim, and must
    keep making it. Identity is resolved by `_is_self_send`, so it is patched
    rather than inferred from the sender string."""
    with patch.object(_inject, "_is_self_send", return_value=True):
        text = _notice(sender="agentbus-client-c70fbf")
    assert "another session of this same agent" in text
    assert "colleague agent in your own workspace" not in text
    assert "authenticated the SENDER" not in text
