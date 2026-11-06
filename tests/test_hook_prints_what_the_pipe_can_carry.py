"""#94: the unread preview survives a subject the pipe cannot encode.

JSON can carry a lone surrogate, and `print` on a strict UTF-8 stream raises on it.
"""

from __future__ import annotations

import io
import sys

import pytest

from agentbus_client.hooks import _turn


def _strict_stdout(monkeypatch: pytest.MonkeyPatch) -> io.BytesIO:
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="utf-8", errors="strict"))
    return raw


def test_an_unencodable_subject_prints_escaped_and_stops_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _strict_stdout(monkeypatch)
    lines = ["  a: bad \ud800 subject  (agentbus show 1)", "  b: good  (agentbus show 2)"]
    assert _turn._print_unread(lines, 2) is True
    sys.stdout.flush()
    printed = raw.getvalue().decode("utf-8")
    assert "bad \\ud800 subject" in printed
    assert "b: good" in printed


def test_a_plain_subject_is_printed_as_it_came(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _strict_stdout(monkeypatch)
    _turn._print_unread(["  a: café  (agentbus show 1)"], 1)
    sys.stdout.flush()
    assert "café" in raw.getvalue().decode("utf-8")


@pytest.mark.parametrize("line", [b"{}", b'{"unrelated": 1}'])
def test_a_wake_line_that_names_no_arrival_is_a_bad_line(line: bytes) -> None:
    assert _turn._stale_arrival(line) is None


def test_a_wake_line_that_names_an_arrival_is_kept() -> None:
    line = b'{"sender": "peer", "subject": "hello", "delivery_id": "01X"}'
    assert _turn._stale_arrival(line) == "  peer: hello  (agentbus show 01X)"


def test_a_wake_line_with_only_a_delivery_id_is_kept() -> None:
    assert _turn._stale_arrival(b'{"delivery_id": "01X"}') is not None
