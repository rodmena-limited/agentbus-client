"""#204: watch-status named a log path whether or not a log existed.

TWO HOSTS, OPPOSITE SYMPTOMS, ONE WRONG KEY.

  bikeroom-freebsd-operato-dd8bca   pidfile present and state-keyed, NO log
                                    beside it — their watcher was started by
                                    the plugin, not `watch --daemon`, and only
                                    the daemon branch ever writes one. They
                                    could not grep for the #194 symptom, and —
                                    their words — could not even show that such
                                    a grep was CAPABLE of finding anything, so
                                    they reported NO DATA rather than absence.

  this host                         a log EXISTS, and belongs to a different,
                                    earlier daemon run. The running watcher is
                                    the monitor-state one. So watch-status named
                                    a real file that was not that watcher's.

The pidfile has been per-(agent, state) since #160; the log was not. Same
function, same disease, fixed once and missed once — the #160 comment about
absence-of-evidence sits ten lines below the offending print.

WHAT THESE TESTS PIN is the ATTRIBUTION, because that is where the remaining
honesty lives. Reporting the legacy shared path is right (a pre-existing daemon
is still writing there and calling it "no log" would be the same lie in the
other direction), but a hit there is not evidence of ownership, and saying so is
the whole point.
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentbus_client import cli


@pytest.fixture()
def runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(cli._watch_runtime, "_watch_runtime_dir", lambda create=True: tmp_path)
    return tmp_path


def test_no_log_at_all_is_reported_as_none(runtime) -> None:
    """bikeroom's case. The old code printed a path here — sending a reader to
    grep a file nothing had ever written."""
    assert cli._existing_logfile("a", "st.json.pid") is None


def test_a_state_keyed_log_is_reported_as_this_watchers(runtime) -> None:
    """THE KNOWN-POSITIVE. Without it every other case here could be satisfied
    by a function that returns None unconditionally, and the feature would be
    'watch-status never reports a log', which is not the fix."""
    (runtime / "a-st.json.pid.log").write_text("keyed")
    found = cli._existing_logfile("a", "st.json.pid")
    assert found is not None
    path, is_ours = found
    assert path.name == "a-st.json.pid.log"
    assert is_ours is True


def test_a_legacy_log_is_reported_but_NOT_claimed_as_this_watchers(runtime) -> None:
    """THIS HOST'S CASE, and the one that keeps the fix honest. The file exists
    and is worth naming; the filename cannot support the claim that it belongs
    to the state-keyed watcher being reported on."""
    (runtime / "a.log").write_text("legacy")
    found = cli._existing_logfile("a", "st.json.pid")
    assert found is not None
    path, is_ours = found
    assert path.name == "a.log"
    assert is_ours is False, "a shared path must never be claimed as this watcher's"


def test_the_state_keyed_log_wins_when_both_exist(runtime) -> None:
    (runtime / "a.log").write_text("legacy")
    (runtime / "a-st.json.pid.log").write_text("keyed")
    found = cli._existing_logfile("a", "st.json.pid")
    assert found is not None, "the keyed log exists, so a lookup must find it"
    path, is_ours = found
    assert path.name == "a-st.json.pid.log"
    assert is_ours is True


def test_a_legacy_watcher_owns_the_legacy_log(runtime) -> None:
    """A watcher with no state key IS the legacy slot, so the unkeyed log is
    genuinely its own. Flagging that one as unattributed would be a false
    warning, and false warnings are how real ones stop being read."""
    (runtime / "a.log").write_text("legacy")
    found = cli._existing_logfile("a", None)
    assert found is not None, "the legacy log exists, so a lookup must find it"
    path, is_ours = found
    assert path.name == "a.log"
    assert is_ours is True


def test_the_daemon_writes_the_state_keyed_path(runtime) -> None:
    """The read side is worthless if the write side still shares one file: two
    watchers for one agent would interleave into it, and every log would be
    unattributable by construction."""
    src = inspect.getsource(cli.cmd_watch)
    assert "_watch_logfile(agent, state_key)" in src, (
        "the daemon must key its log the same way it keys its pidfile"
    )


def test_watch_status_does_not_print_a_path_it_has_not_checked(runtime) -> None:
    """THE REGRESSION. The old line was an unconditional
    `print(f"  log: {_watch_logfile(agent)}")`."""
    src = inspect.getsource(cli.cmd_watch_status)
    assert "log: {_watch_logfile(agent)}" not in src
    assert "_existing_logfile" in src
    # both the running and the not-running branch
    assert src.count("_existing_logfile") >= 2


def test_watch_status_says_why_there_is_no_log(runtime) -> None:
    """'log: none' alone would read as a fault. The reason — only
    `watch --daemon` captures output — is what stops someone hunting a bug in
    their watcher."""
    src = inspect.getsource(cli.cmd_watch_status)
    assert "watch --daemon" in src
    assert "nothing to read" in src
