"""#49: `doctor --wake` version detection — the stale-watcher check.

INSTALLED and RUNNING are deliberately different questions. A watcher is a
long-lived process: upgrading the package on disk does NOT upgrade the process
that is already running, so a host can sit for days with a new client installed
and an old watcher still holding the stream. That gap is invisible from
`agentbus --version`, which reports the disk.

The property this file protects is the one the docstring insists on: NONE IS NOT
A PASS. A watcher too old to stamp its version, or a state file that cannot be
read, must produce "cannot confirm" — never a silent match. Reporting agreement
because nothing was measured is the vacuous-check shape this codebase keeps
finding, applied to its own upgrade path.
"""

from __future__ import annotations

import json

import pytest

from agentbus_client.onboarding import _doctor


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(_doctor, "identity_config_dir", lambda: tmp_path)
    return tmp_path


def _monitor(cfg, agent, version, name="s1"):
    p = cfg / f"monitor-{agent}-{name}.json"
    body = {"agent": agent}
    if version is not None:
        body["client_version"] = version
    p.write_text(json.dumps(body))
    return p


def test_the_running_version_is_read_from_the_watchers_own_state(cfg):
    _monitor(cfg, "a", "0.9.1")
    assert _doctor._running_watcher_version("a") == "0.9.1"


def test_no_state_file_means_cannot_tell_not_a_match(cfg):
    assert _doctor._running_watcher_version("a") is None


def test_a_watcher_too_old_to_stamp_its_version_reports_none(cfg):
    """A file that exists but carries no version is 'cannot confirm'."""
    _monitor(cfg, "a", None)
    assert _doctor._running_watcher_version("a") is None


def test_unreadable_state_is_none_rather_than_an_exception(cfg):
    (cfg / "monitor-a-s1.json").write_text("{not json")
    assert _doctor._running_watcher_version("a") is None


def test_the_NEWEST_state_file_wins(cfg):
    """A session leaves its state file behind. Reading a stale one would report
    the version of a watcher that exited days ago as if it were running."""
    old = _monitor(cfg, "a", "0.1.0", name="old")
    new = _monitor(cfg, "a", "9.9.9", name="new")
    import os

    os.utime(old, (1, 1))
    os.utime(new, (10_000_000, 10_000_000))
    assert _doctor._running_watcher_version("a") == "9.9.9"


def test_another_agents_watcher_is_not_read(cfg):
    """Known-negative: the glob must be scoped, or every agent on a shared host
    reports whichever watcher happened to be newest."""
    _monitor(cfg, "someone-else", "0.9.1")
    assert _doctor._running_watcher_version("a") is None


def test_the_installed_version_is_reported(cfg):
    v = _doctor._installed_version()
    assert v and v[0].isdigit()
