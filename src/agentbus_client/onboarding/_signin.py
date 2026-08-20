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

import argparse
import json
from typing import Any

from ..client import AgentBus, AgentBusError
from ._paths import _config_dir, _keys_dir, _say, _signin_state_path, _write_private

# ---------------------------------------------------------------- signin


def _ensure_sealing_key(bus: Any, ui: Any) -> None:
    """Provision this machine's sealing key when the workspace is encrypted (#189).

    ONE EXTRA LINE OF OUTPUT AND NOTHING ELSE TO DO. Farshid's requirement was
    that a customer sees no new complexity: the install is the same single curl
    and the same signin, and if the workspace happens to be sealed the key is
    generated here, registered, and never mentioned again.

    SILENT WHEN THE WORKSPACE IS NOT ENCRYPTED. Generating a key nobody uses
    would leave an unexplained secret on disk, and an operator auditing the box
    later would rightly ask what it was for.
    """
    from .. import sealing

    try:
        state = bus._request("GET", "/v1/workspace/pubkeys")
    except Exception:
        # Not an error: this machine is talking to a deployment that predates
        # encryption, and signin must still work against it.
        return
    if not state.get("encrypted"):
        return

    if not bus.agent:
        # An unbound operator key has no agent to publish a key FOR. The key is
        # provisioned by `agentbus setup` in each project instead, where the
        # identity exists. Saying so beats a confusing 404.
        ui.item("encrypted workspace", "sealing key is provisioned per agent by `agentbus setup`")
        return

    private, public = sealing.ensure_keypair(bus.agent)
    del private  # never transmitted, never logged, never returned
    try:
        registered = bus._request(
            "POST", f"/v1/agents/{bus.agent}/pubkey", json={"public_key": public}
        )
    except Exception as exc:
        ui.fail(f"this workspace is ENCRYPTED but the public key could not be registered: {exc}")
        _say("  Until it is, this agent cannot read sealed mail and peers cannot seal to it.")
        return
    ui.ok("encrypted workspace — sealing key ready")
    ui.item("fingerprint", str(registered.get("fingerprint")))
    ui.item("private key", f"{sealing.key_path(bus.agent)}  (0600, never leaves this machine)")


def cmd_signin(args: argparse.Namespace) -> int:
    """Validate the key against the live service BEFORE storing anything."""
    from .. import ui

    key = args.key.strip()
    if not key.startswith("ab_sk_"):
        ui.fail("that does not look like an AgentBus key (expected ab_sk_...). Nothing stored.")
        return 1

    ui.banner("sign in — once per machine")
    bus = AgentBus(api_key=key, base_url=args.base_url)
    try:
        who = bus.whoami()
    except AgentBusError as exc:
        ui.fail(f"key REFUSED by {bus.base_url}: {exc}")
        _say("Nothing was stored. Check the key (revoked? truncated? wrong service?).")
        return 1

    key_info = who.get("key") or {}
    workspace = who.get("workspace") or {}
    scope = key_info.get("scope")
    bound = key_info.get("bound_agents") or []

    ui.ok(f"key VERIFIED against {bus.base_url}")
    ui.item("workspace", f"{workspace.get('slug')} ({workspace.get('id')})")
    _ensure_sealing_key(bus, ui)
    ui.item("scope", f"{scope}  (scopes are cumulative — every scope may read)")
    ui.item("bound to", ", ".join(bound) if bound else "(unbound — operator credential)")

    if len(bound) == 1:
        agent = bound[0]
        path = _keys_dir() / f"{agent}.env"
        changed = _write_private(
            path, f"export AGENTBUS_API_KEY={key}\nexport AGENTBUS_AGENT={agent}\n"
        )
        _write_private(
            _signin_state_path(), json.dumps({"default_agent": agent, "operator": False}) + "\n"
        )
        _say(f"  stored:    {path} (0600){'' if changed else '  [unchanged]'}")
        _say("")
        _say(f"Next: cd <your-project> && agentbus setup claude   # wires everything for '{agent}'")
        _say("")
        _say(f"NOTE: this key is BOUND to '{agent}', so it can only ever serve that")
        _say("one agent. `agentbus setup` will work in that agent's project and will")
        _say("REFUSE elsewhere, because a bound key cannot provision anyone else.")
        _say("Running several agents on this machine? Sign in with the WORKSPACE key")
        _say("instead — setup then provisions each project its own agent and its own")
        _say("bound key automatically, and you never type a per-agent secret.")
    elif bound:
        for agent in bound:
            path = _keys_dir() / f"{agent}.env"
            _write_private(path, f"export AGENTBUS_API_KEY={key}\nexport AGENTBUS_AGENT={agent}\n")
            _say(f"  stored:    {path} (0600)")
        _write_private(
            _signin_state_path(), json.dumps({"default_agent": None, "operator": False}) + "\n"
        )
        _say("Next: agentbus setup claude --role <which>   # the key binds several agents")
    else:
        path = _config_dir() / "operator.env"
        changed = _write_private(path, f"export AGENTBUS_API_KEY={key}\n")
        _write_private(
            _signin_state_path(), json.dumps({"default_agent": None, "operator": True}) + "\n"
        )
        ui.item("stored", f"{path} (0600){'' if changed else '  [unchanged]'}")
        _say("")
        _say("Operator credential: setup mints each project its own bound key —")
        _say("you never type a per-agent secret.")
        ui.next_steps("cd <your-project>", "agentbus setup claude")
    return 0


def _sealing_publish_with_retry(
    bus: Any, agent: str, public_key: str, attempts: int = 3
) -> dict[str, Any] | None:
    """POST the sealing pubkey with short exponential backoff.

    Extracted for testability. Retries transient failures (typically a race
    between the newly-minted bound key being usable and the server's read
    path for that key), returns the successful registration dict or None
    when every attempt failed.

    Kept small on purpose — this is on the setup hot path and every second
    of backoff is one an operator waits before their prompt returns. Three
    attempts at 0 / 0.4 / 1.0 s cover the propagation window observed in
    probe reports without adding noticeable latency to the common case
    (attempt 0 succeeds).
    """
    import time as _time

    # attempt 0 no wait; attempts 1 and 2 wait longer than the previous —
    # sanity-tested by test_backoff_delays_grow so a refactor cannot flatten
    # the wait to zero and turn the retry into a tight loop against a
    # struggling server.
    delays = (0.0, 0.4, 1.0)
    for i in range(attempts):
        wait = delays[min(i, len(delays) - 1)]
        if wait > 0:
            _time.sleep(wait)
        try:
            return bus._request(
                "POST", f"/v1/agents/{agent}/pubkey", json={"public_key": public_key}
            )
        except Exception:  # retry every failure shape
            continue
    # Signal failure via return None (caller decides how loud); avoid raising
    # here because the whole point is to keep setup running with a visible
    # warning instead of crashing.
    return None


# ------------------------------------------------------------------ identity
#
# DEPRECATED 2026-08-10 (operator directive): the sibling machinery was the
# wrong answer. Identity is env-var-driven — `AGENTBUS_AGENT` in the project's
# .env, or exported for one command — and a customer who wants two agents on
# one checkout should use a git worktree or a clone, which gives each its own
# directory and therefore its own identity for free. The `sibling add/list/as`
# verbs are retained only to say so, never to act.


def _mint_bound_key(name: str, operator: str | None, base_url: str | None) -> str | None:
    """A `send` key bound to `name` alone, or None with the reason printed."""
    if operator is None:
        _say(f"'{name}' has no key file and there is no operator credential to mint one.")
        _say("  Sign in with the workspace key once: agentbus signin <key>")
        return None
    try:
        minted = AgentBus(api_key=operator, base_url=base_url).mint_key(
            scope="send", agents=[name], label=f"sibling-{name}"
        )
    except AgentBusError as exc:
        _say(f"could not mint a bound key for '{name}': {exc}")
        return None
    secret = minted.get("key") or minted.get("api_key")
    if not secret:
        _say("mint succeeded but no secret in the response; refusing to continue.")
        return None
    return str(secret)
