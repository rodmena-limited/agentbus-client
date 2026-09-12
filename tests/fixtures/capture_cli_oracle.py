from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import sys
from pathlib import Path

import agentbus_client
from agentbus_client import __version__
from agentbus_client.cli._parser import build_parser

VALUE = {int: "7", float: "2.5", None: "v1"}


def _sub(parser: argparse.ArgumentParser) -> argparse._SubParsersAction | None:
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a
    return None


def _long_options(parser: argparse.ArgumentParser) -> list[str]:
    return sorted({o for a in parser._actions for o in a.option_strings if o.startswith("--")})


def _value(a: argparse.Action) -> str:
    if a.choices:
        return str(next(iter(a.choices)))
    return VALUE.get(a.type, "v1")


def _bad_value(a: argparse.Action) -> str | None:
    if a.choices:
        return "not-a-choice"
    if a.type in (int, float):
        return "notanumber"
    return None


def _positional_fill(actions: list[argparse.Action]) -> list[str]:
    out: list[str] = []
    for a in actions:
        if a.option_strings or isinstance(a, argparse._SubParsersAction):
            continue
        if a.nargs in (None, "+"):
            out.append(_value(a) if (a.choices or a.type in (int, float)) else f"P_{a.dest}")
    return out


def _record(argv: list[str]) -> dict:
    err = io.StringIO()
    out = io.StringIO()
    with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
        try:
            ns = build_parser().parse_args(argv)
        except SystemExit as exc:
            return {"argv": argv, "exit": exc.code if isinstance(exc.code, int) else 1}
    data = {}
    for k, v in sorted(vars(ns).items()):
        if k == "func":
            v = f"{v.__module__}.{v.__qualname__}"
        data[k] = v
    return {"argv": argv, "namespace": data}


def _verb_cases(path: list[str], parser: argparse.ArgumentParser) -> list[list[str]]:
    cases: list[list[str]] = []
    nested = _sub(parser)
    if nested is not None:
        cases.append(path)
        cases.append([*path, "no-such-subcommand"])
        for name, sp in nested.choices.items():
            cases.extend(_verb_cases([*path, name], sp))
        return cases

    acts = [a for a in parser._actions if not isinstance(a, argparse._HelpAction)]
    base = path + _positional_fill(acts)
    cases.append(base)
    cases.append([*path, "--help"])
    cases.append([*path, "-h"])
    cases.append([*base, "--no-such-flag"])
    cases.append([*base, "extra-positional-1", "extra-positional-2"])

    required_pos = [a for a in acts if not a.option_strings and a.nargs in (None, "+")]
    if required_pos:
        cases.append(path)

    all_opts: list[str] = []
    for a in acts:
        if not a.option_strings:
            if a.nargs == "?":
                cases.append([*base, f"OPT_{a.dest}"])
            elif a.nargs == "*":
                cases.append([*base, "S1", "S2"])
            elif a.nargs == "+":
                cases.append([*base, f"EXTRA_{a.dest}"])
            elif a.nargs == argparse.REMAINDER:
                cases.append([*base, "cmd", "-x", "--y", "z"])
                cases.append([*base, "--", "cmd", "arg"])
            continue
        for opt in a.option_strings:
            if isinstance(a, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
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
                if isinstance(a, argparse._AppendAction):
                    cases.append([*base, opt, "a1", opt, "a2"])
                if a.choices:
                    for c in list(a.choices)[1:]:
                        cases.append([*base, opt, str(c)])
        longest = max(a.option_strings, key=len)
        if a.dest in ("agent", "json"):
            continue
        if isinstance(a, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
            all_opts += [longest]
        else:
            all_opts += [longest, _value(a)]
        if longest.startswith("--") and len(longest) > 5:
            abbrev = longest[:-2]
            extra = [] if isinstance(a, argparse._StoreTrueAction) else [_value(a)]
            cases.append([*base, abbrev, *extra])
    if all_opts:
        cases.append(base + all_opts)
    for group in parser._mutually_exclusive_groups:
        cases.append(base + [g.option_strings[0] for g in group._group_actions])
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


def main() -> int:
    parser = build_parser()
    sub = _sub(parser)
    assert sub is not None
    cases: list[list[str]] = []
    for name, sp in sub.choices.items():
        cases.extend(_verb_cases([name], sp))
    global_cases = []
    for c in _global_cases(sorted(sub.choices)):
        global_cases.append(c)
    for v in sorted(sub.choices):
        acts = [a for a in sub.choices[v]._actions if not isinstance(a, argparse._HelpAction)]
        fill = _positional_fill(acts)
        nested = _sub(sub.choices[v])
        if nested is not None:
            first = next(iter(nested.choices))
            fill = [first, *_positional_fill(list(nested.choices[first]._actions))]
        for c in (
            ["--agent", "G", v, *fill],
            ["--json", v, *fill],
            [v, *fill, "--agent", "S"],
            [v, *fill, "--json"],
            ["--agent", "G", v, *fill, "--agent", "S"],
        ):
            global_cases.append(c)
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
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=source
    ).stdout.strip()
    options = {"": _long_options(parser)}
    for name, sp in sub.choices.items():
        options[name] = _long_options(sp)
        nested = _sub(sp)
        if nested is not None:
            for child, csp in nested.choices.items():
                options[f"{name} {child}"] = _long_options(csp)
    payload = {
        "captured_from": {
            "version": __version__,
            "git": sha,
            "python": sys.version.split()[0],
            "source": str(source),
        },
        "options": options,
        "verbs": sorted(sub.choices),
        "cases": records,
    }
    target = Path(__file__).with_name("cli_argparse_oracle_0_9_94.json")
    target.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")
    ok = sum(1 for r in records if "namespace" in r)
    print(f"{len(records)} cases -> {target.name}: {ok} parsed, {len(records) - ok} exited")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
