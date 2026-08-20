"""`agentbus signin`, `agentbus setup <harness>`, `agentbus doctor --wake`.

SPECS/0021: onboarding is three commands, and everything a host needs — the
credential, the per-project identity, both passive hooks, and the ACTIVE
Stop re-waker — is generated, verified, and idempotent. Nothing is inlined,
nothing is guessed, and setup never touches configuration it did not write:
our entries are recognized by their own content (commands that invoke
agentbus tooling), never by position.

Every rule encoded here was a real failure first, on the platform's own
hosts, in one night:

  * a key inlined into a hook command outlived its rotation;
  * an agent name guessed from a directory acted as an agent that did not
    exist, silently;
  * a key file sourced without `set -a` left the credential unexported and
    the hook looked wired while printing nothing;
  * a by-the-book install with only passive hooks was structurally deaf; and
  * the re-waker that fixes that loops forever unless it dedupes on delivery
    ids, because unread-but-unacked mail is a permanent wake source.
"""

from __future__ import annotations

import contextlib
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any

from ..identity import config_dir as identity_config_dir


def _device_hash(device_id: str | None) -> str:
    """Hash a device id the way the phonebook publishes it (#74).

    The phonebook emits `device_hash`, not the raw id: the raw id is one of the
    three inputs to session_key = sha256(device_id : repo_fingerprint :
    path_hash), so publishing it handed every workspace member material that
    DEFINES another agent's identity.

    Equality is all this guard ever needed, so a hash serves it exactly as well.
    """
    import hashlib

    return hashlib.sha256((device_id or "").encode()).hexdigest()


HARNESSES = ("claude", "opencode", "codex", "agy")

# Recognition markers: an entry in a harness config is OURS iff its command
# contains one of these. This is what makes setup safe to re-run and safe to
# run beside anyone else's hooks.
_MARKER_HOOK = "agentbus-hook"
_MARKER_REWAKE = "stop-rewake.sh"

# REG-8d SHELL SIBLING (audit 0.9.40): the agent name is interpolated into a
# shell path below. A hostile `.claude/settings.local.json` or `.agentbus/agent`
# can set AGENTBUS_AGENT to `../operator`, which would source
# $HOME/.config/agentbus/operator.env — the OPERATOR key that can MINT a bound
# key for any agent. The Python-side `_agent_key` was sanitized through
# bound_env_filename (REG-8d); this shell hook path was NOT and is the same
# escalation, running on EVERY session start. Guard: reject any agent name
# containing a character outside [a-zA-Z0-9._-] (the same set bound_env_filename
# allows). Anything else — `/`, `..`, `$`, backticks — exits 0 (no key sourced).
_SESSION_START_CMD = (
    '[ -n "${AGENTBUS_AGENT:-}" ] || exit 0; '
    'case "$AGENTBUS_AGENT" in *[!a-zA-Z0-9._-]*) exit 0;; esac; '
    '[ -n "${AGENTBUS_API_KEY:-}" ] || { set -a; '
    '[ -f "${AGENTBUS_CONFIG_DIR:-$HOME/.config/agentbus}/keys/${AGENTBUS_AGENT}.env" ] && . "${AGENTBUS_CONFIG_DIR:-$HOME/.config/agentbus}/keys/${AGENTBUS_AGENT}.env" 2>/dev/null; set +a; } || true; '
    "agentbus-hook session-start || true"
)
_PENDING_CMD = _SESSION_START_CMD.replace("session-start", "pending")

# The re-waker's arming window AND the harness hook timeout are DERIVED FROM ONE
# NUMBER, on purpose. They were two constants in two files — window=600 in
# rewake.py, timeout=15 in the emitted hook — and they drifted 40x apart: the
# harness killed the monitor at 15s while its window was 600s, so it was inert
# for anything arriving after the first 15 seconds and only ever caught mail
# already waiting at turn-end. A monitor the harness kills before its window
# opens is not a monitor (david D10). The hook timeout MUST exceed the window,
# or the monitor is executed for a fraction of the time it was built to run.
# 600s is Claude Code's DOCUMENTED practical maximum for a command hook (and its
# default). Sizing the timeout ABOVE it — the old 660 — risks the harness
# clamping it back to 600, which would silently make timeout == window and
# violate the very invariant D10 established, in a way the probe cannot detect
# because it reads the number we EMITTED, not the number the harness ENFORCED.
# So keep BOTH under the ceiling: window 60s below the timeout, timeout exactly
# at the documented maximum. (david's catch.)
REWAKE_WINDOW_SEC = 540
REWAKE_HOOK_TIMEOUT_SEC = 600  # invariant: strictly greater
# The Stop command injects the window so the value the monitor uses and the
# value the timeout is sized against come from the SAME source and cannot drift.
_STOP_CMD = (
    '[ -n "${AGENTBUS_AGENT:-}" ] && '
    f"AGENTBUS_REWAKE_WINDOW={REWAKE_WINDOW_SEC} "
    '"${AGENTBUS_CONFIG_DIR:-$HOME/.config/agentbus}/stop-rewake.sh"'
)

# The Stop re-waker. Load-bearing properties, each a real failure first:
# dedupe on DELIVERY IDS never on "output exists" (idempotent pending makes
# unread-but-unacked mail a permanent wake source, so "output exists" loops
# forever); hash fallback so a format change degrades to one wake per message
# instead of a loop or silence; credential resolved FROM the agent name;
# `set -a` around the sourcing; AGENTBUS_REWAKE_STATE override so `doctor
# --wake` never poisons the production ledger.
#
# v2 (the reason the first version kept missing messages): the old script
# CHECKED ONCE and exited. As an asyncRewake Stop hook that fires at TURN END,
# check-once could only wake for mail already waiting when the turn finished —
# anything arriving one minute later sat until a human typed. v2 LONG-POLLS:
# after the turn ends it stays armed for a bounded window, polling every
# interval, and exits 2 the moment a genuinely new delivery lands. The window
# is bounded on purpose — a truly idle session eventually goes quiet, which is
# honest, and permanent reachability needs a supervised injector, not a hook.
#
# STOP_REWAKE_VERSION is stamped so `doctor --wake` can refuse to trust a stale
# copy left behind by a client upgrade (david D9).
STOP_REWAKE_VERSION = 4
STOP_REWAKE_SH = r"""#!/bin/sh
# Stop-hook re-wake for AgentBus (installed by `agentbus setup`; SPECS/0021).
# agentbus-rewake-version: 4
#
# THIN wrapper on purpose. Its whole job is the one thing shell does better than
# Python — resolve the per-agent credential with `set -a` sourcing so the child
# inherits an EXPORTED key (an unexported key is the classic wired-but-silent
# failure) — and then hand off to the resilient monitor, which long-polls for a
# bounded window and survives a laptop: wifi drops, DNS loss, suspend/resume,
# via retry+circuit-breaker+failsafe (resilient-circuit). Exit 2 = new mail,
# re-wake the session; exit 0 = nothing, stay idle. It never exits non-zero for
# any other reason, so it cannot break a session.
set -u
# REG-8d SHELL SIBLING (audit 0.9.40): same guard as _SESSION_START_CMD. A
# hostile AGENTBUS_AGENT (from .claude/settings.local.json or .agentbus/agent)
# must not be able to source $HOME/.config/agentbus/operator.env via `../operator`
# traversal. Reject any name with a character outside [a-zA-Z0-9._-].
# The config dir is overridable (AGENTBUS_CONFIG_DIR), the same way every
# Python path in the client resolves it (review #23, S4).
AGENTBUS_CFG="${AGENTBUS_CONFIG_DIR:-$HOME/.config/agentbus}"
if [ -n "${AGENTBUS_AGENT:-}" ] && case "$AGENTBUS_AGENT" in *[!a-zA-Z0-9._-]*) false;; *) true;; esac && [ -r "$AGENTBUS_CFG/keys/${AGENTBUS_AGENT}.env" ]; then
    set -a
    . "$AGENTBUS_CFG/keys/${AGENTBUS_AGENT}.env"
    set +a
else
    exit 0
fi
exec agentbus-hook monitor
"""


def _config_dir() -> Path:
    """ONE config directory, honoured by every path that reads config.

    `AGENTBUS_CONFIG_DIR` was documented as "move the whole config directory"
    and moved exactly one thing: `device-id`, which lives in identity.py and
    read the override. Identity, credentials, key files and watch state all
    hardcoded `~/.config/agentbus` and ignored it.

    david measured the consequence, and it is worse than the parts being
    inconsistent:

        AGENTBUS_CONFIG_DIR=/tmp/x agentbus identity  -> a NEW device_id
        AGENTBUS_CONFIG_DIR=/tmp/x agentbus whoami    -> the REAL agent, the
                                                         REAL key, the REAL
                                                         address

    So following the documentation produced a session presenting a fresh
    machine identity while acting with its real credential — precisely the
    mismatch identity derivation exists to prevent, reached by doing what the
    docs say. A half-isolated environment is worse than none, because it looks
    isolated.

    Second consequence, and it is why this was found: on any host that has run
    `signin`, the no-recorded-identity branch added in 0.4.10 is UNREACHABLE by
    the documented route — the signin default resolves through every override.
    That branch shipped verifiable only on hosts that happen never to have
    signed in, which is an accident of configuration and not a test.
    """
    return identity_config_dir()


def _keys_dir() -> Path:
    return _config_dir() / "keys"


def _signin_state_path() -> Path:
    return _config_dir() / "signin.json"


def _say(msg: str) -> None:
    print(msg)


def _write_private(path: Path, content: str) -> bool:
    """Write a credential-bearing file, 0600 FROM BIRTH. True if the content changed.

    Never write_text()-then-chmod (review #23, S2): that leaves the secret
    world-readable between the two calls. The fd is opened 0600 and an existing
    file's mode is tightened with fchmod before a byte is written.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.read_text() == content:
        with contextlib.suppress(OSError):
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        return False
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with contextlib.suppress(OSError):
        os.fchmod(fd, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    return True


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"refusing to touch {path}: it is not valid JSON ({exc}). "
            "A broken settings file silently disables everything in it — "
            "fix it first, then re-run setup."
        ) from exc
    if not isinstance(data, dict):
        raise SystemExit(f"refusing to touch {path}: expected a JSON object")
    return data


def _dump_json(path: Path, data: dict[str, Any], *, private: bool = False) -> None:
    """Write JSON; `private=True` for a file that carries a credential (0600 from birth)."""
    text = json.dumps(data, indent=2) + "\n"
    if private:
        _write_private(path, text)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def _git_root_or_none() -> Path | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if out.returncode != 0:
            return None
        return Path(out.stdout.strip()) if out.stdout.strip() else None
    except (OSError, subprocess.SubprocessError):
        return None


def _key_from_env_file(path: Path) -> str | None:
    for raw in path.read_text().splitlines():
        stripped = raw.removeprefix("export ").strip()
        if stripped.startswith("AGENTBUS_API_KEY="):
            return stripped.split("=", 1)[1]
    return None


def _remove_hook_entry(hooks: dict[str, Any], event: str, marker: str) -> bool:
    """Drop OUR entry for this event, leaving every foreign hook untouched.

    Idempotency has to mean CONVERGENCE, not just "adding twice is safe". When
    the plugin takes over the wiring, the copies setup wrote earlier must go —
    otherwise an upgrade path silently leaves both, and every greeting, catch-up
    and wake fires twice, which reads as the platform malfunctioning rather than
    as a stale config.
    """
    groups = hooks.get(event)
    if not isinstance(groups, list):
        return False
    changed = False
    for group in list(groups):
        kept = [h for h in group.get("hooks", []) if marker not in str(h.get("command", ""))]
        if len(kept) != len(group.get("hooks", [])):
            changed = True
            group["hooks"] = kept
        if not group.get("hooks"):
            groups.remove(group)
    if not groups:
        hooks.pop(event, None)
    return changed


def _plugin_provides_wake(settings: dict[str, Any]) -> bool:
    """True when the agentbus PLUGIN is enabled, so it owns the wake.

    The plugin ships a `monitors` entry that runs for the LIFETIME of the
    session — strictly better than a Stop hook, which Claude Code caps at its
    documented 600s and which spawns one process per turn. If both were wired
    the same message would wake the session twice, so setup defers: the plugin
    wins and the Stop hook is not written (SPECS/0022).
    """
    enabled = settings.get("enabledPlugins") or {}
    return any(str(k).split("@", 1)[0] == "agentbus" and v for k, v in enabled.items())


def _ensure_hook_entry(
    hooks: dict[str, Any], event: str, command: str, extra: dict[str, Any] | None = None
) -> str:
    """Idempotent merge of OUR hook into settings hooks, leaving all else alone.

    Returns 'added' | 'updated' | 'ok'. Ours is any command hook whose command
    carries our marker; foreign entries are never touched.
    """
    marker = _MARKER_REWAKE if _MARKER_REWAKE in command else _MARKER_HOOK
    groups = hooks.setdefault(event, [])
    if not isinstance(groups, list):
        raise SystemExit(f"hooks.{event} is not a list; refusing to touch it")
    for group in groups:
        for hook in group.get("hooks", []):
            if hook.get("type") == "command" and marker in str(hook.get("command", "")):
                changed = False
                if hook.get("command") != command:
                    hook["command"] = command
                    changed = True
                for k, v in (extra or {}).items():
                    if hook.get(k) != v:
                        hook[k] = v
                        changed = True
                return "updated" if changed else "ok"
    entry: dict[str, Any] = {"type": "command", "command": command, "timeout": 15}
    entry.update(extra or {})
    groups.append({"hooks": [entry]})
    return "added"


def _git_remote_or_none() -> str | None:
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


# The opencode plugin, referenced by its documented npm name (SPECS/0041:
# "SHALL be installed only through opencode's documented plugin mechanism
# (npm package + `plugin` array in `opencode.json`), NEVER by dropping the file
# into the global auto-load directory"). `agentbus setup opencode` writes it to
# the project's plugin array; `opencode plugin <module>` is how it lands.
OPENCODE_PLUGIN_NPM = "@rodmena/agentbus-opencode"
