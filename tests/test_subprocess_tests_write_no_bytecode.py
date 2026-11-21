from __future__ import annotations

import os
import subprocess
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


def test_a_child_python_inherits_dont_write_bytecode() -> None:
    out = subprocess.run(
        [sys.executable, "-c", "import sys; print(sys.flags.dont_write_bytecode)"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        env={**os.environ},
    ).stdout.strip()
    assert out == "1"


def test_a_child_import_leaves_no_pycache(tmp_path: Path) -> None:
    (tmp_path / "probe_mod.py").write_text("VALUE = 1\n")
    subprocess.run(
        [sys.executable, "-c", "import probe_mod"],
        cwd=tmp_path,
        timeout=60,
        check=True,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
    )
    assert not (tmp_path / "__pycache__").exists()
