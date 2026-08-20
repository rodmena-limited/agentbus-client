"""#229: `verify-sender --json` reported an UNSIGNED message as a failed signature.

FOUND BY agentbus-ui-c760a1 on 0.9.8, in a test I did not ask for. Same delivery,
two output modes, two different answers:

    agentbus verify-sender <id>          -> exit 2, "UNSIGNED ... NOT a failed signature"
    agentbus verify-sender <id> --json   -> exit 1, identical to a real mismatch

Reproduced here before fixing: plain exit 2, --json exit 1, on one delivery.

WHY THIS IS THE WORST PLACE FOR IT. #220 exists because a verifier must never
report a negative it did not earn, and the human-readable path was fixed to say
CANNOT VERIFY. The `--json` branch computed its own code — `0 if verified else 1`
— and returned BEFORE the verdict was consulted. So the fix landed in the path a
person reads and missed the path a SCRIPT reads, which is the only one that
automates on the exit code. Anything gating on `--json` + exit status would
treat "I could not check this" as "this is forged".

THE FIX IS ONE MAPPING FOR BOTH BRANCHES. Two copies of a rule is what put the
bug here; `_verify_exit_code` is now the only place the mapping exists.
"""

from __future__ import annotations

import argparse
import contextlib
import io
from typing import Any

import pytest

from agentbus_client import cli

CASES = [
    ({"verified": True, "verdict": "valid", "signed_by": "peer", "key_fingerprint": "ab12"}, 0),
    ({"verified": False, "verdict": "invalid", "reason": "bytes differ"}, 1),
    ({"verified": False, "verdict": "unverifiable", "reason": "no key"}, 2),
    ({"verified": False, "verdict": "unsigned", "reason": "unsigned"}, 2),
]


@pytest.mark.parametrize(("result", "expected"), CASES)
def test_the_mapping_is_what_we_say_it_is(result: dict[str, Any], expected: int) -> None:
    assert cli._verify_exit_code(result) == expected


@pytest.mark.parametrize(("result", "expected"), CASES)
def test_both_output_modes_return_the_same_code(
    result: dict[str, Any], expected: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE REGRESSION. --json and plain text must never disagree.

    Driving the real command both ways rather than asserting on source text,
    because the bug was an early `return` — something a text search would not
    have noticed and only running it exposes.
    """

    class _Bus:
        def verify(self, _delivery_id: str) -> dict[str, Any]:
            return result

    monkeypatch.setattr(cli._common, "_bus", lambda _args: _Bus())

    codes = {}
    for as_json in (False, True):
        args = argparse.Namespace(delivery_id="d", json=as_json)
        with contextlib.redirect_stdout(io.StringIO()):
            codes[as_json] = cli.cmd_verify_signature(args)

    assert codes[False] == codes[True] == expected, (
        f"plain returned {codes[False]} and --json returned {codes[True]} for "
        f"verdict={result.get('verdict')!r}; a script reading the exit code gets "
        "a different answer than a human reading the text"
    )


def test_a_script_can_tell_cannot_check_from_forged() -> None:
    """The property that matters, stated once in the terms an operator cares about.

    Exit 1 must mean 'the bytes do not match the key'. If anything that could
    not be checked also exits 1, every automated caller has to treat unknown as
    hostile — which is the failure #220 was about.
    """
    unchecked = [
        c for c, _ in [(r, e) for r, e in CASES] if c["verdict"] in ("unverifiable", "unsigned")
    ]
    assert unchecked, "no cannot-check cases in the table; this asserts nothing"
    for result in unchecked:
        assert cli._verify_exit_code(result) != 1, (
            f"verdict={result['verdict']!r} exits 1, indistinguishable from a real mismatch"
        )
