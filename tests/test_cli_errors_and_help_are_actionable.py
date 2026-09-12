from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import click

from agentbus_client.cli import _read, _threads
from agentbus_client.cli._app import BusArgument
from agentbus_client.cli._parser import build, verbs
from agentbus_client.client import NotFoundError

SRC = str(Path(__file__).resolve().parents[1] / "src")
UNREACHABLE = {"AGENTBUS_API_KEY": "ab_sk_fake", "AGENTBUS_BASE_URL": "http://127.0.0.1:9"}


def _cli(tmp_path: Path, *argv: str, **env: str) -> subprocess.CompletedProcess[str]:
    home = tmp_path / "home"
    work = home / "work"
    work.mkdir(parents=True, exist_ok=True)
    environment = {"HOME": str(home), "PATH": "/usr/bin:/bin", "PYTHONPATH": SRC}
    environment.update(env)
    return subprocess.run(
        [sys.executable, "-m", "agentbus_client.cli", *argv],
        capture_output=True,
        text=True,
        env=environment,
        cwd=str(work),
        timeout=60,
    )


def test_no_credential_is_three_lines_naming_setup_and_signin(tmp_path):
    result = _cli(tmp_path, "inbox")
    lines = result.stderr.strip().splitlines()
    assert result.returncode == 8
    assert len(lines) <= 3, result.stderr
    assert "agentbus setup" in result.stderr
    assert "agentbus signin" in result.stderr
    assert "Traceback" not in result.stderr


def test_json_errors_are_one_json_object(tmp_path):
    result = _cli(tmp_path, "--json", "inbox")
    error = json.loads(result.stderr)["error"]
    assert result.returncode == 8
    assert error["code"] == "no_credential"
    assert error["exit_code"] == 8


def test_an_unreachable_bus_names_the_url_it_tried(tmp_path):
    result = _cli(tmp_path, "whoami", **UNREACHABLE)
    assert result.returncode == 3
    assert "http://127.0.0.1:9" in result.stderr
    assert "AGENTBUS_BASE_URL" in result.stderr
    assert len(result.stderr.strip().splitlines()) <= 3


def test_an_unreachable_bus_under_json_is_json(tmp_path):
    result = _cli(tmp_path, "inbox", "--json", **UNREACHABLE)
    assert result.returncode == 3
    assert json.loads(result.stderr)["error"]["code"] == "transport_error"


def test_malformed_input_is_a_usage_error_not_a_traceback(tmp_path):
    result = _cli(tmp_path, "remind", "-m", "hi", "--delay", "soonish", **UNREACHABLE)
    assert result.returncode == 2
    assert "Traceback" not in result.stderr
    assert "agentbus remind --help" in result.stderr
    assert "soonish" in result.stderr


def test_debug_restores_the_traceback(tmp_path):
    result = _cli(
        tmp_path, "remind", "-m", "hi", "--delay", "soonish", AGENTBUS_DEBUG="1", **UNREACHABLE
    )
    assert "Traceback" in result.stderr


def test_help_prints_the_overview(tmp_path):
    result = _cli(tmp_path, "help")
    assert result.returncode == 0
    assert "Send mail:" in result.stdout


def test_help_names_a_command_and_a_subcommand(tmp_path):
    send = _cli(tmp_path, "help", "send")
    revoke = _cli(tmp_path, "help", "keys", "revoke")
    assert send.returncode == 0 and "Usage: agentbus send" in send.stdout
    assert revoke.returncode == 0 and "Usage: agentbus keys revoke" in revoke.stdout


def test_help_for_an_unknown_word_still_suggests(tmp_path):
    result = _cli(tmp_path, "help", "cron")
    assert result.returncode == 2
    assert "remind --repeat" in result.stderr


def test_help_is_not_a_verb():
    assert "help" not in verbs()


def _all_commands():
    root = build()
    for name, command in root.commands.items():
        if isinstance(command, click.Group) and not hasattr(command, "handler"):
            for child_name, child in command.commands.items():
                yield f"{name} {child_name}", child
        else:
            yield name, command


def test_every_option_and_argument_has_help():
    missing = []
    for name, command in _all_commands():
        for param in command.params:
            text = getattr(param, "help", None)
            if isinstance(param, (click.Option, BusArgument)) and not text:
                missing.append(f"{name} {param.name}")
    assert missing == []


def test_rendered_descriptions_start_with_a_capital(tmp_path):
    lowercase = []
    for name in ("send", "inbox", "watch-status", "keys"):
        out = _cli(tmp_path, *name.split(), "--help").stdout.splitlines()
        body = next((line.strip() for line in out[1:] if line.strip()), "")
        if body and not body[0].isupper():
            lowercase.append((name, body))
    assert lowercase == []


class _Delivery:
    def __init__(self, seq: int) -> None:
        self.seq = seq
        self.raw = {"read_at": None}
        self.attachment_count = 0
        self.sender = "peer"
        self.subject = f"subject {seq}"
        self.delivery_id = f"del_{seq}"


def _inbox(monkeypatch, deliveries, **flags):
    class _Bus:
        def inbox(self, cursor, **kw):
            return deliveries

    monkeypatch.setattr(_read._common, "_bus", lambda args: _Bus())
    args = argparse.Namespace(cursor=0, limit=50, label=None, wait=0, unread=False, json=False)
    for key, value in flags.items():
        setattr(args, key, value)
    out = io.StringIO()
    with redirect_stdout(out):
        assert _read.cmd_inbox(args) == 0
    return out.getvalue()


def test_an_empty_inbox_is_not_called_new(monkeypatch):
    assert _inbox(monkeypatch, []).strip() == "your inbox is empty"
    assert _inbox(monkeypatch, [], unread=True).strip() == "no new messages"
    assert _inbox(monkeypatch, [], cursor=7).strip() == "no messages after cursor 7"


def test_a_full_page_says_how_to_reach_the_next_one(monkeypatch):
    out = _inbox(monkeypatch, [_Delivery(n) for n in range(1, 4)], limit=3)
    assert "cursor: 3" in out
    assert "agentbus inbox --cursor 3" in out
    assert "agentbus inbox --unread" in out


def test_an_unread_listing_does_not_nag_about_unread(monkeypatch):
    out = _inbox(monkeypatch, [_Delivery(1)], unread=True)
    assert "--unread" not in out


def test_a_bad_thread_id_prints_one_explanation(monkeypatch):
    class _Bus:
        def thread(self, thread_id):
            raise NotFoundError("thread not found", code="not_found", status=404)

        def read(self, delivery_id):
            raise NotFoundError("delivery not found", code="not_found", status=404)

    from agentbus_client.cli import main

    monkeypatch.setattr(_threads._common, "_bus", lambda args: _Bus())
    err = io.StringIO()
    with redirect_stderr(err):
        rc = main(["thread", "01X"])
    assert rc == 3
    lines = [line for line in err.getvalue().splitlines() if "not_found" in line]
    assert len(lines) == 1
    assert "`show` takes a DELIVERY id" in lines[0]


def test_agentbus_hook_run_by_hand_says_what_it_is(capsys):
    from agentbus_client.hooks.claude_code import main as hook_main

    rc = hook_main([])
    err = " ".join(capsys.readouterr().err.split())
    assert rc == 2
    assert "You do not normally run this by hand" in err
    assert "`agentbus`" in err
