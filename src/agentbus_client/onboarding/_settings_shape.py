from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


def _object_field(data: dict[str, Any], field: str, path: Path) -> dict[str, Any]:
    value = data.get(field)
    if value is None:
        value = data[field] = {}
    if not isinstance(value, dict):
        msg = (
            f'refusing to touch {path}: "{field}" is not a JSON object. '
            "Fix that value, then re-run setup."
        )
        raise SystemExit(msg)
    return value


def _settings_agent(settings: dict[str, Any]) -> str | None:
    env = settings.get("env")
    name = env.get("AGENTBUS_AGENT") if isinstance(env, dict) else None
    return name if isinstance(name, str) and name else None


def _command_hooks(hooks: object, event: str) -> list[dict[str, Any]]:
    groups = hooks.get(event) if isinstance(hooks, dict) else None
    handlers: list[dict[str, Any]] = []
    for group in groups if isinstance(groups, list) else []:
        listed = group.get("hooks") if isinstance(group, dict) else None
        if isinstance(listed, list):
            handlers.extend(handler for handler in listed if isinstance(handler, dict))
    return handlers


def _read_object_or_empty(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}
