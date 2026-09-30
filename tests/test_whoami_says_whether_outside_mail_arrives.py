"""#72: `whoami` shows whether mail from outside the bus reaches this address.

On 2026-09-24 two teams read an agent's address — and a QR captioned "scan to
mail X directly" — as a promise that outside mail would arrive, on a workspace
that refuses it. agentbus-8dc08d now serves workspace.external_mail in
/v1/whoami (their #354). Live on rodmena-test-02 it reads:

    {"accepts": "nobody", "refusal_reason": "encrypted_workspace",
     "refused_mail": "retained on the undeliverable surface, never bounced"}

A null policy means UNKNOWN, not open. An older server that sends no field gets
no line at all — the same convention as persona — rather than a guess.
"""

from __future__ import annotations

import argparse

import pytest

from agentbus_client import cli as cli_module
from agentbus_client.cli import _directory

LIVE_REFUSED = {
    "accepts": "nobody",
    "refusal_reason": "encrypted_workspace",
    "refused_mail": "retained on the undeliverable surface, never bounced",
}


def _whoami(monkeypatch, capsys, workspace: dict, qr: bool = False) -> str:
    class _Bus:
        agent = "me"

        def whoami(self, agent=None):
            return {
                "workspace": {"slug": "ws", **workspace},
                "agent": {"name": "me"},
                "address": "agentbus+me.x@mail.rodmena.co.uk",
                "unread": {},
            }

    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: _Bus())
    monkeypatch.setattr(_directory, "_print_qr", lambda payload: True)
    cli_module.cmd_whoami(
        argparse.Namespace(json=False, qr=qr, agent=None, api_key=None, base_url=None)
    )
    return capsys.readouterr().out


def test_a_refusing_workspace_says_so_next_to_the_address(monkeypatch, capsys):
    out = _whoami(monkeypatch, capsys, {"external_mail": LIVE_REFUSED})
    assert "external:  REFUSED — this workspace is encrypted" in out
    assert "refused mail is retained on the undeliverable surface, never bounced" in out
    assert out.index("address:") < out.index("external:")


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        ({"accepts": "anyone"}, "anyone can mail this address"),
        ({"accepts": "contacts"}, "only this workspace's contacts can mail this address"),
        ({"accepts": "nobody", "refusal_reason": "ingress_closed"}, "ingress is closed"),
        ({"accepts": "nobody", "refusal_reason": "something_new"}, "REFUSED — something_new"),
    ],
)
def test_each_policy_renders_as_itself(monkeypatch, capsys, policy, expected):
    assert expected in _whoami(monkeypatch, capsys, {"external_mail": policy})


@pytest.mark.parametrize("policy", [None, {}, {"accepts": None}, {"accepts": "later-value"}])
def test_an_unknown_policy_is_unknown_not_open(monkeypatch, capsys, policy):
    """null means the server could not say. Rendering nothing — or 'open' — would
    be the 09-24 trap again."""
    out = _whoami(monkeypatch, capsys, {"external_mail": policy})
    assert "external:  UNKNOWN" in out
    assert "anyone can mail" not in out


def test_an_older_server_that_sends_no_field_gets_no_line(monkeypatch, capsys):
    """Known-negative, and the persona convention: absent field, no guess."""
    out = _whoami(monkeypatch, capsys, {})
    assert "external:" not in out


def test_the_qr_caption_does_not_invite_mail_that_will_be_refused(monkeypatch, capsys):
    out = _whoami(monkeypatch, capsys, {"external_mail": LIVE_REFUSED}, qr=True)
    assert "scan to mail me directly" not in out
    assert "mail from outside is REFUSED here" in out


def test_the_qr_caption_is_unchanged_where_outside_mail_is_accepted(monkeypatch, capsys):
    """Known-positive for the test above: the old caption still appears where true."""
    out = _whoami(monkeypatch, capsys, {"external_mail": {"accepts": "anyone"}}, qr=True)
    assert "scan to mail me directly" in out
