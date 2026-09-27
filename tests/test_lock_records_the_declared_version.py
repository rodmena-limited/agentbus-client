"""#69: uv.lock must record this project at the version pyproject declares.

Through the 0.9.97 and 0.9.98 releases the lock still said 0.9.96, because
neither version bump re-locked. The lock is what provenance and SBOM tooling
read, so every record generated from it misstated this component's own version.
Nothing failed: the drift is invisible to the code and only visible to the
scanner.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if sys.version_info < (3, 11):
    import tomli as tomllib  # type: ignore[import-not-found,unused-ignore]
else:
    import tomllib


def _declared() -> str:
    m = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M)
    assert m, "no version in pyproject.toml"
    return m.group(1)


def _locked() -> str | None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    for pkg in lock["package"]:
        if pkg["name"] == "rodmena-agentbus":
            return str(pkg["version"])
    return None


def test_the_lock_contains_this_project():
    """KNOWN-POSITIVE: if the lookup found nothing, the comparison below would be
    None != version — a failure for the wrong reason, or, written carelessly, a
    silent pass."""
    assert _locked() is not None


def test_the_locked_version_matches_the_declared_one():
    assert _locked() == _declared(), "bumped the version without re-running `uv lock`"
