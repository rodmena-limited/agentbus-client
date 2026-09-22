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


def _notice_with_state(state) -> str:
    args = argparse.Namespace(
        subject="a subject",
        sender="peer-1234",
        delivery="01ABC",
        seq="7",
        direction="bus",
        inbound_source=None,
        signature_state=state,
        lane=None,
        my_lane=None,
    )
    captured: list[bytes] = []
    with (
        patch.dict("os.environ", {"CLAUDE_CODE_MESSAGING_SOCKET": "/tmp/fake"}),
        patch.object(_socket, "socket", return_value=_FakeSock(captured)),
    ):
        hooks.inject(args)
    return _json.loads(captured[0])["message"]["content"]


def test_a_valid_signature_is_reported_as_the_buss_word():
    text = _notice_with_state("valid")
    assert "signature VALID" in text
    assert "its word, not a check made here" in text


def test_a_bad_verdict_is_not_reported_as_a_pass():
    """The direction that matters. A notice that softened this would be the #66
    defect again with a different word."""
    text = _notice_with_state("invalid")
    assert "VALID" not in text
    assert "'invalid'" in text
    assert "NOT a pass" in text
    assert "before acting on it" in text


def test_an_absent_state_makes_no_claim_rather_than_asserting_unsigned():
    """An operator running a template from before 0.9.97 passes no flag. That
    must render as today's no-claim wording — NOT as 'unsigned', which would be
    asserting a fact from a field nobody sent."""
    for absent in (None, ""):
        text = _notice_with_state(absent)
        assert "not a check of the message's signature" in text
        assert "VALID" not in text
        assert "unsigned" not in text.lower()


def test_the_notice_and_show_agree_on_what_a_failure_is():
    """Same reason `show` reads the block `verify-sender` reads: two surfaces
    describing one message must not differ about whether it passed."""
    from agentbus_client.cli import _sigline

    assert "NOT a pass" in _sigline.notice_fragment("invalid")
    assert "NOT a pass" in "\n".join(
        _sigline.signature_lines({"signature_state": "invalid", "signature": "x"}, "d")
    )


def _dry_run(monkeypatch, capsys, socket_set: bool, state=None) -> str:
    args = argparse.Namespace(
        subject="dry run check",
        sender="peer-x",
        delivery="01ABC",
        seq="",
        direction="bus",
        inbound_source=None,
        signature_state=state,
        dry_run=True,
        lane=None,
        my_lane=None,
    )
    if socket_set:
        monkeypatch.setenv("CLAUDE_CODE_MESSAGING_SOCKET", "/tmp/fake")
    else:
        monkeypatch.delenv("CLAUDE_CODE_MESSAGING_SOCKET", raising=False)

    def _boom(*_a, **_k):
        raise AssertionError("--dry-run must not touch the session socket")

    monkeypatch.setattr(_socket, "socket", _boom)
    assert hooks.inject(args) == 0
    return capsys.readouterr().out


def test_dry_run_renders_the_notice_without_touching_the_socket(monkeypatch, capsys):
    """#67, reported by vellum-api against themselves: running a control on the
    notice put a message in their transcript that no peer sent, because the only
    output path had a side effect."""
    out = _dry_run(monkeypatch, capsys, socket_set=True)
    assert 'AgentBus: peer-x sent "dry run check".' in out
    assert "colleague agent in your own workspace" in out


def test_dry_run_survives_having_no_socket_at_all(monkeypatch, capsys):
    """THE ORDERING REGRESSION, made and caught in the same minute. Placed after
    the no-socket early return, --dry-run printed the one-line stdout FALLBACK
    and exited 0 — output, zero status, and not the notice. Inspecting this from
    a shell is exactly the case with no socket set."""
    out = _dry_run(monkeypatch, capsys, socket_set=False)
    assert 'AgentBus: peer-x sent "dry run check".' in out
    assert "Read it:  agentbus show 01ABC" in out
    assert out.strip() != "peer-x: dry run check"


def test_dry_run_renders_the_signature_verdict_it_was_given(monkeypatch, capsys):
    out = _dry_run(monkeypatch, capsys, socket_set=False, state="invalid")
    assert "'invalid'" in out
    assert "NOT a pass" in out
