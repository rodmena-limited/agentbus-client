"""#93 item 21: a sender cannot break out of the operator's own quotes.

shlex.quote is only correct where the placeholder is unquoted. Written inside a
template's own quotes, `notify '{subject}'`, its quotes close the operator's and
the rest of the subject runs as shell. These tests run the real handler through
the real /bin/sh. `notify` below is a script on PATH that writes its argv to a
file, so the assertions read what the shell delivered, not what we built.
"""

from __future__ import annotations

import os
import stat
from typing import TYPE_CHECKING

import pytest

from agentbus_client import watch

if TYPE_CHECKING:
    from pathlib import Path

HOSTILE = """it's "quoted" $(touch CANARY-subst) `touch CANARY-tick` '; touch CANARY-semi; echo '"""
ODD = [
    HOSTILE,
    "a'b",
    'a"b',
    "$HOME ${HOME} $(id)",
    "`id`",
    "back\\slash\\",
    "trailing'",
    "'",
    '"',
    "new\nline",
    "tab\tspace  two",
    "* ? [a-z]",
    "; touch CANARY-bare",
    "| touch CANARY-pipe",
    "&& touch CANARY-and",
    "> CANARY-redirect",
    "-n",
    "café ☃",
    "%s %d",
    "{subject}",
]
TEMPLATES = {
    "unquoted": "notify {subject}",
    "single": "notify '{subject}'",
    "double": 'notify "{subject}"',
}
WRAPPED = {
    "single_with_text": ("notify 'before {subject} after'", "before ", " after"),
    "double_with_text": ('notify "before {subject} after"', "before ", " after"),
    "glued_unquoted": ("notify pre-{subject}-post", "pre-", "-post"),
    "flag_equals": ("notify --subject={subject}", "--subject=", ""),
    "in_substitution": (
        "notify \"$(printf '%s' 'in {subject} out')\"",
        "in ",
        " out",
    ),
    "in_backticks": ("notify \"`printf '%s' '{subject}'`\"", "", ""),
    "after_escaped_quote": ("notify \\' '{subject}'", "", ""),
}


@pytest.fixture
def shell(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "notify"
    script.write_text('#!/bin/sh\nprintf \'%s\\0\' "$@" > "$NOTIFY_OUT"\n')
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("NOTIFY_OUT", str(tmp_path / "argv"))
    monkeypatch.delenv("AGENTBUS_EXEC_TIMEOUT", raising=False)
    return tmp_path


def _expand(template: str, fields: dict[str, str]) -> tuple[str, dict[str, str]]:
    from agentbus_client import _watch_exec

    return _watch_exec.expand(template, fields)


def _delivered(root: Path) -> list[str]:
    raw = (root / "argv").read_bytes()
    return raw.decode().split("\0")[:-1]


def _canaries(root: Path) -> list[str]:
    return sorted(p.name for p in (root / "work").iterdir())


@pytest.mark.parametrize("value", ODD)
@pytest.mark.parametrize("template", TEMPLATES.values(), ids=TEMPLATES.keys())
def test_the_shell_delivers_the_subject_as_one_exact_argument(
    template: str, value: str, shell: Path
) -> None:
    watch.notify_command(template)({"subject": value})
    assert _delivered(shell) == [value]
    assert _canaries(shell) == []


@pytest.mark.parametrize("value", [HOSTILE, "plain", ""])
@pytest.mark.parametrize(
    ("template", "prefix", "suffix"), WRAPPED.values(), ids=list(WRAPPED.keys())
)
def test_text_around_the_placeholder_survives(
    template: str, prefix: str, suffix: str, value: str, shell: Path
) -> None:
    watch.notify_command(template)({"subject": value})
    assert _delivered(shell)[-1] == f"{prefix}{value}{suffix}"
    assert _canaries(shell) == []


def test_the_item_example_runs_no_command_substitution(shell: Path) -> None:
    subject = "x'$(touch CANARY-item)'y"
    watch.notify_command("notify '{subject}'")({"subject": subject})
    assert _delivered(shell) == [subject]
    assert not (shell / "work" / "CANARY-item").exists()


def test_several_placeholders_keep_their_own_values(shell: Path) -> None:
    message = {"subject": "s'1", "sender_display": 'p"2', "delivery_id": "01D", "agent_seq": 42}
    watch.notify_command("""notify {subject} '{sender}' "{delivery_id}" {agent_seq} '{subject}'""")(
        message
    )
    assert _delivered(shell) == ["s'1", 'p"2', "01D", "42", "s'1"]


def test_an_empty_value_is_still_an_argument(shell: Path) -> None:
    watch.notify_command("notify a {subject} b")({"subject": ""})
    assert _delivered(shell) == ["a", "", "b"]


def test_literal_braces_still_reach_the_shell(shell: Path) -> None:
    watch.notify_command("notify '{{literal}}' {subject}")({"subject": "s"})
    assert _delivered(shell) == ["{literal}", "s"]


def test_a_format_spec_applies_to_the_value(shell: Path) -> None:
    watch.notify_command("notify '{subject:>4}' {subject!r}")({"subject": "a'"})
    assert _delivered(shell) == ["  a'", '"a\'"']


def test_a_value_with_no_shell_meaning_stays_in_the_command() -> None:
    """Same bytes as shlex.quote wrote, so a timeout line stays readable."""
    command, passed = _expand(
        "notify --id {delivery_id} '{delivery_id}'", {"delivery_id": "01D.a_b-c"}
    )
    assert command == "notify --id 01D.a_b-c '01D.a_b-c'"
    assert passed == {}


@pytest.mark.parametrize("value", [HOSTILE, "a b", "a'b", "", "é"])
def test_any_other_value_never_enters_the_shell_text(value: str) -> None:
    """The property everything rests on. The quote tracking only decides how a
    value renders, because a value it cannot place is never parsed as shell."""
    command, passed = _expand("notify {subject} '{subject}'", {"subject": value})
    assert command == ("notify \"${AGENTBUS_FIELD_0}\" ''\"${AGENTBUS_FIELD_1}\"''")
    assert passed == {"AGENTBUS_FIELD_0": value, "AGENTBUS_FIELD_1": value}


def test_an_unknown_placeholder_still_raises() -> None:
    with pytest.raises(KeyError, match="typo"):
        _expand("notify {typo}", {"subject": "s"})


def test_a_bad_template_still_raises() -> None:
    with pytest.raises(ValueError, match="Single '}'"):
        _expand("notify }", {"subject": "s"})
