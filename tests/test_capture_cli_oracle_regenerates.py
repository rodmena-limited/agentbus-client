"""tests/fixtures/capture_cli_oracle.py regenerates the oracle from the current CLI.

The script reads the command tree and records what the live parser does with each
argv it generates. Regenerated output differs from the checked-in 0.9.94 oracle only
by the deltas test_cli_parses_exactly_like_0_9_94.py documents and by flags added
since. These tests run the script the way an operator does, into a temp path.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

import agentbus_client
from agentbus_client.cli._parser import build

from .test_cli_parses_exactly_like_0_9_94 import ADDED_AFTER_0_9_94, ORACLE, delta

FIXTURES = Path(__file__).parent / "fixtures"
SCRIPT = FIXTURES / "capture_cli_oracle.py"
CHECKED_IN = FIXTURES / "cli_argparse_oracle_0_9_94.json"
FLAGS_ADDED_AFTER_0_9_94 = {
    "drafts": {"--delete"},
    "send": {"--allow-empty"},
    "reply": {"--allow-empty"},
}


def _capture(*argv: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": str(Path(agentbus_client.__file__).parents[1])}
    return subprocess.run(
        [sys.executable, str(SCRIPT), *argv],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )


@pytest.fixture(scope="module")
def regenerated(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    target = tmp_path_factory.mktemp("oracle") / "regenerated.json"
    done = _capture(str(target))
    assert done.returncode == 0, done.stderr
    return dict(json.loads(target.read_text()))


def _by_argv(cases: list[dict[str, Any]]) -> dict[tuple[str, ...], dict[str, Any]]:
    return {tuple(case["argv"]): case for case in cases}


def _outcome(case: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in case.items() if key != "argv"}


def _without_later_fields(outcome: dict[str, Any]) -> dict[str, Any]:
    namespace = outcome.get("namespace")
    if namespace is None:
        return outcome
    command = namespace.get("command")
    added = ADDED_AFTER_0_9_94.get(command, {}) if isinstance(command, str) else {}
    kept = {
        key: value for key, value in namespace.items() if not (key in added and value == added[key])
    }
    return {"namespace": kept}


def test_it_records_the_running_cli(regenerated: dict[str, Any]) -> None:
    assert regenerated["captured_from"]["version"] == agentbus_client.__version__
    assert regenerated["verbs"] == sorted(build().commands)
    assert len(regenerated["cases"]) == len(_by_argv(regenerated["cases"]))


def test_every_oracle_case_is_generated_again(regenerated: dict[str, Any]) -> None:
    """An oracle argv missing from the output is one that listed every option of
    its verb, and the regenerated argv extends it with the flags added since."""
    fresh = _by_argv(regenerated["cases"])
    missing = [argv for argv in _by_argv(ORACLE["cases"]) if argv not in fresh]
    assert missing, "send and reply gained flags, so their full-option cases must have changed"
    for argv in missing:
        extensions = [set(other[len(argv) :]) for other in fresh if other[: len(argv)] == argv]
        assert extensions == [FLAGS_ADDED_AFTER_0_9_94.get(argv[0], set())], argv


def test_the_outcomes_differ_only_by_the_documented_deltas(regenerated: dict[str, Any]) -> None:
    fresh = _by_argv(regenerated["cases"])
    kinds = {"abbreviation": 0, "no-verb": 0}
    for argv, case in _by_argv(ORACLE["cases"]).items():
        if argv not in fresh:
            continue
        kind = delta(list(argv))
        if kind is None:
            assert _without_later_fields(_outcome(fresh[argv])) == _outcome(case), argv
            continue
        assert fresh[argv].get("exit") == (2 if kind == "abbreviation" else 0), argv
        kinds[kind] += 1
    assert kinds["no-verb"] == 2
    assert kinds["abbreviation"] > 0


def test_options_match_except_for_flags_added_since(regenerated: dict[str, Any]) -> None:
    assert set(regenerated["options"]) == set(ORACLE["options"])
    for path, known in ORACLE["options"].items():
        added = FLAGS_ADDED_AFTER_0_9_94.get(path, set())
        assert set(regenerated["options"][path]) == set(known) | added, path


def test_the_output_path_is_required() -> None:
    """No default target, so a bare run cannot replace the checked-in oracle."""
    before = hashlib.sha256(CHECKED_IN.read_bytes()).hexdigest()

    done = _capture()

    assert done.returncode == 2
    assert "output" in done.stderr
    assert hashlib.sha256(CHECKED_IN.read_bytes()).hexdigest() == before
