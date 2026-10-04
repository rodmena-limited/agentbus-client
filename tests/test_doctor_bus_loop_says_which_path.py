from __future__ import annotations

import argparse
from types import SimpleNamespace
from typing import Any

import pytest

from agentbus_client import onboarding, sealing
from agentbus_client.cli import _common, _diag


class _Bus:
    base_url = "https://bus.doctor.test"
    agent = "doctor-agent-1"

    def __init__(self, stored_body: str | None) -> None:
        self._stored = stored_body
        self.acked: list[str] = []
        self._sent = False

    def whoami(self) -> dict[str, Any]:
        return {"workspace": {"slug": "ws"}}

    def usage(self) -> dict[str, Any]:
        return {"policies": []}

    def inbox(self, cursor: int, limit: int = 200) -> list[Any]:
        if not self._sent:
            return []
        return [SimpleNamespace(message_id="m-1", delivery_id="d-1", seq=1)]

    def send(self, to: list[str], subject: str = "", text: str = "") -> dict[str, str]:
        self._sent = True
        return {"id": "m-1"}

    def read(self, delivery_id: str, raw: bool = False) -> dict[str, Any]:
        if raw:
            if self._stored is None:
                raise RuntimeError("raw read failed")
            return {"text_body": self._stored}
        return {"text_body": "self-test"}

    def ack(self, delivery_id: str) -> None:
        self.acked.append(delivery_id)


def _doctor(monkeypatch: pytest.MonkeyPatch, bus: _Bus) -> str:
    monkeypatch.setattr(_common, "_bus", lambda _a: bus)
    monkeypatch.setattr(onboarding, "doctor_credential_scope", lambda base_url=None: [])
    monkeypatch.setattr(onboarding, "skill_state", lambda base_url=None: ("current", "ok"))
    lines: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: lines.append(" ".join(map(str, a))))
    _diag.cmd_doctor(argparse.Namespace(wake=False, agent=None))
    return "\n".join(lines)


def test_a_sealed_self_test_is_reported_as_in_band(monkeypatch: pytest.MonkeyPatch) -> None:
    _private, public = sealing.ensure_keypair("doctor-agent-1")
    out = _doctor(monkeypatch, _Bus(sealing.seal_for("self-test", [public])))
    assert "bus loop:       OK (arrived and READABLE in" in out
    assert "in-band, sealed: it never left the bus" in out
    assert "does not show that email from outside the bus arrives" in out
    assert "smtp" not in out.lower()


def test_an_unsealed_self_test_is_reported_as_the_mail_api_relay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _doctor(monkeypatch, _Bus("self-test"))
    assert "via the mail-api relay, not public email ingress" in out
    assert "smtp" not in out.lower()


def test_an_undetermined_path_is_said_so_not_guessed(monkeypatch: pytest.MonkeyPatch) -> None:
    out = _doctor(monkeypatch, _Bus(None))
    assert "path not determined" in out
    assert "in-band" not in out
    assert "relay" not in out
