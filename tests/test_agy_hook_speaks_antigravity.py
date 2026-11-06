"""#56: the Antigravity hooks must speak Antigravity, not Claude Code.

THE TRAP THIS FILE EXISTS TO PIN. Claude Code re-wakes a session on EXIT CODE 2
with the mail on stdout. Antigravity ignores exit codes entirely and re-wakes on
`{"decision": "continue"}` in stdout JSON. A handler ported by copying the Claude
one would exit 2, print prose, and do NOTHING — and would look completely healthy
from our side, because the hook ran, found mail, and reported success. The only
observable difference is a session that never wakes.

The second trap is worse, and it is why `test_the_same_delivery_does_not_wake_twice`
is here: `{"decision": "continue"}` RE-ENTERS THE AGENT LOOP. On Claude an
unclaimed re-wake duplicates a notification. On Antigravity it spins the model
against the same unread message forever, with no human in the room, burning quota.
Unread-but-unacked mail is a permanent wake source, so the ledger is the only
thing standing between this feature and an unbounded loop.
"""

from __future__ import annotations

import io
import json
import sys
from contextlib import redirect_stdout
from typing import TYPE_CHECKING, Any, NoReturn, cast

import pytest

from agentbus_client.hooks import _antigravity
from agentbus_client.onboarding import _paths

if TYPE_CHECKING:
    from pathlib import Path


def _run(
    handler: Any, payload: dict[str, Any] | None, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """Drive one hook with a payload on stdin; return the parsed stdout JSON."""
    raw = "" if payload is None else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = handler(None)
    assert rc == 0, "an Antigravity hook must always exit 0; it blocks the agent loop"
    out = buf.getvalue()
    assert out, "a hook must always print one JSON object"
    return cast("dict[str, Any]", json.loads(out))


@pytest.fixture
def wired(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A checkout declaring agent `alpha`, with a credential already in env."""
    repo = tmp_path / "repo"
    (repo / ".agentbus").mkdir(parents=True)
    (repo / ".agentbus" / "agent").write_text("alpha\n")
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(tmp_path / "cfg"))
    _paths.agy_mark_wired(repo)
    monkeypatch.setenv("AGENTBUS_API_KEY", "ab_sk_test_key")
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)
    return repo


def test_mail_produces_decision_continue_not_exit_2(
    wired: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE REGRESSION. A Claude-shaped port would exit 2 and print prose."""
    monkeypatch.setattr(
        _antigravity,
        "poll_for_fresh_mail",
        lambda *a, **k: "from peer: deploy is red",
        raising=False,
    )
    from agentbus_client import rewake

    monkeypatch.setattr(rewake, "poll_for_fresh_mail", lambda *a, **k: "from peer: deploy is red")
    out = _run(_antigravity.agy_stop, {"workspacePaths": [str(wired)]}, monkeypatch)
    assert out["decision"] == "continue"
    assert "deploy is red" in out["reason"]


def test_no_mail_lets_the_agent_stop(wired: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other direction — without it, 'continue' proves nothing."""
    from agentbus_client import rewake

    monkeypatch.setattr(rewake, "poll_for_fresh_mail", lambda *a, **k: None)
    out = _run(_antigravity.agy_stop, {"workspacePaths": [str(wired)]}, monkeypatch)
    assert out["decision"] != "continue"


def test_the_same_delivery_does_not_wake_twice(
    wired: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The unbounded-loop guard, driven through the REAL flock'd ledger.

    Not a fake: the whole point is that the ledger claims, so a stubbed claim
    would test nothing. `AGENTBUS_REWAKE_STATE` keeps the production ledger out
    of it, exactly as `doctor --wake` does.
    """
    monkeypatch.setenv("AGENTBUS_REWAKE_STATE", str(tmp_path / "ledger.txt"))
    monkeypatch.setenv("AGENTBUS_REWAKE_WINDOW", "0")
    from agentbus_client import rewake

    mail = "agentbus show 01M1FD58GE4KG6A8FNE7DC3WVP\nsubject: wake me"
    monkeypatch.setattr(rewake, "_build_resilient_poll", lambda agent, wait: lambda: mail)

    first = _run(_antigravity.agy_stop, {"workspacePaths": [str(wired)]}, monkeypatch)
    assert first["decision"] == "continue", "first sight of a delivery must wake"

    second = _run(_antigravity.agy_stop, {"workspacePaths": [str(wired)]}, monkeypatch)
    assert second["decision"] != "continue", (
        "the SAME delivery woke the session twice — on Antigravity that is an "
        "unbounded model loop, not a duplicate notification"
    )


def test_a_genuinely_new_delivery_still_wakes(
    wired: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """KNOWN-POSITIVE TWIN for the dedupe: the ledger must not wedge shut."""
    monkeypatch.setenv("AGENTBUS_REWAKE_STATE", str(tmp_path / "ledger.txt"))
    monkeypatch.setenv("AGENTBUS_REWAKE_WINDOW", "0")
    from agentbus_client import rewake

    seen = []

    def poll(agent: str, wait: Any) -> Any:
        def _p() -> str:
            seen.append(1)
            return f"agentbus show 01M1FD58GE4KG6A8FNE7DC3WV{len(seen)}\nsubject: n"

        return _p

    monkeypatch.setattr(rewake, "_build_resilient_poll", poll)
    a = _run(_antigravity.agy_stop, {"workspacePaths": [str(wired)]}, monkeypatch)
    b = _run(_antigravity.agy_stop, {"workspacePaths": [str(wired)]}, monkeypatch)
    assert a["decision"] == "continue"
    assert b["decision"] == "continue"


def test_catchup_injects_an_ephemeral_message_never_a_user_message(
    wired: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`userMessage` reads as something a human typed — the #91 class of bug."""
    from agentbus_client import rewake

    monkeypatch.setattr(rewake, "poll_for_fresh_mail", lambda *a, **k: "peer: ping")
    out = _run(_antigravity.agy_preinvocation, {"workspacePaths": [str(wired)]}, monkeypatch)
    step = out["injectSteps"][0]
    assert "ephemeralMessage" in step
    assert "userMessage" not in step
    assert "ping" in step["ephemeralMessage"]


def test_catchup_is_silent_with_no_mail(wired: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from agentbus_client import rewake

    monkeypatch.setattr(rewake, "poll_for_fresh_mail", lambda *a, **k: None)
    out = _run(_antigravity.agy_preinvocation, {"workspacePaths": [str(wired)]}, monkeypatch)
    assert out == {}


def test_a_bus_failure_never_holds_the_loop(wired: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """These hooks run synchronously INSIDE the agent loop. An exception here
    surfaces as a broken harness, not as a quiet AgentBus."""
    from agentbus_client import rewake

    def boom(*a: Any, **k: Any) -> NoReturn:
        msg = "bus down"
        raise RuntimeError(msg)

    monkeypatch.setattr(rewake, "poll_for_fresh_mail", boom)
    out = _run(_antigravity.agy_stop, {"workspacePaths": [str(wired)]}, monkeypatch)
    assert out["decision"] != "continue"
    out = _run(_antigravity.agy_preinvocation, {"workspacePaths": [str(wired)]}, monkeypatch)
    assert out == {}


def test_malformed_stdin_is_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """agy hands us JSON; a future version handing us something else must not
    take the session down."""
    for payload in ("", "not json at all", "[1,2,3]"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = _antigravity.agy_stop(None)
        assert rc == 0
        json.loads(buf.getvalue())


def test_no_identity_means_no_network(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """AGENTBUS_AGENT is the kill switch: an unwired project gets a silent
    no-op, and must not construct a bus at all."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)
    from agentbus_client import rewake

    def must_not_run(*a: Any, **k: Any) -> NoReturn:
        msg = "an unwired project must never reach the bus"
        raise AssertionError(msg)

    monkeypatch.setattr(rewake, "poll_for_fresh_mail", must_not_run)
    out = _run(_antigravity.agy_stop, {"workspacePaths": [str(tmp_path)]}, monkeypatch)
    assert out["decision"] != "continue"


def test_a_checkout_that_never_opted_into_agy_is_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE MACHINE-WIDE GUARD. These hooks fire in every agy session on the box,
    and `.agentbus/agent` already exists in every checkout wired for Claude Code
    or opencode. Acting on that alone would put a bus poll and a foreground Stop
    pause into a project that never asked for agy."""
    other = tmp_path / "claude-wired"
    (other / ".agentbus").mkdir(parents=True)
    (other / ".agentbus" / "agent").write_text("someone-else\n")
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)
    monkeypatch.chdir(tmp_path)

    from agentbus_client import rewake

    def must_not_run(*a: Any, **k: Any) -> NoReturn:
        msg = "a project that never opted into agy must not be polled"
        raise AssertionError(msg)

    monkeypatch.setattr(rewake, "poll_for_fresh_mail", must_not_run)
    out = _run(_antigravity.agy_stop, {"workspacePaths": [str(other)]}, monkeypatch)
    assert out["decision"] != "continue"


def test_once_opted_in_that_same_checkout_is_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """KNOWN-POSITIVE TWIN: without it, the guard above passes just as well
    against a hook that can never serve anybody."""
    other = tmp_path / "claude-wired"
    (other / ".agentbus").mkdir(parents=True)
    (other / ".agentbus" / "agent").write_text("someone-else\n")
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.setenv("AGENTBUS_API_KEY", "ab_sk_x")
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)
    monkeypatch.chdir(tmp_path)
    _paths.agy_mark_wired(other)

    from agentbus_client import rewake

    monkeypatch.setattr(rewake, "poll_for_fresh_mail", lambda *a, **k: "peer: hi")
    out = _run(_antigravity.agy_stop, {"workspacePaths": [str(other)]}, monkeypatch)
    assert out["decision"] == "continue"
