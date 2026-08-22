"""Setup's sealing-key publish must retry the transient race and, when it
truly fails, MUST be loud about it (issuedb #15).

Backend #243 diagnostic (thread 01M08QS3M10M49WKT8WVX3P2P7): the ephemeral
onboard-probe agents ended up registered but with no published sealing
pubkey on an encrypted workspace, and every subsequent send TO them failed
"cannot seal: no public key". The setup path silently caught the failure
and added a soft "sealing key: NOT REGISTERED" report line.

Two invariants this file pins:

  1. _sealing_publish_with_retry MUST retry a raise-once-then-succeed
     pattern — the observed shape of newly-minted-key propagation lag
     between /v1/agents/register and /v1/agents/{name}/pubkey.
  2. When retries are exhausted, the caller receives None (never an
     exception up through setup), and the setup report line MUST be
     visibly loud (contains "!!!" or "PUBLISH FAILED" or similar so a
     scanning eye catches it, and names an actionable recovery
     command).
"""

from __future__ import annotations

from agentbus_client import onboarding


class _BusRecording:
    """Records every _request call so tests can assert on retry count + ordering."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[tuple] = []

    def _request(self, method, path, json=None, **kw):
        self.calls.append((method, path, json))
        if not self._responses:
            raise RuntimeError("_request called more times than test scripted")
        next_resp = self._responses.pop(0)
        if isinstance(next_resp, Exception):
            raise next_resp
        return next_resp


# ------------------------------------------------------------- retry helper


def test_first_attempt_succeeds_no_sleep(monkeypatch):
    """Common case: the pubkey publish works first time. No backoff on the
    hot path is the whole point of `delays = (0.0, ...)` in the helper."""
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))

    bus = _BusRecording([{"fingerprint": "abc123"}])
    result = onboarding._sealing_publish_with_retry(bus, "ephemeral-agent", "pubkey-bytes")

    assert result == {"fingerprint": "abc123"}
    assert len(bus.calls) == 1
    assert bus.calls[0][0] == "POST"
    assert bus.calls[0][1] == "/v1/agents/ephemeral-agent/pubkey"
    assert bus.calls[0][2] == {"public_key": "pubkey-bytes"}
    # No sleep on the fast path.
    assert slept == [0.0] or slept == []


def test_raise_once_then_succeed_retries_and_returns_result(monkeypatch):
    """The exact backend #243 propagation-lag shape: attempt 0 fails, attempt
    1 succeeds."""
    monkeypatch.setattr("time.sleep", lambda s: None)  # skip real waits

    bus = _BusRecording(
        [
            RuntimeError("transient: newly-minted key not visible to pubkey endpoint yet"),
            {"fingerprint": "abc123"},
        ]
    )
    result = onboarding._sealing_publish_with_retry(bus, "ephemeral", "pubkey")

    assert result == {"fingerprint": "abc123"}
    assert len(bus.calls) == 2  # one retry


def test_three_failures_return_none_never_raise(monkeypatch):
    """The whole point of the helper is that setup keeps running with a
    visible warning instead of crashing. None is the signal to the caller."""
    monkeypatch.setattr("time.sleep", lambda s: None)

    bus = _BusRecording(
        [
            RuntimeError("attempt 0 failed"),
            RuntimeError("attempt 1 failed"),
            RuntimeError("attempt 2 failed"),
        ]
    )
    result = onboarding._sealing_publish_with_retry(bus, "e", "pk")
    assert result is None
    assert len(bus.calls) == 3


def test_backoff_delays_grow(monkeypatch):
    """Sanity: retry 1 waits longer than retry 0. Prevents someone
    setting all delays to 0.0 in a refactor and turning the retry into a
    tight loop that hammers a struggling server."""
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))

    bus = _BusRecording([RuntimeError("f0"), RuntimeError("f1"), {"fingerprint": "z"}])
    result = onboarding._sealing_publish_with_retry(bus, "e", "pk")

    assert result == {"fingerprint": "z"}
    # slept once per retry (attempts 1 and 2), and the second sleep > first.
    non_zero = [s for s in slept if s > 0]
    assert len(non_zero) >= 2
    assert non_zero[1] > non_zero[0]


def test_helper_survives_final_failure_being_a_different_exception_shape(monkeypatch):
    """A ConnectionError, an AgentBusError with .status=502, an OSError —
    every transient shape must be retried, and every terminal shape must
    result in None rather than a raise up the stack."""
    monkeypatch.setattr("time.sleep", lambda s: None)

    class MadeUp(RuntimeError):
        pass

    bus = _BusRecording([OSError("net"), ConnectionError("reset"), MadeUp("weird")])
    result = onboarding._sealing_publish_with_retry(bus, "e", "pk")
    assert result is None


# ------------------------------------------------------------- loud message


def test_report_line_on_publish_failure_is_loud_enough_to_notice():
    """Golden-string check: the report line the setup path writes when the
    publish helper returns None must contain a visible marker (something
    stronger than a soft 'NOT REGISTERED') and name an actionable recovery
    command. This is verified by reading the setup source, because writing
    a full end-to-end test of _setup_claude is a fixture heap.

    The failing text WAS:
      'sealing key: NOT REGISTERED ({exc}) — rerun `agentbus setup`'
    which sits between a dozen other 'sealing key: ...' lines in the
    report and reads as routine on a scanning eye. Any refactor that
    strips the '!!!' or drops the recovery command names this test."""
    import inspect

    src = inspect.getsource(onboarding._setup_claude)
    # The loud marker in the publish-failed branch.
    assert "!!! PUBLISH FAILED" in src
    # The exit-loud marker for the non-publish exception branch.
    assert "!!! NOT REGISTERED" in src
    # An actionable recovery is named in at least one of the branches.
    assert "agentbus keys rotate" in src
