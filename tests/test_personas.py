"""Personas client surface (SPECS/0021).

Three-way agreed design (client + backend + ui, thread
01M093JGVWQ2HXT3G6KJRG40T3). Farshid chose POLICY: the server enforces
the vocabulary and admin authority is required to assign a lane.

Backend shipped the column (commit e8f9cf7, migration
01M0982RBKD6M7J9ESTGYWA5RK). These tests pin the client surface that
consumes it.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

from agentbus_client import cli as cli_module
from agentbus_client.client import AgentBus, AsyncAgentBus

if TYPE_CHECKING:
    import pytest

# --------------------------------------------------------------- SDK register


def test_sdk_register_passes_persona_in_payload() -> None:
    bus = AgentBus(api_key="ab_sk_test_test", agent="me")
    captured: dict[str, Any] = {}

    def fake_request(method: Any, path: Any, json: Any = None, **kw: Any) -> dict[str, Any]:
        captured.update(json or {})
        return {"agent": {"name": "test-agent"}}

    with patch.object(bus, "_request", side_effect=fake_request):
        bus.register("test-agent", persona="backend")

    assert captured.get("persona") == "backend"


def test_sdk_register_omits_persona_when_not_provided() -> None:
    """Absence is not the same as empty string. A None persona must NOT
    appear in the payload at all — the server treats a missing field and
    an empty field differently (missing = 'don't change', empty = 'clear
    it')."""
    bus = AgentBus(api_key="ab_sk_test_test", agent="me")
    captured: dict[str, Any] = {}

    def fake_request(method: Any, path: Any, json: Any = None, **kw: Any) -> dict[str, Any]:
        captured.update(json or {})
        return {"agent": {"name": "test-agent"}}

    with (
        patch.object(bus, "_request", side_async=fake_request)
        if False
        else patch.object(bus, "_request", side_effect=fake_request)
    ):
        bus.register("test-agent")

    assert "persona" not in captured


async def test_async_register_passes_persona() -> None:
    bus = AsyncAgentBus(api_key="ab_sk_test_test", agent="me")
    captured: dict[str, Any] = {}

    async def fake_request(method: Any, path: Any, json: Any = None, **kw: Any) -> dict[str, Any]:
        captured.update(json or {})
        return {"agent": {"name": "test-agent"}}

    with patch.object(bus, "_request", side_effect=fake_request):
        await bus.register("test-agent", persona="legal")
    assert captured.get("persona") == "legal"


# --------------------------------------------------------------- CLI register


def _register_args(**over: Any) -> argparse.Namespace:
    base: dict[str, Any] = {
        "name": None,
        "label": None,
        "role": None,
        "workdir": None,
        "ephemeral": False,
        "repo_remote": None,
        "capability": [],
        "unlisted": False,
        "persona": None,
        "agent": None,
        "json": False,
        "api_key": None,
        "base_url": None,
    }
    base.update(over)
    return argparse.Namespace(**base)


def test_cli_register_argparse_has_persona_flag() -> None:
    """The --persona flag is registered on the register subparser.
    Without this, the flag is unreachable from the CLI even though the
    SDK accepts it."""
    src = _cli_source()
    assert '"--persona"' in src
    assert 'metavar="LANE"' in src or "metavar='LANE'" in src


def test_cli_setup_argparse_has_persona_flag() -> None:
    """Same for the setup subparser — the primary onboarding path."""
    src = _cli_source()
    # setup's --persona is in a different block from register's
    assert src.count('"--persona"') >= 2


# --------------------------------------------------------------- whoami display


def test_whoami_displays_persona_when_present(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _Bus:
        def whoami(self, agent: Any = None) -> dict[str, Any]:
            return {
                "workspace": {"slug": "test-ws"},
                "agent": {"name": "me", "persona": "backend"},
                "address": "me@x",
                "unread": {},
            }

    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: _Bus())
    cli_module.cmd_whoami(
        argparse.Namespace(json=False, qr=False, agent=None, api_key=None, base_url=None)
    )
    out = capsys.readouterr().out
    assert "persona:   backend" in out


def test_whoami_silent_when_persona_absent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Forward-compatible: old server returns no persona field. The line
    must not appear so the output is byte-identical to pre-persona."""

    class _Bus:
        def whoami(self, agent: Any = None) -> dict[str, Any]:
            return {
                "workspace": {"slug": "test-ws"},
                "agent": {"name": "me"},
                "address": "me@x",
                "unread": {},
            }

    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: _Bus())
    cli_module.cmd_whoami(
        argparse.Namespace(json=False, qr=False, agent=None, api_key=None, base_url=None)
    )
    out = capsys.readouterr().out
    assert "persona" not in out


# --------------------------------------------------------------- phonebook display


def test_phonebook_shows_persona_column_when_any_agent_has_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class _Bus:
        def phonebook(self, *a: Any, **kw: Any) -> list[Any]:
            return [
                {
                    "name": "alice",
                    "presence": "responsive",
                    "address": "a@x",
                    "capabilities": [],
                    "labels": {},
                    "persona": "backend",
                },
                {
                    "name": "bob",
                    "presence": "idle",
                    "address": "b@x",
                    "capabilities": [],
                    "labels": {},
                    "persona": None,
                },
            ]

    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: _Bus())
    cli_module.cmd_phonebook(
        argparse.Namespace(
            query=None,
            capability=None,
            label=None,
            json=False,
            agent=None,
            api_key=None,
            base_url=None,
        )
    )
    out = capsys.readouterr().out
    assert "backend" in out
    # Bob has no persona — shows '-' not empty.
    lines = [line for line in out.splitlines() if "bob" in line.lower()]
    assert any("-" in line for line in lines)


def test_phonebook_no_persona_column_when_nobody_has_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Forward-compatible: old server returns no persona for anyone. The
    column must not appear, so the layout is byte-identical to pre-persona."""

    class _Bus:
        def phonebook(self, *a: Any, **kw: Any) -> list[Any]:
            return [
                {
                    "name": "alice",
                    "presence": "responsive",
                    "address": "a@x",
                    "capabilities": [],
                    "labels": {},
                    "persona": None,
                },
            ]

    monkeypatch.setattr(cli_module._common, "_bus", lambda _a: _Bus())
    cli_module.cmd_phonebook(
        argparse.Namespace(
            query=None,
            capability=None,
            label=None,
            json=False,
            agent=None,
            api_key=None,
            base_url=None,
        )
    )
    out = capsys.readouterr().out
    # The persona column header would show "backend" or similar; its absence
    # means no column was added. Verify by checking the line doesn't have
    # a trailing persona field (the address should be the last significant field).
    assert "persona" not in out.lower()


def _cli_source() -> str:
    """The CLI is a package now (one module per command family): read all of it."""
    from pathlib import Path

    return "".join(f.read_text() for f in sorted(Path(cli_module.__file__).parent.glob("*.py")))
