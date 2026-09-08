"""`agentbus doctor --wake` for the Antigravity harness (#56).

WHY THIS EXISTS, and it is not a general wish for more diagnostics. Wiring this
harness correctly and then seeing nothing is a two-hour failure, and it happened
exactly that way with an operator watching: setup reported a clean wire-up, the
hooks were installed and valid, `agy plugin validate` said `[ok]`, and mail never
surfaced. Every individual check was green. The one question nobody could ask
was "what does the hook actually resolve when it runs here", and that is the only
question that would have answered it.

So this probe RUNS THE HOOK. Not "is the file present", not "is the JSON valid" —
it feeds the real binary a real payload and reports the agent it came back with.
A check that inspects configuration can only confirm the shape of what we wrote;
this one observes the effect, which is where every failure in that two hours
actually lived.

THE THREE THINGS IT CAN CATCH, each a real failure already seen:

  * the hook binary named in hooks.json no longer exists (a pip upgrade moved it,
    or setup ran from a shell whose PATH found a different copy). Silent: a hook
    that cannot execute is indistinguishable from an agent with no mail.
  * this checkout never opted in, so the machine-wide hooks correctly ignore it.
    Silent for the same reason.
  * the hook resolves a DIFFERENT agent than this checkout declares — which is
    what actually happened, from an inherited `AGENTBUS_AGENT`. It polled another
    agent's inbox and reported no mail while this project's own mail sat unread.

And the one thing it cannot catch, stated rather than glossed: whether the
operator's real agy session will have a workspace. `workspacePaths` is empty
unless agy has one, and this probe supplies its own. See `_WORKSPACE_NOTE`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from ._paths import _say, agy_is_wired, agy_plugin_dir

_WORKSPACE_NOTE = (
    "NOTE — this probe supplies its own workspace, so it proves the hook works "
    "WHEN GIVEN ONE. It cannot prove your real session will have one: agy sends "
    "`workspacePaths: []` unless it actually has a workspace (an interactive "
    "session with the folder open, or `agy --add-dir <path>`). If this probe is "
    "green and live mail still does not surface, that is the first thing to check."
)


def _hook_command(plugin_dir: Path) -> tuple[str | None, str | None]:
    """The binary hooks.json names for the Stop lane, and why it is unusable."""
    hooks_path = plugin_dir / "hooks.json"
    if not hooks_path.is_file():
        return None, f"no hooks.json at {hooks_path} — run `agentbus setup agy`"
    try:
        hooks = json.loads(hooks_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{hooks_path} is not readable JSON ({exc})"
    for block in hooks.values():
        for handler in block.get("Stop", []) or []:
            command = str(handler.get("command", ""))
            if "agy-stop" in command:
                return command, None
    return None, "hooks.json carries no agy-stop Stop handler — run `agentbus setup agy`"


def _binary_from(command: str) -> str:
    """The executable out of a command that may carry VAR=value prefixes."""
    for token in command.split():
        if "=" not in token.split("/")[-1] or token.startswith("/"):
            return token
    return command.split()[-1]


def doctor_wake_agy(agent: str, root: Path) -> int:
    """Run the real hook against a real payload and report what it resolved."""
    failures: list[str] = []
    plugin_dir = agy_plugin_dir()
    _say(f"  harness: agy (Antigravity), plugin at {plugin_dir}")

    command, why = _hook_command(plugin_dir)
    if command is None:
        _say(f"  [FAIL] {why}")
        return 1
    binary = _binary_from(command)
    if binary.startswith("/") and not Path(binary).is_file():
        failures.append(
            f"hooks.json names {binary}, which does not exist — a pip upgrade or a "
            "moved venv will do this, and a hook that cannot execute looks exactly "
            "like an agent with no mail. Re-run `agentbus setup agy`."
        )
    elif not binary.startswith("/") and not shutil.which(binary):
        failures.append(f"hooks.json names bare `{binary}` and it is not on PATH")
    else:
        _say(f"  [ok] hook binary: {binary}")

    if not agy_is_wired(root):
        failures.append(
            f"{root} is not in the agy opt-in list, so the machine-wide hooks "
            "deliberately ignore it. Run `agentbus setup agy` in this checkout."
        )
    else:
        _say(f"  [ok] opted in: {root}")

    # WHICH AGENT DOES THE HOOK RESOLVE? This is the check that would have
    # caught the real failure: an inherited `AGENTBUS_AGENT` made the hook act as
    # a DIFFERENT agent, poll that agent's inbox, and report no mail while this
    # project's own mail sat unread. Nothing errored; every other check was green.
    from ..hooks import _antigravity

    resolved = _antigravity._agent_for({"workspacePaths": [str(root)]})
    # COMPARE AGAINST THE CHECKOUT'S OWN DECLARATION, not against whatever name
    # doctor resolved for itself. doctor honours $AGENTBUS_AGENT (correctly — it
    # is a foreground CLI the operator invoked); the hook deliberately does not,
    # because on this harness that variable is inherited from whatever shell
    # launched agy. Comparing the two would flag the hook as wrong every time an
    # operator ran doctor from a shell carrying another agent's identity — which
    # is exactly what the first version of this check did, on its first run.
    declared = _antigravity._read_declared_agent(root)
    if resolved is None:
        failures.append(
            "the hook resolves NO agent for this checkout, so it will always do "
            "nothing here. Run `agentbus setup agy` in this directory."
        )
    elif declared and resolved != declared:
        failures.append(
            f"the hook resolves {resolved!r} but {root}/.agentbus/agent declares "
            f"{declared!r}. It would poll the WRONG agent's inbox and report no "
            "mail while this project's mail sits unread."
        )
    else:
        _say(f"  [ok] hook resolves: {resolved} (from {root}/.agentbus/agent)")
        if agent and resolved != agent:
            _say(
                f"       note: your shell says AGENTBUS_AGENT={agent}, but the hook "
                f"correctly uses the checkout's own identity. Not a fault."
            )

    # THE PROBE. Everything above inspects what we wrote; this observes what the
    # hook does when agy runs it. AGENTBUS_REWAKE_WINDOW=0 keeps it to one
    # deterministic pass, and a temp ledger keeps the production one untouched —
    # the same discipline the Claude wake probe already uses.
    payload = json.dumps({"workspacePaths": [str(root)], "conversationId": "doctor-probe"})
    import os as _os
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".txt") as ledger:
        env = {
            **_os.environ,
            "AGENTBUS_REWAKE_WINDOW": "0",
            "AGENTBUS_REWAKE_STATE": ledger.name,
        }
        try:
            proc = subprocess.run(
                ["/bin/sh", "-c", command],
                input=payload,
                capture_output=True,
                text=True,
                timeout=45,
                env=env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            _say(f"  [FAIL] the hook could not be executed: {exc}")
            return 1

    out = (proc.stdout or "").strip()
    if not out:
        failures.append(
            "the hook produced NO output. agy requires a JSON object on stdout; "
            f"stderr was: {(proc.stderr or '').strip()[:200] or '(empty)'}"
        )
    else:
        try:
            decision = json.loads(out).get("decision")
        except json.JSONDecodeError:
            failures.append(f"the hook printed non-JSON, which agy cannot read: {out[:160]}")
        else:
            _say(f"  [ok] hook ran and answered agy: decision={decision!r}")
            if decision == "continue":
                _say("       (it found unread mail and would have woken the session)")
            else:
                _say("       (nothing unread to claim right now — the lane is still wired)")

    _say("")
    _say(_WORKSPACE_NOTE)
    if failures:
        _say("")
        for f in failures:
            _say(f"  FAIL: {f}")
        return 1
    _say("")
    # Name the agent the HOOK will act as, not the one doctor resolved for
    # itself — they legitimately differ, and the hook's is the operative one.
    _say(f"  agy wake lane: WIRED for {resolved or agent} in {root}.")
    return 0
