"""audit/evaluations/_python.sh picks the interpreter for run_all.sh and the shell probes.

A linked git worktree has no .venv of its own, and /usr/bin/python3 lacks the client's
dependencies, so the worktree uses the virtualenv of the checkout it was added from.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

HELPER = Path(__file__).resolve().parents[1] / "audit" / "evaluations" / "_python.sh"


def _resolve(repo: Path, **env: str) -> str:
    done = subprocess.run(
        ["sh", "-c", f'. "{HELPER}"; audit_python "$1"', "sh", str(repo)],
        capture_output=True,
        text=True,
        env={"PATH": os.environ["PATH"], **env},
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _venv(root: Path) -> Path:
    python = root / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n")
    python.chmod(0o755)
    return python


def _git(cwd: Path, *argv: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *argv],
        cwd=cwd,
        capture_output=True,
        check=True,
    )


def _main_with_linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    main = (tmp_path / "main").resolve()
    main.mkdir()
    _git(main, "init", "-q")
    _git(main, "commit", "-q", "--allow-empty", "-m", "start")
    linked = (tmp_path / "linked").resolve()
    _git(main, "worktree", "add", "-q", str(linked))
    return main, linked


def test_an_explicit_python_wins_over_a_venv(tmp_path: Path) -> None:
    _venv(tmp_path)
    assert _resolve(tmp_path, PYTHON="/opt/chosen/python") == "/opt/chosen/python"


def test_a_checkout_uses_its_own_venv(tmp_path: Path) -> None:
    python = _venv(tmp_path)
    assert _resolve(tmp_path) == str(python)


def test_a_linked_worktree_uses_the_main_checkouts_venv(tmp_path: Path) -> None:
    main, linked = _main_with_linked_worktree(tmp_path)
    python = _venv(main)
    assert _resolve(linked) == str(python)


def test_a_linked_worktree_prefers_a_venv_of_its_own(tmp_path: Path) -> None:
    main, linked = _main_with_linked_worktree(tmp_path)
    _venv(main)
    own = _venv(linked)
    assert _resolve(linked) == str(own)


def test_with_no_venv_anywhere_it_falls_back_to_python3(tmp_path: Path) -> None:
    assert _resolve(tmp_path) == "python3"
