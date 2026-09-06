"""The Antigravity plugin artifact: its content, and a surgical merge (#56).

Kept apart from `_agy_setup` so the plugin's SHAPE can be tested without
argparse, a bus, or a filesystem full of credentials — the same split that lets
`_paths.STOP_REWAKE_SH` be exercised by a real `sh -c` in its own test.

WHY A SEPARATE MERGER RATHER THAN `_ensure_hook_entry`. Claude Code's
`settings.json` maps an EVENT to a list of matcher groups:

    {"hooks": {"Stop": [{"hooks": [{"command": ...}]}]}}

Antigravity's `hooks.json` maps a HOOK NAME to an object of events, and the
event's value is a flat handler list for `PreInvocation`/`PostInvocation`/`Stop`
but a matcher-group list for `PreToolUse`/`PostToolUse`:

    {"agentbus-wake": {"Stop": [{"command": ...}]}}

Two different shapes at two different depths. Reusing one merger for both would
mean a function that branches on a shape it cannot see, so this is deliberately
its own small, independently tested thing.

THE MERGE IS SURGICAL BECAUSE THE FILE IS SHARED. Antigravity merges named hooks
from every plugin and runs them sequentially, so clobbering a foreign key here
silently disables somebody else's tooling — the same law that governs every
config this client touches: our entries are recognized by their own content
(`_MARKER_HOOK`), never by position.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ._paths import (
    _MARKER_HOOK,
    AGY_WAKE_HOOK_TIMEOUT_SEC,
    AGY_WAKE_WINDOW_SEC,
)

# Our hook names in the shared `hooks.json`. Namespaced with the `agentbus-`
# prefix so a foreign hook called "wake" cannot collide with ours.
HOOK_WAKE = "agentbus-wake"
HOOK_CATCHUP = "agentbus-catchup"
OUR_HOOK_NAMES = (HOOK_WAKE, HOOK_CATCHUP)

# Stamped so a later `doctor` can refuse to trust a plugin written by an older
# client, exactly as STOP_REWAKE_VERSION does for the Claude re-waker. It lives
# in a sidecar file rather than in plugin.json, because `agy plugin validate`
# is not documented to tolerate unknown manifest keys and a validator failing on
# our own version stamp would be a self-inflicted outage.
PLUGIN_VERSION = 1
PLUGIN_VERSION_FILENAME = ".agentbus-plugin-version"


def plugin_manifest() -> dict[str, Any]:
    """`plugin.json` — the marker that makes the directory a plugin.

    `name` is optional (it defaults to the directory name) but is written
    explicitly: a plugin whose identity depends on what someone called the
    folder is a plugin that silently renames itself when moved.
    """
    return {
        "name": "agentbus",
        "description": "AgentBus — a real inbox for this agent, with an active wake lane.",
    }


def hooks_config(hook_bin: str) -> dict[str, Any]:
    """`hooks.json` — the passive catch-up lane and the active wake lane.

    `hook_bin` is an ABSOLUTE path where one could be resolved. Antigravity may
    be launched from a desktop entry whose PATH does not include the user's
    local bin, and a hook that cannot be executed is indistinguishable from an
    agent with no mail — silence either way.

    NO `PreToolUse` ENTRY, DELIBERATELY. See `_agy_setup._GATE_REFUSAL`: agy's
    decision vocabulary is not Claude's, and what agy does with a missing,
    timed-out or invalid-JSON hook is unmeasured. A gate on that footing can
    fail CLOSED and hold a session hostage, so it is absent rather than present
    and disabled — a disabled block pointing at an unimplemented subcommand is a
    trap for whoever flips it.
    """
    return {
        HOOK_CATCHUP: {
            "PreInvocation": [
                {
                    "type": "command",
                    "command": f"{hook_bin} agy-preinvocation",
                    "timeout": 15,
                }
            ]
        },
        HOOK_WAKE: {
            "Stop": [
                {
                    "type": "command",
                    "command": (
                        f"AGENTBUS_REWAKE_WINDOW={AGY_WAKE_WINDOW_SEC} {hook_bin} agy-stop"
                    ),
                    "timeout": AGY_WAKE_HOOK_TIMEOUT_SEC,
                }
            ]
        },
    }


def mcp_config(url: str, key: str) -> dict[str, Any]:
    """`mcp_config.json` — the exact shape `agy mcp add --header` writes.

    DERIVED, NOT GUESSED. The embedded MCP schema documents `command`/`args`/
    `env` for stdio and `serverUrl` for remote, and omits `headers` entirely —
    so reading the docs would have produced a wrong-shaped file that fails
    silently as "no tools". Running

        HOME=$(mktemp -d) agy mcp add --header "Authorization: Bearer X" \\
            agentbus https://agentbus.rodmena.co.uk/mcp/

    writes `{"disabled": false, "headers": {...}, "serverUrl": "..."}`, and that
    is what this reproduces.

    This file is the ONLY part of the plugin carrying a credential, which is why
    it is the only path added to `.gitignore` and why it is written 0600 from
    birth by the caller.
    """
    return {
        "mcpServers": {
            "agentbus": {
                "disabled": False,
                "headers": {"Authorization": f"Bearer {key}"},
                "serverUrl": url,
            }
        }
    }


# Recognition markers. `_MARKER_HOOK` alone is NOT enough here, and a test
# caught it: the command embeds an ABSOLUTE path resolved by `shutil.which`, so
# recognition keyed only on the string "agentbus-hook" silently fails the moment
# the binary is installed, symlinked or vendored under any other name — and a
# hook we cannot recognize is one teardown orphans and setup duplicates. The
# SUBCOMMANDS are ours no matter what the binary is called, so they are the
# durable half of the marker.
_OUR_MARKERS = (_MARKER_HOOK, "agy-stop", "agy-preinvocation")


def _is_ours(entry: Any) -> bool:
    """A hooks.json entry is ours iff a handler invokes agentbus tooling.

    Recognition by CONTENT, never by name or position — so an operator who
    renamed our hook still gets it updated rather than duplicated, and a foreign
    hook that happens to be called `agentbus-wake` is not silently overwritten
    on the strength of its name alone.
    """
    if not isinstance(entry, dict):
        return False
    for handlers in entry.values():
        if not isinstance(handlers, list):
            continue
        for handler in handlers:
            if not isinstance(handler, dict):
                continue
            command = str(handler.get("command", ""))
            if any(marker in command for marker in _OUR_MARKERS):
                return True
    return False


def ensure_hooks(existing: dict[str, Any], desired: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Merge our named hooks into `existing`, returning (merged, state).

    State is "ok" when nothing changed — the caller must not rewrite the file in
    that case, so that a re-run is a genuine no-op on disk rather than a
    same-bytes rewrite that still churns the mtime.

    Foreign keys survive byte-for-byte: we only ever touch keys we recognize as
    ours, and we replace them wholesale so a constant change (a new window, a
    moved binary) actually lands instead of half-updating.
    """
    merged = dict(existing)
    changed = False
    for name, block in desired.items():
        if merged.get(name) != block:
            merged[name] = block
            changed = True
    # An older client may have written our hooks under different names. Drop any
    # OTHER entry that is recognizably ours, or a rename in this module would
    # leave the previous name behind and every wake would fire twice.
    for name in [n for n in merged if n not in desired and _is_ours(merged.get(n))]:
        del merged[name]
        changed = True
    return merged, ("updated" if changed else "ok")


def remove_hooks(existing: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """Drop every entry of ours; leave everything else untouched. For teardown."""
    remaining = {n: e for n, e in existing.items() if not _is_ours(e)}
    return remaining, len(remaining) != len(existing)


def load_hooks(path: Path) -> dict[str, Any]:
    """Read a hooks.json, treating an empty or absent file as {}.

    An EMPTY file is not malformed here, and that distinction is load-bearing on
    this very machine: `~/.gemini/config/mcp_config.json` is zero bytes, and the
    shared `_load_json` raises SystemExit on that rather than guessing. For a
    file we are merging into, empty unambiguously means "nothing configured".
    """
    if not path.is_file():
        return {}
    raw = path.read_text(encoding="utf-8").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"refusing to touch {path}: it is not valid JSON ({exc})") from None
    if not isinstance(data, dict):
        raise SystemExit(f"refusing to touch {path}: expected a JSON object")
    return data
