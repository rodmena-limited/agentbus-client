from __future__ import annotations


def _settings_agent(settings: object) -> str | None:
    env = settings.get("env") if isinstance(settings, dict) else None
    name = env.get("AGENTBUS_AGENT") if isinstance(env, dict) else None
    return name if isinstance(name, str) and name else None
