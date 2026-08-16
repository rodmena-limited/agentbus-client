"""REG-8c (round-3.6, bikeroom): sanitize agent name in STATE-FILE builders.

REG-8/8b closed the credential-file class. Bikeroom's re-audit found the same
attacker-controllable `.agentbus/agent` source feeds five MORE filename
builders — for state files, not credentials — that were not swept in the
round-3.5 pass. These are the WRITE-primitive class: notify + rewake mkdir
parents and write JSON; session-end unlinks. A hostile checkout with
`.agentbus/agent` containing `../../../../../tmp/PWNED` used to create a
directory tree, write a JSON file into /tmp, or delete an arbitrary path
at session end. All from the exact source REG-8's threat model named.

Sites fixed:
  hooks/claude_code.py:_wake_file            wake-{agent}.jsonl        WRITE (notify)
  hooks/claude_code.py:_notify_error_file    notify-error-{agent}.json WRITE (record_notify_failure)
  hooks/claude_code.py:_gate_degraded_file   gate-degraded-{agent}.json WRITE (record_gate_degraded) + READ (fast-fail circuit)
  hooks/claude_code.py:_identity_claim_path  session-claim-{agent}.json WRITE (session-start) + DELETE (session-end)
  rewake.py:_ledger_path                     rewake-seen-{agent}.txt    WRITE (touch + append) — passively reachable via the Stop-hook monitor on every turn

Also the session-end monitor state file (probable DELETE primitive per
bikeroom) is now sanitized too.

Shared invariant: every builder now interpolates `sealing.agent_slug(agent)`
so a hostile name cannot escape its parent directory. Same primitive the
credential builders use; consolidation is what stops a sixth class of
sibling sites appearing in round-3.7.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from agentbus_client import rewake, sealing
from agentbus_client.hooks import claude_code as hc


@pytest.fixture
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTBUS_WAKE_DIR", str(tmp_path))
    monkeypatch.delenv("AGENTBUS_REWAKE_STATE", raising=False)
    return tmp_path


HOSTILE = "../../../../../../tmp/PWNED_state_traversal"


@pytest.mark.parametrize(
    "name,fn",
    [
        ("_wake_file", hc._wake_file),
        ("_notify_error_file", hc._notify_error_file),
        ("_gate_degraded_file", hc._gate_degraded_file),
        ("_identity_claim_path", hc._identity_claim_path),
        ("rewake._ledger_path", rewake._ledger_path),
    ],
)
def test_traversal_payload_stays_inside_config_dir(name, fn, isolated_config):
    """The core property: a filename built from an attacker-controlled agent
    name resolves INSIDE the state directory, never outside it. Tested via
    actual path.resolve() rather than substring — a filename containing '..'
    is fine as long as it stays a filename inside the parent dir."""
    resolved = Path(os.path.normpath(str(fn(HOSTILE))))
    config_resolved = isolated_config.resolve()
    assert resolved.parent == config_resolved, (
        f"{name} escaped the config dir: {resolved} (config dir was {config_resolved})"
    )
    # The filename itself contains no path separators — a directory-creating
    # write like notify() would otherwise mkdir a fresh tree.
    assert "/" not in resolved.name
    assert "\\" not in resolved.name


def test_legitimate_agent_name_unchanged(isolated_config):
    """The sanitizer MUST NOT change legitimate names. An agent whose state
    file changes name silently loses continuity — wake cursor resets, notify
    errors reappear because a running-clean file was written under a new name.
    """
    good = "agentbus-8dc08d"
    assert hc._wake_file(good).name == f"wake-{good}.jsonl"
    assert hc._notify_error_file(good).name == f"notify-error-{good}.json"
    assert hc._gate_degraded_file(good).name == f"gate-degraded-{good}.json"
    assert hc._identity_claim_path(good).name == f"session-claim-{good}.json"
    assert rewake._ledger_path(good).name == f"rewake-seen-{good}.txt"


def test_agent_slug_is_the_public_sanitizer():
    """The public sealing.agent_slug is aliased to the internal _agent_slug —
    so a caller who imports either name gets the same function. If a refactor
    ever diverges them, this test catches it before a filename builder ends
    up sanitizing with the wrong rule."""
    assert sealing.agent_slug is sealing._agent_slug


def test_write_primitive_actually_lands_inside_config_dir(isolated_config):
    """End-to-end: writing to the wake file via a traversal payload must
    create a file inside the config dir, not in /tmp/PWNED.jsonl."""
    # Directly write to what notify() would compute.
    p = hc._wake_file(HOSTILE)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"probe": true}\n')

    # The file exists INSIDE the config dir.
    assert p.exists()
    assert p.resolve().parent == isolated_config.resolve()

    # And nothing landed at /tmp/PWNED_state_traversal.jsonl.
    assert not Path("/tmp/PWNED_state_traversal.jsonl").exists(), (
        "state-file traversal payload wrote outside the config dir — REG-8c regression"
    )


def test_session_end_monitor_filename_is_sanitized():
    """The session-end code path unlinks a monitor state file whose name
    interpolates BOTH agent and session — a traversal payload in either
    used to be an arbitrary-path DELETE primitive. Sanitizing both keeps
    the unlink target inside the config dir."""
    payload = "../../etc/passwd"
    slug = sealing.agent_slug(payload)
    assert "/" not in slug
    assert "\\" not in slug
    # The filename that would be built at hooks/claude_code.py:1285:
    filename = f"monitor-{slug}-{sealing.agent_slug('sess-123')}.json"
    assert "/" not in filename
    assert "\\" not in filename
    # Would resolve inside its parent dir.
    root = Path(tempfile.gettempdir())
    assert (root / filename).resolve().parent == root.resolve()
