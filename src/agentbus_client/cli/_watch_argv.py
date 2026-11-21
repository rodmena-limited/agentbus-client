from __future__ import annotations

import contextlib
from pathlib import Path

_CLI_MODULES = frozenset({"agentbus_client.cli"})
_PY_OPTS_WITH_VALUE = frozenset({"-W", "-X", "-Q"})
_PY_NO_SCRIPT = frozenset({"-c", "-"})


def _interpreter_end(argv: list[str]) -> int | None:
    k = 1
    end: int | None = None
    while k < len(argv) and end is None:
        tok = argv[k]
        if tok == "-m":
            ok = k + 1 < len(argv) and argv[k + 1] in _CLI_MODULES
            end = k + 2 if ok else -1
        elif tok in _PY_NO_SCRIPT:
            end = -1
        elif tok in _PY_OPTS_WITH_VALUE:
            k += 2
        elif tok.startswith("-"):
            k += 1
        else:
            end = k + 1 if Path(tok).name == "agentbus" else -1
    return None if end is None or end < 0 else end


def _program_end(argv: list[str]) -> int | None:
    if not argv:
        return None
    head = Path(argv[0]).name
    if head == "agentbus":
        return 1
    if head.startswith("python"):
        return _interpreter_end(argv)
    return None


def watch_argv_agent(argv: list[str]) -> str | None:
    start = _program_end(argv)
    if start is None or start >= len(argv) or argv[start] != "watch":
        return None
    rest = argv[start + 1 :]
    agent: str | None = None
    for j, tok in enumerate(rest):
        if tok == "--agent" and j + 1 < len(rest):
            agent = rest[j + 1]
        elif tok.startswith("--agent="):
            agent = tok[len("--agent=") :]
    if agent is None:
        return None
    return agent.strip("'\"") or None


def argv_is_watcher(argv: list[str], agent: str) -> bool:
    return watch_argv_agent(argv) == agent


def proc_argv(pid: int) -> list[str] | None:
    with contextlib.suppress(OSError):
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        if raw:
            parts = raw.split(b"\0")
            if parts and parts[-1] == b"":
                parts.pop()
            return [p.decode(errors="replace") for p in parts]
    return None


def scan_argv(pid: int, ps_args: str) -> list[str]:
    exact = proc_argv(pid)
    if exact is not None and " ".join(exact).strip() == ps_args.strip():
        return exact
    return ps_args.split()
