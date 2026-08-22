"""F9 (issuedb #3): the "message downgraded to unsigned" notice must live on
stderr, never on stdout, so `agentbus send ... --json | jq` does not choke.

Reported by peer agentbus-ui-c760a1 (batch #2, finding #9). The SDK does not
know whether the caller is emitting --json to stdout, so the notice must be
explicit stderr regardless of the ambient logging config.
"""

from __future__ import annotations

import io
import sys
from unittest.mock import patch

from agentbus_client.client import AgentBus


class _FakeKey:
    """Stand-in for a private signing key so _sign_if_possible reaches the
    downgrade branch — the only branch that emits the notice."""


def _bus() -> AgentBus:
    return AgentBus(api_key="ab_sk_test_test", agent="test-agent")


def test_notice_lands_on_stderr_not_stdout() -> None:
    bus = _bus()
    payload = {"text": "hi", "attachments": [{"filename": "x", "content_base64": ""}]}

    captured_out = io.StringIO()
    captured_err = io.StringIO()
    with (
        patch("agentbus_client.sealing.load_signing_key", return_value=_FakeKey()),
        patch.object(sys, "stdout", captured_out),
        patch.object(sys, "stderr", captured_err),
    ):
        out = bus._sign_if_possible(payload, resolved=None, agent="test-agent")

    # Payload passes through unsigned (as before) — behaviour unchanged.
    assert out is payload
    assert "signature" not in out

    # STDOUT MUST BE EMPTY. Anything else pollutes --json pipes.
    assert captured_out.getvalue() == ""
    # STDERR MUST carry the notice, verbatim start.
    assert "agentbus: message downgraded to unsigned" in captured_err.getvalue()


def test_no_notice_when_body_is_plain_text() -> None:
    """A signable body must not fire the notice — regression guard so a future
    edit does not turn every send into a stderr line."""
    bus = _bus()
    payload = {"text": "hi"}

    captured_err = io.StringIO()
    with (
        patch("agentbus_client.sealing.load_signing_key", return_value=None),
        patch.object(sys, "stderr", captured_err),
    ):
        bus._sign_if_possible(payload, resolved=None, agent="test-agent")
    assert captured_err.getvalue() == ""


def test_no_notice_when_agent_has_no_signing_key() -> None:
    """No signing key -> no downgrade -> no notice, even with attachments."""
    bus = _bus()
    payload = {"text": "hi", "attachments": [{"filename": "x", "content_base64": ""}]}

    captured_out = io.StringIO()
    captured_err = io.StringIO()
    with (
        patch("agentbus_client.sealing.load_signing_key", return_value=None),
        patch.object(sys, "stdout", captured_out),
        patch.object(sys, "stderr", captured_err),
    ):
        bus._sign_if_possible(payload, resolved=None, agent="test-agent")
    assert captured_out.getvalue() == ""
    assert captured_err.getvalue() == ""
