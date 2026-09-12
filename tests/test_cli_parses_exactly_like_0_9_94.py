from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import pytest

from agentbus_client.cli._parser import parse_args

ORACLE = json.loads(
    (Path(__file__).parent / "fixtures" / "cli_argparse_oracle_0_9_94.json").read_text()
)
VERBS = set(ORACLE["verbs"])
OPTIONS = {path: set(opts) for path, opts in ORACLE["options"].items()}
TAKES_VALUE = {"--agent", "--api-key", "--base-url"}
CASES = ORACLE["cases"]


def _path(argv: list[str]) -> tuple[str | None, int]:
    i = 0
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in TAKES_VALUE else 1
    if i >= len(argv) or argv[i] not in VERBS:
        return None, i
    verb = argv[i]
    if i + 1 < len(argv) and f"{verb} {argv[i + 1]}" in OPTIONS:
        return f"{verb} {argv[i + 1]}", i + 2
    return verb, i + 1


def delta(argv: list[str]) -> str | None:
    path, start = _path(argv)
    if path is None:
        if all(t.startswith("-") for t in argv) and not {"--help", "-h", "--version"} & set(argv):
            return "no-verb"
        return None
    known = OPTIONS[path]
    for token in argv[start:]:
        if token == "--":
            break
        if (
            token.startswith("--")
            and "=" not in token
            and token not in known
            and any(option.startswith(token) for option in known)
        ):
            return "abbreviation"
    return None


def _observe(argv: list[str]) -> str:
    try:
        ns = parse_args(argv)
    except SystemExit as exc:
        return json.dumps({"exit": exc.code if isinstance(exc.code, int) else 1})
    data = {}
    for key, value in sorted(vars(ns).items()):
        if key == "func":
            value = f"{value.__module__}.{value.__qualname__}"
        data[key] = value
    return json.dumps({"namespace": data}, sort_keys=True)


def _expected(case: dict) -> str:
    return json.dumps({k: v for k, v in case.items() if k != "argv"}, sort_keys=True)


def test_the_oracle_is_the_argparse_release_and_covers_every_verb():
    assert ORACLE["captured_from"]["version"] == "0.9.94"
    assert ORACLE["captured_from"]["git"].startswith("cbaae48")
    parsed = [c for c in CASES if "namespace" in c]
    assert len(parsed) >= 1000
    verbs_parsed = {p.split()[0] for p in (_path(c["argv"])[0] for c in parsed) if p}
    assert verbs_parsed == VERBS


def test_the_only_deltas_are_the_documented_ones():
    kinds = Counter(delta(c["argv"]) for c in CASES)
    assert set(kinds) <= {None, "abbreviation", "no-verb"}
    assert kinds["no-verb"] == 2
    assert kinds["abbreviation"] > 0
    assert all(c.get("exit") == 2 for c in CASES if delta(c["argv"]) == "no-verb")


@pytest.mark.parametrize("case", CASES, ids=lambda c: " ".join(c["argv"]) or "<bare>")
def test_the_command_line_parses_exactly_as_0_9_94_did(case, capsys):
    got = _observe(case["argv"])
    kind = delta(case["argv"])
    if kind == "abbreviation":
        assert got == json.dumps({"exit": 2})
    elif kind == "no-verb":
        assert got == json.dumps({"exit": 0})
    else:
        assert got == _expected(case)
