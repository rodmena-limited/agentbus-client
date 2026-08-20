#!/usr/bin/env python3
"""CLAIM (async_messaging): AsyncAgentBus.read()/thread() unseal like the sync client. FALSIFIED
2026-08-20: unseal_message delegates to `AgentBus.unseal_message`, a name the module never
imports, so every async read()/thread() raises NameError (since the client.py split, #19)."""
from _common import SRC, verdict
from agentbus_client.client import AsyncAgentBus
b = AsyncAgentBus(api_key="ab_sk_probe", base_url="https://probe.invalid", agent="probe")
try:
    out = b.unseal_message({"text_body": "plain"}); print(f"unseal_message -> {out}"); ok = True
except Exception as e:
    print(f"unseal_message -> {type(e).__name__}: {e}"); ok = False
raise SystemExit(verdict(ok, "AsyncAgentBus.read()/thread() crash with NameError"))
