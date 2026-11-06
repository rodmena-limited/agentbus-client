"""#94 item 4: the shared reader for numbers in the environment.

An unusable value falls back with one stderr line per distinct value, and reading
a setting can never be what fails the caller. `test_env_numbers_reach_the_stack.py`
covers the settings themselves.
"""

from __future__ import annotations

import ast
import io
import sys
import threading
import time
from pathlib import Path

import pytest

import agentbus_client
from agentbus_client import _env_numbers
from agentbus_client._env_numbers import LONGEST_SECONDS, env_int, env_seconds

NAME = "AGENTBUS_TEST_NUMBER"
SRC = Path(agentbus_client.__file__).resolve().parent


@pytest.fixture(autouse=True)
def _nothing_reported_yet(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_env_numbers, "_REPORTED", set())
    monkeypatch.delenv(NAME, raising=False)


def test_an_unset_whole_number_is_the_default_and_says_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert env_int(NAME, 9) == 9
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(("raw", "value"), [("5", 5), (" 5 ", 5), ("1", 1), ("10000000", 10**7)])
def test_a_usable_whole_number_is_used_and_says_nothing(
    raw: str, value: int, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(NAME, raw)
    assert env_int(NAME, 9) == value
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "raw", ["abc", "", " ", "1.5", "1e3", "nan", "inf", "-inf", "0", "-1", "9" * 5000]
)
def test_an_unusable_whole_number_falls_back_and_says_so(
    raw: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(NAME, raw)
    assert env_int(NAME, 9) == 9
    assert capsys.readouterr().err == (
        f"agentbus: {NAME}={raw!r} is not a whole number of 1 or more. "
        "The client is using 9. Set it to 1 or higher, or unset it for the default of 9.\n"
    )


def test_zero_can_be_allowed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(NAME, "0")
    assert env_int(NAME, 9, minimum=0) == 0
    assert capsys.readouterr().err == ""
    monkeypatch.setenv(NAME, "-1")
    assert env_int(NAME, 9, minimum=0) == 9
    assert "is not a whole number of 0 or more" in capsys.readouterr().err


@pytest.mark.parametrize(("raw", "value"), [("0", 1), ("-4", 1), ("abc", 9), ("1", 1), ("2", 2)])
def test_a_floor_applies_only_below_the_minimum(
    raw: str, value: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(NAME, raw)
    assert env_int(NAME, 9, below=1) == value


def test_a_floor_is_named_as_the_value_in_use(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(NAME, "0")
    assert env_int(NAME, 9, below=1) == 1
    assert capsys.readouterr().err == (
        f"agentbus: {NAME}='0' is not a whole number of 1 or more. "
        "The client is using 1. Set it to 1 or higher, or unset it for the default of 9.\n"
    )


@pytest.mark.parametrize(("raw", "value"), [("1", 1), ("10", 10), ("11", 9), ("1" + "0" * 400, 9)])
def test_a_maximum_refuses_the_value_above_it(
    raw: str, value: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(NAME, raw)
    assert env_int(NAME, 9, maximum=10) == value


def test_a_maximum_is_named_in_the_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(NAME, "11")
    env_int(NAME, 9, maximum=10)
    assert capsys.readouterr().err == (
        f"agentbus: {NAME}='11' is not a whole number from 1 to 10. "
        "The client is using 9. Set it to a value in that range, or unset it for the "
        "default of 9.\n"
    )


def test_unset_seconds_are_the_default_and_say_nothing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert env_seconds(NAME, 30.0) == 30.0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("raw", "value"),
    [
        ("0", 0.0),
        ("0.25", 0.25),
        ("30", 30.0),
        ("1e3", 1000.0),
        (" 7 ", 7.0),
        (str(LONGEST_SECONDS), float(LONGEST_SECONDS)),
    ],
)
def test_usable_seconds_are_used_and_say_nothing(
    raw: str, value: float, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(NAME, raw)
    assert env_seconds(NAME, 30.0) == value
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "raw",
    ["abc", "", "nan", "-nan", "inf", "-inf", "-1", "-0.001", "1e14", "1e400", "31536001"],
)
def test_unusable_seconds_fall_back_and_say_so(
    raw: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(NAME, raw)
    assert env_seconds(NAME, 30.0) == 30.0
    assert capsys.readouterr().err == (
        f"agentbus: {NAME}={raw!r} is not a number of seconds from 0 to 31536000. "
        "The client is using 30. Set it to a value in that range, or unset it for the "
        "default of 30.\n"
    )


@pytest.mark.parametrize("raw", ["0", "-1", "nan", "inf"])
def test_zero_seconds_can_be_refused(
    raw: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(NAME, raw)
    assert env_seconds(NAME, 5.0, zero_allowed=False) == 5.0
    assert capsys.readouterr().err == (
        f"agentbus: {NAME}={raw!r} is not a number of seconds above 0 and up to 31536000. "
        "The client is using 5. Set it to a value in that range, or unset it for the "
        "default of 5.\n"
    )


def test_a_small_value_is_kept_when_zero_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(NAME, "0.001")
    assert env_seconds(NAME, 5.0, zero_allowed=False) == 0.001


def test_each_distinct_value_is_reported_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(NAME, "abc")
    assert [env_int(NAME, 9) for _ in range(5)] == [9] * 5
    assert len(capsys.readouterr().err.splitlines()) == 1
    monkeypatch.setenv(NAME, "xyz")
    env_int(NAME, 9)
    assert "'xyz'" in capsys.readouterr().err
    monkeypatch.setenv(NAME, "abc")
    env_int(NAME, 9)
    assert capsys.readouterr().err == ""


def test_the_same_value_in_another_setting_is_reported_too(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    other = "AGENTBUS_TEST_OTHER_NUMBER"
    monkeypatch.setenv(NAME, "abc")
    monkeypatch.setenv(other, "abc")
    env_int(NAME, 9)
    env_int(other, 9)
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 2
    assert f"{NAME}='abc'" in err
    assert f"{other}='abc'" in err


class _SlowSet(set[tuple[str, str]]):
    def __contains__(self, item: object) -> bool:
        answer = super().__contains__(item)
        time.sleep(0.01)
        return answer


def test_concurrent_first_readers_print_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The set widens the gap between the check and the add, so without the lock
    every thread passes the check and prints."""
    monkeypatch.setattr(_env_numbers, "_REPORTED", _SlowSet())
    monkeypatch.setenv(NAME, "abc")
    start = threading.Barrier(8)
    answers: list[int] = []

    def _read() -> None:
        start.wait(5)
        answers.append(env_int(NAME, 9))

    threads = [threading.Thread(target=_read) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert answers == [9] * 8
    assert len(capsys.readouterr().err.splitlines()) == 1


def test_a_value_with_a_newline_stays_on_one_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(NAME, "1\n2")
    assert env_int(NAME, 9) == 9
    assert len(capsys.readouterr().err.splitlines()) == 1


class _BrokenPipe(io.StringIO):
    def write(self, _text: str) -> int:
        raise BrokenPipeError


def _closed_stream() -> io.StringIO:
    stream = io.StringIO()
    stream.close()
    return stream


@pytest.mark.parametrize(
    "stream",
    [
        pytest.param(None, id="no-stderr"),
        pytest.param(_closed_stream(), id="closed-stderr"),
        pytest.param(_BrokenPipe(), id="broken-pipe"),
    ],
)
def test_a_stderr_that_cannot_be_written_changes_nothing(
    stream: io.StringIO | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The hooks answer the harness on stdout, and `print(file=None)` writes there."""
    monkeypatch.setenv(NAME, "abc")
    monkeypatch.setattr(sys, "stderr", stream)
    assert env_int(NAME, 9) == 9
    assert env_seconds(NAME, 5.0) == 5.0
    assert capsys.readouterr().out == ""


def _direct_reads(source: str) -> list[str]:
    return [
        ast.unparse(node)
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"int", "float"}
        and "environ" in ast.unparse(node)
    ]


def test_the_scan_below_finds_a_known_positive() -> None:
    wrapped = 'import os\nx = int(\n    os.environ.get("A", "1")\n)\ny = float(os.environ["B"])\n'
    assert _direct_reads(wrapped) == ["int(os.environ.get('A', '1'))", "float(os.environ['B'])"]
    assert _direct_reads('import os\nx = int("1")\ny = os.environ.get("A")\n') == []


def test_nothing_in_src_parses_the_environment_by_hand() -> None:
    found = {
        str(path.relative_to(SRC)): reads
        for path in SRC.rglob("*.py")
        if (reads := _direct_reads(path.read_text(encoding="utf-8")))
    }
    assert found == {}
