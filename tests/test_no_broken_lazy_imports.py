"""Every lazy import inside a function must actually resolve.

BUG THIS EXISTS FOR: `agentbus verify-sender` and the async signing path both
did `from . import _signing` from inside `client/`, but `_signing` lives at the
PACKAGE ROOT. One dot too few. Reported by bikeroom-freebsd-operato-b124c2 with
the root cause already isolated.

WHY THE TEST SUITE DID NOT CATCH IT: the import is INSIDE a function, so it is
not executed at module import time. Nothing evaluates that line until a user
runs the verb — so `verify-sender` was 100% broken with a green suite, and the
async twin carried the identical fault untested.

That is the module-shadowing family Farshid warned the other platforms about the
same evening: a refactor moves a module, imports elsewhere keep naming the old
location, and the failure is invisible until the code path actually runs.

THIS TEST EXECUTES EVERY LAZY IMPORT rather than reading them, because a regex
over source could not tell a correct `from . import x` from a broken one — that
depends on where x lives, which only the import system knows.
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1] / "src" / "agentbus_client"


def _lazy_imports() -> list[tuple[str, str, int]]:
    """Every `from X import Y` that sits inside a function body.

    Module-level imports are already proven by the suite importing anything at
    all; function-level ones are the blind spot.
    """
    found: list[tuple[str, str, int]] = []
    for path in sorted(ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.ImportFrom) and inner.level:
                    rel = path.relative_to(ROOT.parent)
                    pkg = ".".join(rel.with_suffix("").parts[:-1])
                    base = pkg
                    for alias in inner.names:
                        found.append(
                            (
                                base,
                                "." * inner.level + (inner.module or "") + f".{alias.name}",
                                inner.lineno,
                            )
                        )
    return found


def test_there_are_lazy_imports_to_check():
    """KNOWN-POSITIVE for the collector itself.

    If `_lazy_imports()` silently returned nothing — a walk that matched no
    nodes — every assertion below would pass vacuously and this file would
    report a clean bill of health while checking nothing.
    """
    assert len(_lazy_imports()) > 10, "the AST walk found almost nothing; it is broken"


@pytest.mark.parametrize("pkg,target,lineno", _lazy_imports())
def test_every_lazy_import_resolves(pkg, target, lineno):
    """Execute it. A function-level import is dead code until someone runs the
    verb, which is exactly how `verify-sender` shipped broken."""
    module, _, name = target.rpartition(".")
    level = len(module) - len(module.lstrip("."))
    mod_name = module.lstrip(".")
    try:
        resolved = importlib.import_module(
            ("." * level) + mod_name if mod_name else ("." * level), package=pkg
        )
    except ImportError as exc:  # pragma: no cover - the failure we are hunting
        pytest.fail(f"{pkg} line {lineno}: `from {module} import {name}` -> {exc}")
    if not hasattr(resolved, name) and not mod_name.endswith(name):
        try:
            importlib.import_module(f"{resolved.__name__}.{name}")
        except ImportError as exc:  # pragma: no cover
            pytest.fail(f"{pkg} line {lineno}: `{name}` not in {resolved.__name__}: {exc}")


def test_room_history_unseals_like_thread_does():
    """BUG: `history --json` returned raw age armor while every other read path
    rendered prose. Silent — the call SUCCEEDS and the body is unusable, which
    is worse than an error because nothing signals it.

    Reported by bikeroom-freebsd-operato-b124c2, who isolated it to the CLI side.

    Asserted on BOTH twins: async `read` once skipped unsealing entirely and a
    caller switching to async silently got ciphertext. Same defect, same file
    pair, so the parity is asserted rather than assumed.
    """
    import inspect

    from agentbus_client.client import async_misc, sync_misc

    for module in (sync_misc, async_misc):
        src = (
            inspect.getsource(
                module.__dict__["AsyncMiscMixin" if module is async_misc else "SyncMiscMixin"]
            )
            if any(k.endswith("MiscMixin") for k in module.__dict__)
            else inspect.getsource(module)
        )
        i = src.index("room_history")
        window = src[i : src.index("def ", i + 200)]
        assert "unseal_message" in window, (
            f"{module.__name__}.room_history does not unseal; --json will leak "
            f"age armor to the caller"
        )
