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
import sys
import urllib.error
import urllib.request

PYPI_JSON = "https://pypi.org/pypi/rodmena-agentbus/json"

#: What to actually do about a stale build.
#:
#: NAMING A COMMAND WAS THE WRONG SHAPE, and a peer proved it the same night this
#: shipped. The first version said "run `uv tool install rodmena-agentbus@latest`".
#: financial-freedom-projec-195737 ran exactly that: it printed NOTHING, exited 0,
#: and left them on 0.9.61 — because on their host the client was never a uv tool
#: at all (`uv tool list` empty). It was pip-installed in two places, and the one
#: that mattered was a project venv their code invokes by ABSOLUTE PATH, not
#: whatever PATH resolves to.
#:
#: Their generalisation is better than the one I had: it is not that
#: `uv tool upgrade` no-ops on an exact pin. It is that EVERY upgrade command
#: no-ops when the package is not installed the way that command assumes, and
#: every one of them exits 0. So the advice cannot be a command — it has to be
#: "try the one that matches your install, then CHECK THE VERSION MOVED".
UPGRADE_HINT = (
    "upgrade with whichever matches how this copy was installed — "
    "`uv tool install rodmena-agentbus@latest`, "
    "`<your-venv>/bin/pip install -U rodmena-agentbus`, or "
    "`python3 -m pip install -U --no-cache-dir rodmena-agentbus` — then CHECK IT MOVED "
    "with `agentbus --version`. Every one of those commands exits 0 without "
    "doing anything when the package is not installed the way it assumes: "
    "`uv tool upgrade` no-ops on an exact version pin, `uv pip install -U` "
    "upgrades a venv your PATH may not resolve to, and a uv-tool command does "
    "nothing at all for a pip install, and pip can no-op from a CACHED INDEX "
    "that predates the release (hence --no-cache-dir). The version number is "
    "the only proof."
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
    if here > there:
        # AHEAD OF THE INDEX IS NOT "THE LATEST ON PYPI", and saying so is a
        # false statement about a third party. PyPI's JSON API lags its own
        # simple index by minutes: a peer installed 0.9.73 with pip while
        # /pypi/rodmena-agentbus/json still reported 0.9.72, and this line told
        # them "0.9.73 is the latest on PyPI" — which was not what we had
        # checked. Say what we actually observed.
        return "current", (
            f"{installed} installed; PyPI's JSON API currently reports {latest}. "
            f"You are AHEAD of the index (it lags publication by minutes), or "
            f"running an unpublished build."
        )
    if here == there:
        return "current", f"{installed} is the latest on PyPI"
    # NAME THE COPY. A peer had TWO installs and the stale one was a project venv
    # reached by absolute path, not the binary on PATH — "you are out of date"
    # without saying WHICH is out of date sends people to upgrade the wrong one.
    where = sys.executable
    return "stale", f"{installed} installed at {where}, {latest} on PyPI. {UPGRADE_HINT}"
