"""Is the CLI on PATH actually the current release? (agentbus #342)

WHY THIS EXISTS. On 2026-08-28 a release was published, the documented upgrade
command was run, and the binary did not move:

    uv tool upgrade rodmena-agentbus
    Nothing to upgrade
    hint: `rodmena-agentbus` is pinned to `0.9.67` (installed with an exact
          version pin); reinstall with `uv tool install rodmena-agentbus@latest`

IT EXITS 0. A person or a script reading "Nothing to upgrade" concludes they are
current, and they are a release behind with the new verb missing. That is the
same family as the two traps already recorded against this client — `uv pip
install -U` upgrading a venv nothing on PATH uses, and a console script whose
shebang points into an environment you did not think you were touching. In all
three the command SUCCEEDS while achieving nothing, so only a version comparison
can tell you.

THREE STATES, NEVER TWO. "current", "stale", and "unknown" are different
answers, and `unknown` must never be rendered as `current`: a doctor that cannot
reach PyPI has not checked anything, and saying "up to date" on that basis is
the unearned-negative this project has a standing rule about. Running from a
source checkout is its own state — there is no meaningful comparison to make.

NEVER FAILS THE COMMAND. This is advisory. A network hiccup must not turn
`agentbus doctor` red, so every failure path returns "unknown" with the reason.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

PYPI_JSON = "https://pypi.org/pypi/rodmena-agentbus/json"

#: How the binary is actually moved. `uv tool upgrade` is NOT enough — it
#: declines silently when the tool was installed with an exact version pin.
UPGRADE_HINT = (
    "upgrade with `uv tool install rodmena-agentbus@latest` "
    "(if you installed it with uv tool) or `pip install -U rodmena-agentbus`. "
    "NOTE: `uv tool upgrade` prints 'Nothing to upgrade' and exits 0 when the "
    "tool carries an exact version pin, and `uv pip install -U` upgrades a venv "
    "that is not what your PATH resolves to."
)


def _parse(version: str) -> tuple[int, ...] | None:
    """`0.9.68` -> (0, 9, 68). Anything not purely numeric returns None.

    Deliberately NOT a full PEP 440 parser: this package has never shipped a
    pre-release or a local segment other than `+source`, and inventing a
    comparison for shapes we do not publish would be untested code deciding
    whether to nag a user.
    """
    core = version.split("+", 1)[0].strip()
    parts = core.split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def is_source_build(version: str | None) -> bool:
    return bool(version) and "source" in str(version)


def latest_on_pypi(timeout: float = 4.0) -> tuple[str | None, str]:
    """(version, detail). `None` means we could not tell — never 'up to date'."""
    try:
        with urllib.request.urlopen(PYPI_JSON, timeout=timeout) as response:
            if response.status != 200:
                return None, f"PyPI answered HTTP {response.status}"
            data = json.loads(response.read().decode())
    except urllib.error.URLError as exc:
        return None, f"could not reach PyPI ({exc.reason})"
    except Exception as exc:  # timeouts, malformed JSON, anything else
        return None, f"could not read PyPI ({type(exc).__name__}: {exc})"
    version = (data.get("info") or {}).get("version")
    if not isinstance(version, str) or not version:
        return None, "PyPI response carried no info.version"
    return version, "ok"


def cli_freshness(installed: str | None, timeout: float = 4.0) -> tuple[str, str]:
    """("current" | "stale" | "source" | "unknown", human-readable detail)."""
    if not installed or installed == "unknown":
        return "unknown", "this build does not report a version"
    if is_source_build(installed):
        return "source", f"running from a checkout ({installed}); nothing to compare"

    latest, detail = latest_on_pypi(timeout=timeout)
    if latest is None:
        return "unknown", f"{detail} — NOT the same as being up to date"

    here, there = _parse(installed), _parse(latest)
    if here is None or there is None:
        return "unknown", f"cannot compare {installed!r} with {latest!r}"
    if here >= there:
        return "current", f"{installed} is the latest on PyPI"
    return "stale", f"{installed} installed, {latest} on PyPI. {UPGRADE_HINT}"
