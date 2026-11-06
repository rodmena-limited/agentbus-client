from __future__ import annotations

import argparse
import io
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, NoReturn

import pytest

from agentbus_client.cli import _common, _keys, _watch_status, main


def _declare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str = "alpha") -> Path:
    project = tmp_path / "proj"
    (project / ".agentbus").mkdir(parents=True)
    (project / ".agentbus" / "agent").write_text(name + "\n")
    monkeypatch.chdir(project)
    return project


def _args(**over: Any) -> argparse.Namespace:
    base = {"agent": None, "api_key": None, "base_url": None, "json": False}
    base.update(over)
    return argparse.Namespace(**base)


def _run(argv: list[str]) -> tuple[object, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc: object = main(argv)
        except SystemExit as exc:
            rc = exc.code
    return rc, out.getvalue(), err.getvalue()


def _refuse_network(args: Any) -> NoReturn:
    msg = "resolving a declared identity must not build a client"
    raise AssertionError(msg)


def _record_pids(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []

    def fake(agent: str) -> list[Any]:
        seen.append(agent)
        return []

    monkeypatch.setattr(_watch_status, "_watch_pids", fake)
    return seen


class _BoundBus:
    agent = None

    def __init__(self) -> None:
        self.asked = 0
        self.target: str | None = None
        self.paths: list[str] = []

    def whoami(self, agent: Any = None) -> dict[str, Any]:
        self.asked += 1
        return {"agent": {"name": "bound-one"}}

    def health(self, target: Any, agent: Any = None) -> dict[str, Any]:
        self.target = target
        return {"agent": target, "wake_channel_state": "live"}

    def _request(self, method: Any, path: Any, **kw: Any) -> dict[str, Any]:
        self.paths.append(path)
        return {"retired": True}


@pytest.mark.parametrize("verb", ["watch-status", "watch-stop"])
def test_watch_verbs_act_as_the_checkouts_declared_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verb: Any
) -> None:
    _declare(tmp_path, monkeypatch)
    monkeypatch.setattr(_common, "_bus", _refuse_network)
    seen = _record_pids(monkeypatch)
    rc, _out, err = _run([verb])
    assert rc != 2, err
    assert "no acting agent" not in err
    assert seen
    assert set(seen) == {"alpha"}


def test_service_emits_a_unit_for_the_declared_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(tmp_path, monkeypatch)
    monkeypatch.setattr(_common, "_bus", _refuse_network)
    rc, out, err = _run(["service", "--manager", "systemd"])
    assert rc == 0, err
    assert "alpha" in out


def test_an_explicit_agent_outranks_the_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(tmp_path, monkeypatch)
    seen = _record_pids(monkeypatch)
    rc, _out, err = _run(["watch-status", "--agent", "beta"])
    assert rc != 2, err
    assert set(seen) == {"beta"}


def test_nothing_declared_is_one_short_refusal_naming_the_fix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    seen = _record_pids(monkeypatch)
    rc, _out, err = _run(["watch-status"])
    assert rc == 2
    assert seen == []
    assert "--agent" in err
    assert "agentbus setup" in err
    assert len(err.strip().splitlines()) <= 6


def test_the_resolver_agrees_with_the_client_whoami_builds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(tmp_path, monkeypatch)
    keys = Path(os.environ["AGENTBUS_CONFIG_DIR"]) / "keys"
    keys.mkdir(parents=True)
    (keys / "alpha.env").write_text("export AGENTBUS_API_KEY=ab_sk_test_alpha\n")
    assert _common._bus(_args()).agent == "alpha"
    assert _common.acting_agent(_args()) == "alpha"


def test_a_key_with_nothing_declared_asks_the_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENTBUS_API_KEY", "ab_sk_test_bound")
    bus = _BoundBus()
    monkeypatch.setattr(_common, "_bus", lambda args: bus)
    assert _common.acting_agent(_args()) == "bound-one"
    assert bus.asked == 1


def test_the_server_answer_is_not_used_when_the_agent_is_known_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(tmp_path, monkeypatch)
    bus = _BoundBus()
    monkeypatch.setattr(_common, "_bus", lambda args: bus)
    assert _common.acting_agent(_args()) == "alpha"
    assert bus.asked == 0


def test_keys_uses_the_agent_whoami_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENTBUS_API_KEY", "ab_sk_test_bound")
    bus = _BoundBus()
    monkeypatch.setattr(_common, "_bus", lambda args: bus)
    seen: list[str] = []

    def _record(b: Any, a: Any, agent: str, fp: Any) -> int:
        seen.append(agent)
        return 0

    monkeypatch.setattr(_keys, "_keys_list", _record)
    monkeypatch.setattr(_keys, "_this_machines_fingerprint", lambda: None)
    rc, _out, err = _run(["keys", "list"])
    assert rc == 0, err
    assert seen == ["bound-one"]


def test_health_defaults_to_the_agent_whoami_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGENTBUS_API_KEY", "ab_sk_test_bound")
    bus = _BoundBus()
    monkeypatch.setattr(_common, "_bus", lambda args: bus)
    rc, _out, err = _run(["health", "--json"])
    assert rc == 0, err
    assert bus.target == "bound-one"


def test_retire_without_a_name_retires_the_declared_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(tmp_path, monkeypatch)
    bus = _BoundBus()
    monkeypatch.setattr(_common, "_bus", lambda args: bus)
    monkeypatch.setattr(_common, "_key_for_agent", lambda name: None)
    rc, _out, err = _run(["retire"])
    assert rc == 0, err
    assert bus.paths == ["/v1/agents/alpha/retire"]


def test_block_refuses_a_self_block_from_the_declaration_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _declare(tmp_path, monkeypatch)
    monkeypatch.setattr(_common, "_bus", _refuse_network)
    rc, _out, err = _run(["block", "alpha"])
    assert rc == 2
    assert "refusing to block yourself (alpha)" in err
