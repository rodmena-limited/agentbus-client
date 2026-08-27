"""#54: the version this package REPORTS must match the version it IS.

`__version__` resolves through `importlib.metadata`, i.e. the INSTALLED dist
metadata — deliberately, because a hardcoded literal here once drifted three
releases behind the published package. Metadata is the better source, and it has
exactly one failure mode: an editable install whose metadata was written at
`pip install -e` time and never refreshed.

FOUND 2026-08-27, in this repo, at 22 releases of drift: pyproject said 0.9.66
and the venv reported 0.9.44. The code imported from src/ was current the whole
time, so every test passed — only the number was wrong.

WHY THAT IS NOT COSMETIC. `_watch_state.py` stamps `client_version` into the
watcher's state file from this value, and `doctor --wake` READS THAT FIELD to
decide whether a running watcher is stale. A stale install therefore feeds the
staleness detector a wrong version: the instrument for detecting old code is
itself told the wrong version by old metadata.

The same class cost a peer three weeks of "verified against the client" that
actually meant a client 43 releases old, in a venv nothing had refreshed.

This guard fails exactly when the working environment is lying about itself, and
passes in CI where the install is fresh.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    text = (ROOT / "pyproject.toml").read_text()
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert m, "no version in pyproject.toml"
    return m.group(1)


def test_pyproject_declares_a_version():
    """KNOWN-POSITIVE: if the parse silently returned nothing, the comparison
    below would be vacuous rather than false."""
    v = _pyproject_version()
    assert re.fullmatch(r"\d+\.\d+\.\d+", v), v


def test_the_reported_version_matches_the_declared_one():
    """THE DRIFT. A mismatch means this environment reports a version it is not."""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as pkg_version

    try:
        installed = pkg_version("rodmena-agentbus")
    except PackageNotFoundError:
        pytest.skip("package not installed in this environment; nothing to drift")

    declared = _pyproject_version()
    assert installed == declared, (
        f"installed metadata says {installed}, pyproject says {declared}. "
        "The code may still be current — only the NUMBER is stale, which is "
        "worse, because it is what watcher state files and `doctor --wake` "
        "report. Refresh with: uv pip install -e . --python .venv/bin/python"
    )


def test_dunder_version_agrees_with_the_metadata():
    """__version__ must not acquire a second source of truth."""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as pkg_version

    import agentbus_client

    try:
        installed = pkg_version("rodmena-agentbus")
    except PackageNotFoundError:
        pytest.skip("package not installed in this environment")
    assert agentbus_client.__version__ == installed
