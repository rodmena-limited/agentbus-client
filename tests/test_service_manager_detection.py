"""#153 — `agentbus service` must never emit a unit for an init that isn't there.

On FreeBSD (neither systemctl nor launchctl) the old code defaulted to systemd:
a complete unit, exit 0, and instructions naming a binary that does not exist —
so the documented remedy for the number-one failure (an unwatched inbox)
silently guaranteed it. Reported by auth-service-b080da + infra-manager-c13110
from rodmena-vm-2, two independent observations.

PROVEN RED FIRST, as both reporters demanded: this file was written before the
fix and run against the then-current build — the hard-fail test FAILED (the
command exited 0 and printed [Unit]) — then the fix landed and it passed. A
check that has never gone red cannot go green meaningfully.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import shlex
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentbus_client import cli


def _invoke(manager: str | None, which: dict[str, str | None], system: str = "FreeBSD") -> Any:
    """Run cmd_service with a faked platform; return (exit_code, stdout, stderr)."""
    args = argparse.Namespace(agent="probe-agent", manager=manager, env_file=None)
    out, err = io.StringIO(), io.StringIO()
    with (
        patch("platform.system", return_value=system),
        patch("shutil.which", side_effect=which.get),
        patch.dict("os.environ", {"AGENTBUS_AGENT": "probe-agent"}),
        contextlib.redirect_stdout(out),
        contextlib.redirect_stderr(err),
    ):
        code = cli.cmd_service(args)
    return code, out.getvalue(), err.getvalue()


_FREEBSD = {"systemctl": None, "launchctl": None, "agentbus": "/root/.local/bin/agentbus"}
_LINUX = {
    "systemctl": "/usr/bin/systemctl",
    "launchctl": None,
    "agentbus": "/usr/local/bin/agentbus",
}


def test_bare_autodetect_hard_fails_where_no_manager_exists() -> None:
    """The reported defect verbatim: FreeBSD, no flags, expect NON-ZERO and no unit."""
    code, out, err = _invoke(None, _FREEBSD)
    assert code != 0, "exit 0 with an unloadable unit is the silent no-watcher failure"
    assert "[Unit]" not in out, "no systemd unit may be emitted for an absent init"
    assert "systemctl" not in out, "no instruction may name a binary the host lacks"
    combined = out + err
    assert "rc.d" in combined, "the refusal must name the way out (--manager rc.d)"
    assert "systemd" in combined, "say what was looked for"
    assert "launchd" in combined, "say what was looked for"


def test_linux_with_systemd_still_gets_a_unit() -> None:
    """KNOWN-POSITIVE: the hard-fail must not fire where systemd exists."""
    code, out, _err = _invoke(None, _LINUX, system="Linux")
    assert code == 0
    assert "[Unit]" in out
    assert "ExecStart=" in out


def _shell_lines(script: str) -> list[str]:
    return [line for line in script.splitlines() if not line.lstrip().startswith("#")]


def _daemon_words(script: str) -> list[str]:
    """The words of the script's command_args assignment, which is what daemon(8) receives."""
    match = re.search(
        r'^command_args="(.*?)"$', "\n".join(_shell_lines(script)), re.MULTILINE | re.DOTALL
    )
    assert match, "the rc.d script assigns no command_args"
    return shlex.split(match.group(1).replace("\\\n", " "))


def _flag_value(words: list[str], flag: str) -> str:
    assert flag in words, f"daemon(8) is not given {flag}: {words}"
    return words[words.index(flag) + 1]


def test_explicit_rcd_emits_an_rc_script_not_a_unit() -> None:
    code, out, err = _invoke("rc.d", _FREEBSD)
    assert code == 0, "an explicitly requested rc.d script must be emitted"
    assert "[Unit]" not in out
    assert 'run_rc_command "$1"' in _shell_lines(out)
    assert ". /etc/rc.subr" in _shell_lines(out)
    words = _daemon_words(out)
    # the supervisor/child pidfile split — using only one means `service stop`
    # kills the wrong process and daemon(8) restarts the watcher being stopped
    assert 'pidfile="/var/run/${name}.pid"' in _shell_lines(out)
    assert _flag_value(words, "-P") == "${pidfile}", "-P names the pidfile rc.subr stops"
    assert _flag_value(words, "-p") == "/var/run/${name}.child.pid"
    assert "-r" in words, "-r must pair with -R or a config error hot-loops"
    assert int(_flag_value(words, "-R")) >= 60, (
        "daemon(8) cannot suppress a restart per exit status, so a revoked key loops "
        "here forever; #58 raised the interval to cap that at ~1,440 requests a day "
        "instead of the 24,324 measured on a 5s interval"
    )
    assert "KEYWORD: shutdown" in out
    assert "ab_sk_" not in (out + err), "never inline a credential into a world-readable rc script"
    # Instructions go to STDERR deliberately, so `> /usr/local/etc/rc.d/...`
    # captures only the script — same split the systemd branch uses.
    assert "watch-status" in err, (
        "the install instructions must verify ATTACHMENT, not service-start exit 0 — "
        "a watcher that gives up is indistinguishable from one never started"
    )


def test_explicit_systemd_on_freebsd_is_the_operators_call() -> None:
    """--manager systemd stays honored anywhere: explicit beats detection."""
    code, out, _err = _invoke("systemd", _FREEBSD)
    assert code == 0
    assert "[Unit]" in out
