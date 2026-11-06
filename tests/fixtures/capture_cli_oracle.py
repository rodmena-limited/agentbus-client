from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

import agentbus_client
from agentbus_client import __version__
from agentbus_client.cli._app import Verb
from agentbus_client.cli._parser import build, parse_args

VALUE: dict[Any, str] = {int: "7", float: "2.5", None: "v1"}
NUMERIC_TYPES: dict[Any, type] = {click.INT: int, click.FLOAT: float}
HELP_OPTION = "--help"
COMMON_DESTS = {"agent", "json", "sub_agent", "sub_json"}
OPTIONAL = "?"
ZERO_OR_MORE = "*"
ONE_OR_MORE = "+"


@dataclass(frozen=True)
class Param:
    option_strings: tuple[str, ...]
    dest: str
    nargs: str | None
    choices: tuple[str, ...]
    type: type | None
    flag: bool
    append: bool


def _nargs(param: click.Parameter, *, remainder: bool) -> str | None:
    if param.nargs == -1:
        if remainder:
            return argparse.REMAINDER
        return ONE_OR_MORE if param.required else ZERO_OR_MORE
    return None if param.required else OPTIONAL


def _param(param: click.Parameter, *, remainder: bool) -> Param:
    choices = param.type.choices if isinstance(param.type, click.Choice) else ()
    numeric = NUMERIC_TYPES.get(param.type)
    dest = param.name or ""
    if isinstance(param, click.Option):
        names = (*param.opts, *param.secondary_opts)
        return Param(names, dest, None, tuple(choices), numeric, param.is_flag, param.multiple)
    nargs = _nargs(param, remainder=remainder)
    return Param((), dest, nargs, tuple(choices), numeric, flag=False, append=False)


def _params(command: click.Command) -> list[Param]:
    remainder = isinstance(command, Verb) and command.remainder
    return [_param(p, remainder=remainder) for p in command.params]


def _sub(command: click.Command) -> click.Group | None:
    return command if isinstance(command, click.Group) else None


def _long_options(command: click.Command) -> list[str]:
    declared = {o for a in _params(command) for o in a.option_strings if o.startswith("--")}
    return sorted(declared | {HELP_OPTION})


def _value(a: Param) -> str:
    if a.choices:
        return str(next(iter(a.choices)))
    return VALUE.get(a.type, "v1")


def _bad_value(a: Param) -> str | None:
    if a.choices:
        return "not-a-choice"
    if a.type in (int, float):
        return "notanumber"
    return None


def _positional_fill(actions: list[Param]) -> list[str]:
    out: list[str] = []
    for a in actions:
        if a.option_strings:
            continue
        if a.nargs in (None, ONE_OR_MORE):
            out.append(_value(a) if (a.choices or a.type in (int, float)) else f"P_{a.dest}")
    return out


def _record(argv: list[str]) -> dict[str, Any]:
    err = io.StringIO()
    out = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
        try:
            ns = parse_args(argv)
        except SystemExit as exc:
            return {"argv": argv, "exit": exc.code if isinstance(exc.code, int) else 1}
    data = {
        k: f"{v.__module__}.{v.__qualname__}" if k == "func" else v
        for k, v in sorted(vars(ns).items())
    }
    return {"argv": argv, "namespace": data}


def _verb_cases(path: list[str], command: click.Command) -> list[list[str]]:  # noqa: C901, PLR0912, PLR0915
    cases: list[list[str]] = []
    nested = _sub(command)
    if nested is not None:
        cases.append(path)
        cases.append([*path, "no-such-subcommand"])
        for name, sp in nested.commands.items():
            cases.extend(_verb_cases([*path, name], sp))
        return cases

    acts = _params(command)
    base = path + _positional_fill(acts)
    cases.append(base)
    cases.append([*path, "--help"])
    cases.append([*path, "-h"])
    cases.append([*base, "--no-such-flag"])
    cases.append([*base, "extra-positional-1", "extra-positional-2"])

    required_pos = [a for a in acts if not a.option_strings and a.nargs in (None, ONE_OR_MORE)]
    if required_pos:
        cases.append(path)

    all_opts: list[str] = []
    for a in acts:
        if not a.option_strings:
            if a.nargs == OPTIONAL:
                cases.append([*base, f"OPT_{a.dest}"])
            elif a.nargs == ZERO_OR_MORE:
                cases.append([*base, "S1", "S2"])
            elif a.nargs == ONE_OR_MORE:
                cases.append([*base, f"EXTRA_{a.dest}"])
            elif a.nargs == argparse.REMAINDER:
                cases.append([*base, "cmd", "-x", "--y", "z"])
                cases.append([*base, "--", "cmd", "arg"])
            continue
        for opt in a.option_strings:
            if a.flag:
                cases.append([*base, opt])
                cases.append([path[0], opt, *base[1:]] if len(base) > 1 else [*base, opt])
            else:
                val = _value(a)
                cases.append([*base, opt, val])
                if opt.startswith("--"):
                    cases.append([*base, f"{opt}={val}"])
                else:
                    cases.append([*base, f"{opt}{val}"])
                cases.append([path[0], opt, val, *base[1:]])
                cases.append([*base, opt])
                bad = _bad_value(a)
                if bad is not None:
                    cases.append([*base, opt, bad])
                if a.append:
                    cases.append([*base, opt, "a1", opt, "a2"])
                if a.choices:
                    cases.extend([*base, opt, str(c)] for c in a.choices[1:])
        longest = max(a.option_strings, key=len)
        if a.dest in COMMON_DESTS:
            continue
        if a.flag:
            all_opts += [longest]
        else:
            all_opts += [longest, _value(a)]
        if longest.startswith("--") and len(longest) > 5:
            abbrev = longest[:-2]
            extra = [] if a.flag else [_value(a)]
            cases.append([*base, abbrev, *extra])
    if all_opts:
        cases.append(base + all_opts)
    exclusive = command.exclusive if isinstance(command, Verb) else ()
    by_dest = {a.dest: a for a in acts}
    cases.extend(base + [by_dest[n].option_strings[0] for n in group] for group in exclusive)
    return cases


def _global_cases(verbs: list[str]) -> list[list[str]]:
    cases: list[list[str]] = [[], ["--version"], ["--help"], ["-h"], ["no-such-verb"], ["--json"]]
    for v in verbs:
        cases.append(["--agent", "G", v])
        cases.append(["--json", v])
        cases.append(["--api-key", "K", "--base-url", "U", v])
        cases.append(["--agent", "G", v, "--agent", "S"])
        cases.append([v, "--agent", "S"])
        cases.append([v, "--json"])
        cases.append(["--json", v, "--json"])
        cases.append([v, "--api-key", "K"])
    return cases


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(description="Record how the CLI parses every generated argv.")
    cli.add_argument("output", type=Path, help="JSON file to write; there is no default")
    target: Path = cli.parse_args(argv).output

    root = build()
    verbs = root.commands
    cases: list[list[str]] = []
    for name, sp in verbs.items():
        cases.extend(_verb_cases([name], sp))
    global_cases = list(_global_cases(sorted(verbs)))
    for v in sorted(verbs):
        fill = _positional_fill(_params(verbs[v]))
        nested = _sub(verbs[v])
        if nested is not None:
            first = next(iter(nested.commands))
            fill = [first, *_positional_fill(_params(nested.commands[first]))]
        global_cases.extend(
            (
                ["--agent", "G", v, *fill],
                ["--json", v, *fill],
                [v, *fill, "--agent", "S"],
                [v, *fill, "--json"],
                ["--agent", "G", v, *fill, "--agent", "S"],
            )
        )
    cases.extend(global_cases)

    seen = set()
    unique = []
    for c in cases:
        key = tuple(c)
        if key not in seen:
            seen.add(key)
            unique.append(c)

    records = [_record(c) for c in unique]
    source = Path(agentbus_client.__file__).resolve().parent
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=source, check=False
    ).stdout.strip()
    options = {"": _long_options(root)}
    for name, sp in verbs.items():
        options[name] = _long_options(sp)
        nested = _sub(sp)
        if nested is not None:
            for child, csp in nested.commands.items():
                options[f"{name} {child}"] = _long_options(csp)
    payload = {
        "captured_from": {
            "version": __version__,
            "git": sha,
            "python": sys.version.split()[0],
            "source": str(source),
        },
        "options": options,
        "verbs": sorted(verbs),
        "cases": records,
    }
    target.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    ok = sum(1 for r in records if "namespace" in r)
    sys.stdout.write(
        f"{len(records)} cases -> {target.name}: {ok} parsed, {len(records) - ok} exited\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
