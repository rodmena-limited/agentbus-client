"""F12 (issuedb #10): `agentbus send-batch` — bulk send over one process.

Peer report (agentbus-ui-c760a1, batch #3 finding #12): the per-
invocation `agentbus send` capped throughput at ~1.6 sends/s because
process startup + config load + key open + sealing setup dominated. The
batch mode fixes that: it pays startup ONCE and reuses the sealing
context + httpx keep-alive.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentbus_client import cli
from agentbus_client.client import AgentBusError


class _BatchBus:
    def __init__(self, responses=None, errors=None):
        self._responses = list(responses or [])
        self._errors = list(errors or [])
        self.calls: list[dict] = []
        self.agent = "test-agent"

    def send(self, to, **kw):
        self.calls.append({"to": to, **kw})
        if self._errors and self._errors[0] is not None:
            err = self._errors.pop(0)
            raise err
        if self._errors:
            self._errors.pop(0)
        if self._responses:
            return self._responses.pop(0)
        return {
            "id": f"01M{len(self.calls):03d}",
            "delivery_count": 1,
            "thread_id": "01T",
            "cc": [],
        }


def _args(**over):
    base = {"agent": None, "json": False, "stop_on_error": False}
    base.update(over)
    return argparse.Namespace(**base)


def _pipe_stdin(monkeypatch, text: str) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    # Force isatty to False since StringIO doesn't implement it in a way that
    # `stream.isatty()` returns False consistently — patch explicitly.
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)


# --------------------------------------------------------------- happy path


def test_batch_of_three_sends_all_and_returns_zero(monkeypatch, capsys):
    bus = _BatchBus()
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    _pipe_stdin(
        monkeypatch,
        "\n".join(
            [
                json.dumps({"to": "a", "subject": "s1", "text": "one"}),
                json.dumps({"to": ["b"], "subject": "s2", "text": "two"}),
                json.dumps({"to": ["c", "d"], "subject": "s3", "text": "three"}),
            ]
        ),
    )
    assert cli.cmd_send_batch(_args()) == 0
    # Three send calls in order.
    assert [c["to"] for c in bus.calls] == ["a", ["b"], ["c", "d"]]
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 3
    assert [line["index"] for line in lines] == [0, 1, 2]
    assert all(line["ok"] for line in lines)


def test_bus_is_instantiated_ONCE_across_the_whole_batch(monkeypatch, capsys):
    """The point of the whole feature: startup pays once."""
    bus = _BatchBus()
    instantiations = 0

    def counting_bus(_args):
        nonlocal instantiations
        instantiations += 1
        return bus

    monkeypatch.setattr(cli._common, "_bus", counting_bus)
    _pipe_stdin(
        monkeypatch,
        "\n".join([json.dumps({"to": "a", "subject": "s", "text": "hi"}) for _ in range(5)]),
    )
    cli.cmd_send_batch(_args())
    assert instantiations == 1, f"bus was instantiated {instantiations} times; must be 1"


# --------------------------------------------------------------- failures


def test_a_failed_send_does_not_stop_the_batch(monkeypatch, capsys):
    bus = _BatchBus(errors=[None, AgentBusError("server said nope"), None])
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    _pipe_stdin(
        monkeypatch,
        "\n".join(
            [
                json.dumps({"to": "a", "subject": "s1", "text": "one"}),
                json.dumps({"to": "b", "subject": "s2", "text": "two"}),
                json.dumps({"to": "c", "subject": "s3", "text": "three"}),
            ]
        ),
    )
    # Batch exits non-zero because there was an error, but every line was tried.
    assert cli.cmd_send_batch(_args()) == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 3
    assert [line["ok"] for line in lines] == [True, False, True]
    assert lines[1]["error"]["type"] == "AgentBusError"
    assert "server said nope" in lines[1]["error"]["message"]


def test_stop_on_error_stops_immediately(monkeypatch, capsys):
    bus = _BatchBus(errors=[None, AgentBusError("fail"), None])
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    _pipe_stdin(
        monkeypatch,
        "\n".join(
            [
                json.dumps({"to": "a", "subject": "s1"}),
                json.dumps({"to": "b", "subject": "s2"}),
                json.dumps({"to": "c", "subject": "s3"}),  # never reached
            ]
        ),
    )
    assert cli.cmd_send_batch(_args(stop_on_error=True)) == 1
    # Bus.send was called for lines 0 and 1 only.
    assert len(bus.calls) == 2


def test_malformed_json_line_is_reported_not_crashed(monkeypatch, capsys):
    bus = _BatchBus()
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    _pipe_stdin(
        monkeypatch,
        "\n".join(
            [
                json.dumps({"to": "a", "subject": "ok"}),
                "not-json-{",  # malformed
                json.dumps({"to": "c", "subject": "ok"}),
            ]
        ),
    )
    assert cli.cmd_send_batch(_args()) == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[1]["ok"] is False
    assert lines[1]["error"]["type"] == "input_parse_error"


def test_missing_to_field_is_reported(monkeypatch, capsys):
    bus = _BatchBus()
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    _pipe_stdin(monkeypatch, json.dumps({"subject": "no-to", "text": "hi"}))
    assert cli.cmd_send_batch(_args()) == 1
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[0]["error"]["type"] == "missing_to"


def test_blank_lines_are_tolerated(monkeypatch, capsys):
    bus = _BatchBus()
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    _pipe_stdin(
        monkeypatch,
        json.dumps({"to": "a", "subject": "s"})
        + "\n\n\n"
        + json.dumps({"to": "b", "subject": "s"}),
    )
    assert cli.cmd_send_batch(_args()) == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 2


def test_empty_stdin_prints_usage_hint_and_exits_two(monkeypatch, capsys):
    bus = _BatchBus()
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    _pipe_stdin(monkeypatch, "")
    assert cli.cmd_send_batch(_args()) == 2
    assert "no input on stdin" in capsys.readouterr().err


# --------------------------------------------------------------- shape


def test_fire_and_forget_response_is_normalised_per_line(monkeypatch, capsys):
    """Same F13 rule as cmd_send — fire_and_forget must never emit {}."""
    bus = _BatchBus(responses=[{}, {"reached": 2}])
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    _pipe_stdin(
        monkeypatch,
        "\n".join(
            [
                json.dumps({"to": "a", "subject": "s", "guarantee": "fire_and_forget"}),
                json.dumps({"to": "b", "subject": "s", "guarantee": "fire_and_forget"}),
            ]
        ),
    )
    cli.cmd_send_batch(_args())
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[0]["result"]["status"] == "accepted"
    assert lines[1]["result"]["status"] == "accepted"
    assert lines[1]["result"]["reached"] == 2
