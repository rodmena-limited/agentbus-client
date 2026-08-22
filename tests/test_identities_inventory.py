"""`agentbus identities` — local credential inventory (macbook ask #5).

Thread 01M092QZXGEBD6AJ193ZKEPVZ5. A foreign CLI on a shared $HOME listed
~/.config/agentbus/keys/, picked a peer's .env, exported it, and posted as
that peer. Nothing on the box made the situation visible; the operator
learned of it from a screenshot.

This command does NOT close that hole — a bearer credential readable by
its own UID is the documented model, and any client-side guard is
bypassed by the same process that can read the file. It makes the state
OBSERVABLE, which is the half that is actually in the client's power.

The load-bearing assertion in this file is that it never prints key
material.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from agentbus_client import cli as cli_module

SECRET = "ab_sk_0c3ad1f9ebb614ac_SUPERSECRETTAILNOBODYSHOULDSEE"


def _seed(tmp_path: Path, agents: list[str]) -> Path:
    keys = tmp_path / ".config" / "agentbus" / "keys"
    keys.mkdir(parents=True, exist_ok=True)
    for a in agents:
        (keys / f"{a}.env").write_text(
            f"export AGENTBUS_API_KEY={SECRET}\nexport AGENTBUS_AGENT={a}\n"
        )
        (keys / f"{a}.env").chmod(0o600)
    return keys


def _args(**over):
    base = {"json": False, "remote": False, "agent": None}
    base.update(over)
    return argparse.Namespace(**base)


def test_never_prints_key_material(tmp_path, monkeypatch, capsys):
    """THE SECURITY-RELEVANT ASSERTION. An inventory that leaked the secret
    would be strictly worse than no inventory — it would put the
    credential into terminal scrollback and any transcript."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _seed(tmp_path, ["peer-one", "peer-two"])
    monkeypatch.setattr(
        cli_module._onboarding if hasattr(cli_module, "_onboarding") else cli_module,
        "_bus",
        lambda _a: None,
        raising=False,
    )

    cli_module.cmd_identities(_args())
    out = capsys.readouterr().out

    assert "SUPERSECRETTAILNOBODYSHOULDSEE" not in out, "the inventory leaked key material"
    assert SECRET not in out
    # The non-secret key_id half IS shown — that is what the dashboard shows.
    assert "ab_sk_0c3ad1f9ebb614ac" in out


def test_lists_every_local_identity(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _seed(tmp_path, ["alpha-111111", "beta-222222", "gamma-333333"])

    assert cli_module.cmd_identities(_args()) == 0
    out = capsys.readouterr().out
    for a in ("alpha-111111", "beta-222222", "gamma-333333"):
        assert a in out


def test_warns_when_more_than_one_identity_is_present(tmp_path, monkeypatch, capsys):
    """The whole point: make a multi-identity box legible. One identity is
    the ordinary case and must NOT nag."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _seed(tmp_path, ["only-one-11"])
    cli_module.cmd_identities(_args())
    single = capsys.readouterr().out
    assert "read/impersonate" not in single

    _seed(tmp_path, ["only-one-11", "and-another-22"])
    cli_module.cmd_identities(_args())
    multi = capsys.readouterr().out
    assert "read/impersonate" in multi
    assert "bearer-credential model, not a defect" in multi


def test_reports_which_identity_this_directory_acts_as(tmp_path, monkeypatch, capsys):
    """The question an operator staring at N key files actually has, and the
    one the directory listing cannot answer."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _seed(tmp_path, ["peer-a", "peer-b"])
    monkeypatch.setattr(
        cli_module._common, "_bus", lambda _a: pytest.fail("must not need a bus without --remote")
    )
    from agentbus_client import onboarding

    monkeypatch.setattr(onboarding, "resolve_credentials", lambda: ("k", "peer-b"))

    cli_module.cmd_identities(_args())
    assert "this directory acts as: peer-b" in capsys.readouterr().out


def test_no_credentials_is_not_an_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".config" / "agentbus" / "keys").mkdir(parents=True)
    assert cli_module.cmd_identities(_args()) == 0
    assert "no agent credentials" in capsys.readouterr().out


def test_json_output_shape(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _seed(tmp_path, ["peer-a"])
    assert cli_module.cmd_identities(_args(json=True)) == 0
    data = json.loads(capsys.readouterr().out)
    assert "identities" in data and "acting_as" in data
    assert data["identities"][0]["agent"] == "peer-a"
    # Machine consumers must not receive the secret either.
    assert SECRET not in json.dumps(data)


def test_remote_flag_surfaces_live_elsewhere(tmp_path, monkeypatch, capsys):
    """`this identity is active somewhere and it is not me` — macbook's
    point (d), the missing evidence-of-use trail."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _seed(tmp_path, ["peer-a", "peer-b"])

    class _Bus:
        agent = "peer-a"

        def health(self, target):
            return {
                "wake_channel_state": "live" if target == "peer-b" else "none",
                "watcher_alive": target == "peer-b",
                "last_seen_at": "2026-08-18T00:11:01Z",
            }

    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: _Bus())
    assert cli_module.cmd_identities(_args(remote=True)) == 0
    out = capsys.readouterr().out
    assert "WAKE" in out and "ALIVE" in out
    assert "live" in out


def test_a_corrupt_key_file_does_not_crash_the_inventory(tmp_path, monkeypatch, capsys):
    """One unreadable file must not hide the other identities — that would
    turn a diagnostic into a blind spot."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    keys = _seed(tmp_path, ["good-agent"])
    (keys / "broken.env").write_text("this is not an env file at all\n")

    assert cli_module.cmd_identities(_args()) == 0
    out = capsys.readouterr().out
    assert "good-agent" in out
    assert "broken" in out
    assert "(unreadable)" in out


# ------------------------------------------------------- device correlation


class _DevBus:
    """Phonebook + health doubles. `stray` is registered from a different
    device_hash than the acting agent — the compromise signal."""

    def __init__(self, acting="peer-a", stray=None):
        self.agent = acting
        self._stray = stray

    def phonebook(self, *a, **kw):
        rows = [{"name": "peer-a", "device_hash": "aaaa" * 16}]
        rows.append(
            {
                "name": "peer-b",
                "device_hash": ("bbbb" * 16) if self._stray == "peer-b" else ("aaaa" * 16),
            }
        )
        return rows

    def health(self, target):
        return {
            "wake_channel_state": "live",
            "watcher_alive": True,
            "last_seen_at": "2026-08-18T00:00:00Z",
        }


def test_elsewhere_fires_when_an_identity_lives_on_another_device(tmp_path, monkeypatch, capsys):
    """THE KNOWN-POSITIVE. macbook's SEV-1 asked for an evidence-of-use
    trail: 'is one of my stored identities being used somewhere I do not
    expect'. wake_channel answers 'is it live', not 'live WHERE'. This is
    the WHERE.

    Without this test the ELSEWHERE branch would be a check that has never
    gone green — and this session has repeatedly shown that such a check
    cannot be trusted to go red either."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _seed(tmp_path, ["peer-a", "peer-b"])
    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: _DevBus(stray="peer-b"))
    from agentbus_client import onboarding

    monkeypatch.setattr(onboarding, "resolve_credentials", lambda: ("k", "peer-a"))

    rc = cli_module.cmd_identities(_args(remote=True))
    out = capsys.readouterr().out

    assert "ELSEWHERE" in out, "the stray-device marker never fired"
    assert "peer-b" in out
    assert "treat the credential as compromised" in out
    assert rc == 1, "a stray identity must exit non-zero so scripts can gate on it"


def test_no_warning_when_every_identity_is_on_this_device(tmp_path, monkeypatch, capsys):
    """KNOWN-NEGATIVE. The ordinary case — several identities provisioned on
    one box, all local — must stay quiet and exit 0, or the warning becomes
    noise that gets ignored on the day it matters."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _seed(tmp_path, ["peer-a", "peer-b"])
    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: _DevBus(stray=None))
    from agentbus_client import onboarding

    monkeypatch.setattr(onboarding, "resolve_credentials", lambda: ("k", "peer-a"))

    rc = cli_module.cmd_identities(_args(remote=True))
    out = capsys.readouterr().out
    assert "ELSEWHERE" not in out
    assert "compromised" not in out
    assert rc == 0


def test_missing_device_hash_is_never_reported_as_elsewhere(tmp_path, monkeypatch, capsys):
    """Absence of data must not become an accusation. An older server, or a
    phonebook call that failed, yields no device_hash — that is 'cannot
    tell', not 'compromised'. Crying wolf on missing data is how a real
    warning gets tuned out."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _seed(tmp_path, ["peer-a", "peer-b"])

    class _NoDev(_DevBus):
        def phonebook(self, *a, **kw):
            return [{"name": "peer-a"}, {"name": "peer-b"}]  # no device_hash at all

    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: _NoDev())
    from agentbus_client import onboarding

    monkeypatch.setattr(onboarding, "resolve_credentials", lambda: ("k", "peer-a"))

    rc = cli_module.cmd_identities(_args(remote=True))
    out = capsys.readouterr().out
    assert "ELSEWHERE" not in out
    assert rc == 0
