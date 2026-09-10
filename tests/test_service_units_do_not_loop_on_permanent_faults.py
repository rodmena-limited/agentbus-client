"""#58: a permanent auth failure must not become an infinite restart loop.

Measured from an nginx access log by infra-manager-c13110 and forwarded by the
server team: 24,324 401s in ONE DAY from a single host, flat across every hour,
sustained for 23 days. `agentbus-david.service` showed NRestarts=404,386,
restarting every 5s, enabled so it survived reboot.

The retry classifier was never the problem: the SDK already treats 401 as
definitive and cmd_watch already exits 8. The loop was the unit WE EMIT —
Restart=always with StartLimitIntervalSec=0, which restarts on every exit status
and disables systemd's own start-rate brake. A comment in that file already
named the hazard and it was never acted on.

A 401 from AgentBus is permanent by construction: `unauthenticated` (no key) or
`invalid_api_key` (missing, malformed, unknown, revoked). None becomes valid by
waiting.
"""

from __future__ import annotations

import argparse
import io
from contextlib import redirect_stdout

import pytest

from agentbus_client import cli


def _emit(manager: str) -> str:
    args = argparse.Namespace(
        agent="probe-agent", manager=manager, base_url="https://bus.test", json=False
    )
    buf = io.StringIO()
    err = io.StringIO()
    import contextlib

    with redirect_stdout(buf), contextlib.redirect_stderr(err):
        cli.cmd_service(args)
    return buf.getvalue() + err.getvalue()


def test_systemd_refuses_to_restart_a_permanent_auth_failure():
    unit = _emit("systemd")
    assert "RestartPreventExitStatus=" in unit, "the 404,386-restart loop is back"
    line = next(x for x in unit.splitlines() if x.startswith("RestartPreventExitStatus="))
    codes = set(line.split("=", 1)[1].split())
    assert "8" in codes, "exit 8 is AuthError — a revoked key cannot be fixed by waiting"


def test_systemd_still_restarts_forever_on_a_transient_fault():
    """KNOWN-POSITIVE TWIN. The point is to stop hammering a wall, not to make a
    network blip terminal — a watcher that gives up on DNS is worse than one that
    retries."""
    unit = _emit("systemd")
    assert "Restart=always" in unit
    line = next(x for x in unit.splitlines() if x.startswith("RestartPreventExitStatus="))
    codes = set(line.split("=", 1)[1].split())
    assert "1" not in codes, "exit 1 is the generic failure; suppressing it would strand outages"
    assert not codes & {"7"}, "7 is a dead wake socket, which a new session legitimately re-arms"


@pytest.mark.parametrize("manager", ["launchd", "rc.d"])
def test_a_manager_without_exit_status_suppression_says_so(manager):
    """Neither launchd nor daemon(8) can suppress a restart per exit status.
    Implying parity would leave an operator believing a loop is impossible on a
    platform where it is merely slower."""
    out = _emit(manager)
    assert "per-exit-status" in out, "must name the missing capability, not imply parity"
    assert "8" in out and "revoked" in out, "must name the exit status and what causes it"


@pytest.mark.parametrize(
    ("manager", "needle"),
    [("launchd", "ThrottleInterval"), ("rc.d", "-R 60")],
)
def test_the_unsuppressable_managers_are_throttled(manager, needle):
    """If the loop cannot be prevented it must at least be paced: one attempt a
    minute instead of twelve turns 24,324 requests a day into ~1,440."""
    assert needle in _emit(manager)
