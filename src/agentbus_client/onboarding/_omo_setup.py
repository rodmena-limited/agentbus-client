from __future__ import annotations

from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

from agentbus_client._omo import omo_skill_path

from ._agy_setup import _publish_sealing_key, _warn_shared_identity
from ._identity import _write_worktree_identity
from ._omo_extension import install_extension
from ._paths import _git_root_or_none, _say
from ._provision import _provision_project_agent

if TYPE_CHECKING:
    import argparse

_SKILL_HARNESS = "omo"
_MIN_SKILL_BYTES = 500


def _install_skill(base_url: str, report: list[str]) -> None:
    import httpx

    path = omo_skill_path()
    try:
        resp = httpx.get(f"{base_url}/skills/index.json", timeout=10)
        if resp.status_code != HTTPStatus.OK:
            report.append(f"skill: NOT installed; /skills/index.json answered {resp.status_code}")
            return
        index = resp.json()
        harnesses = set(index.get("harnesses") or [])
        aliases = index.get("aliases") or {}
        flavour = next(
            (n for n in (_SKILL_HARNESS, aliases.get(_SKILL_HARNESS)) if n in harnesses), None
        )
        if flavour is None:
            report.append(
                "skill: none installed. /skills/index.json lists no omo flavour "
                f"(it lists {', '.join(sorted(harnesses)) or 'none'}). omo runs without "
                "it, and this line asks the index again on every setup."
            )
            return
        body = httpx.get(f"{base_url}/skills/{flavour}.md", timeout=10)
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        report.append(f"skill: NOT installed ({type(exc).__name__}: {exc})")
        return
    if body.status_code != HTTPStatus.OK or len(body.content) <= _MIN_SKILL_BYTES:
        report.append(
            f"skill: NOT installed; /skills/{flavour}.md answered {body.status_code} "
            f"with {len(body.content)} bytes"
        )
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.replace(path.with_name("SKILL.md.bak"))
    path.write_bytes(body.content)
    report.append(f"skill: installed /skills/{flavour}.md ({len(body.content)} bytes) at {path}")


def _setup_omo(args: argparse.Namespace) -> int:
    report: list[str] = []
    base_url = (args.base_url or "https://agentbus.rodmena.co.uk").rstrip("/")
    name = _provision_project_agent(args, report, "omo")
    if name is None:
        return 1
    _write_worktree_identity(name, report)
    root = _git_root_or_none() or Path.cwd()
    _warn_shared_identity(root, name, report)
    extension_ok = install_extension(report)
    _publish_sealing_key(base_url, name, report)
    _install_skill(base_url, report)
    report.append(
        "watcher: one per identity. While an omo session holds "
        f"~/.config/agentbus/omo-watch-{name}.lock, `agentbus watch` for {name} "
        "refuses to start (exit 9), so a Claude Code session in this directory "
        "stays passive. Use a git worktree for a second concurrent session."
    )
    report.append("mcp: not configured. omo uses the agentbus CLI and agentbus-hook.")
    _say("agentbus setup omo:")
    for line in report:
        _say(f"  {line}")
    _say("")
    _say("Verify it with:")
    _say("  omo                     # the footer shows 'bus: <agent>' once the stream is up")
    _say(f"  agentbus watch-status --agent {name}")
    return 0 if extension_ok else 1
