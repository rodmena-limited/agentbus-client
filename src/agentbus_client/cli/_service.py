"""The `agentbus` command line client."""

from __future__ import annotations

import argparse
import os
import sys

from ..client import AgentBusError
from . import _common
from ._common import _cfg_dir, _print
from ._watch_runtime import _watch_logfile


def cmd_retire(args: argparse.Namespace) -> int:
    """Stand an agent down. REVERSIBLE — re-registering restores everything.

    Documented in /llms.txt since the withdrawal section was written and never
    actually implemented, which a CLI-parity probe caught on its first run. A doc
    naming a command the binary lacks is worse than no doc: the reader concludes
    their install is broken.
    """
    name = args.name or args.agent or os.environ.get("AGENTBUS_AGENT")
    if not name:
        print("which agent? pass a name, --agent, or set AGENTBUS_AGENT", file=sys.stderr)
        return 2

    # ACT AS THE AGENT YOU NAMED, when we hold its bound key (#57).
    #
    # `retire` used to build the client from the AMBIENT identity and then ask
    # the server to retire a name that identity may have nothing to do with. So
    # the last step of tearing a project down — delete `.agentbus/agent`, delete
    # settings.local.json, then retire — failed with:
    #
    #   permission_denied: an agent may act only on itself; acting as no agent
    #
    # while that very agent's bound key sat in ~/.config/agentbus/keys/. Every
    # piece of information needed was present and the command refused anyway,
    # which is the shape of every identity bug in this release.
    #
    # An explicit --api-key still wins, and an agent whose key we do NOT hold
    # still gets the server's permission error — correctly, because retiring
    # somebody else's agent genuinely does need an admin key.
    if not getattr(args, "api_key", None):
        own_key = _common._key_for_agent(name)
        if own_key:
            args.api_key = own_key
            args.agent = name

    bus = _common._bus(args)
    try:
        result = bus._request("POST", f"/v1/agents/{name}/retire")
    except AgentBusError as exc:
        # RETIRING A RETIRED AGENT IS NOT A FAILURE. It is the state you asked
        # for. The second `retire` in the operator's transcript printed an error
        # that read like something had gone wrong when nothing had.
        if getattr(exc, "code", "") == "agent_retired":
            print(f"{name} is already retired — nothing to do.")
            print(
                "  reversible: re-register with the same name to restore the same "
                "identity, address, inbox and history"
            )
            return 0
        raise
    if args.json:
        _print(result, True)
    else:
        print(f"retired {name}")
        print(
            "  reversible: re-register with the same name to restore the same "
            "identity, address, inbox and history"
        )
        _warn_local_wiring_will_revive(name)
    return 0


def _warn_local_wiring_will_revive(name: str) -> None:
    """Say when THIS checkout will bring the agent you just retired back.

    The operator ran retire six times and it "did not work" every time. It
    worked every time — and then `setup` read `.agentbus/agent`, re-registered
    that name, and un-retired it. Two commands that each reported success,
    composing into a no-op, with nothing in either output connecting them.

    Retiring is deliberately NOT teardown: the agent may be wired in other
    checkouts, and deleting someone's local files as a side effect of a
    server-side state change would be its own bug. So this warns and names the
    command, rather than acting.
    """
    import contextlib
    from pathlib import Path

    from ..onboarding import _git_root_or_none

    root = _git_root_or_none() or Path.cwd()
    declares: list[str] = []
    agent_file = root / ".agentbus" / "agent"
    with contextlib.suppress(OSError):
        if agent_file.is_file() and agent_file.read_text().strip() == name:
            declares.append(str(agent_file))
    settings = root / ".claude" / "settings.local.json"
    with contextlib.suppress(OSError, ValueError):
        import json as _json

        if settings.is_file() and (
            (_json.loads(settings.read_text()).get("env") or {}).get("AGENTBUS_AGENT") == name
        ):
            declares.append(str(settings))
    if not declares:
        return
    print()
    print(f"  WARNING — this checkout still DECLARES '{name}':")
    for d in declares:
        print(f"      {d}")
    print("  So the next `agentbus setup` here will re-register it and UN-RETIRE it.")
    print("  To stop using it in this checkout:  agentbus teardown")


def _plist_key_line(key: str, agent: str) -> str:
    """Only emit a REAL key. A placeholder in a launchd plist is a malformed
    credential that KeepAlive retries forever (david D8); omitting it lets the
    0.3.1 resolution chain read ~/.config/agentbus/keys/<agent>.env instead."""
    if key and key.startswith("ab_sk_"):
        return f"\n        <key>AGENTBUS_API_KEY</key><string>{key}</string>"
    return (
        f"\n        <!-- AGENTBUS_API_KEY read from "
        f"~/.config/agentbus/keys/{agent}.env at runtime; run signin first -->"
    )


def cmd_service(args: argparse.Namespace) -> int:
    """Emit a service definition so the watcher is supervised, not just detached.

    `--daemon` survives the session that started it. It does NOT survive a
    reboot, an OOM kill, or a crash — and a watcher that dies silently is the
    exact failure this whole feature exists to prevent. Supervision is the
    difference between "started" and "stays running".

    Deliberately EMITS a unit rather than installing one: writing into a user's
    init system unprompted is not ours to do, and a printed unit can be read
    before it is trusted.
    """
    import platform
    import shutil

    agent = args.agent or os.environ.get("AGENTBUS_AGENT") or ""
    if not agent:
        print("no acting agent: pass --agent or set AGENTBUS_AGENT", file=sys.stderr)
        return 2

    exe = shutil.which("agentbus") or f"{sys.executable} -m agentbus_client.cli"
    key = os.environ.get("AGENTBUS_API_KEY", "")
    base = os.environ.get("AGENTBUS_BASE_URL", "https://agentbus.rodmena.co.uk")
    system = platform.system()
    manager = args.manager
    if manager is None:
        # #153: NEVER default to an init system the host does not have. FreeBSD
        # got a complete systemd unit, exit 0, and instructions naming a binary
        # that does not exist — the documented remedy for an unwatched inbox
        # silently guaranteeing one. Found by auth-service-b080da, reproduced
        # independently by infra-manager-c13110 (rodmena-vm-2). An explicit
        # --manager stays honored anywhere: the operator outranks detection.
        if system == "Darwin" and shutil.which("launchctl"):
            manager = "launchd"
        elif shutil.which("systemctl"):
            manager = "systemd"
        else:
            hint = " (this host looks like FreeBSD)" if system == "FreeBSD" else ""
            print(
                "no supported service manager found: looked for systemd's "
                f"`systemctl` and launchd's `launchctl`, neither is on PATH{hint}.\n"
                f"For FreeBSD rc.d:  agentbus service --manager rc.d --agent {agent}\n"
                "Refusing to emit a unit an absent init would never load — exit 0\n"
                "with an unloadable file is the silent-no-watcher failure this\n"
                "command exists to prevent (#153).",
                file=sys.stderr,
            )
            return 2

    env_file = getattr(args, "env_file", None)
    # Default to the per-agent 0600 key file that signin/setup already wrote.
    # The old default emitted `Environment=AGENTBUS_API_KEY=<your ab_sk_ key>`,
    # a LITERAL placeholder that whoami rejects as malformed — with
    # Restart=always the watcher then loops forever on auth failure, and an
    # explicit (invalid) env var also DEFEATS the key-file resolution chain
    # added in 0.3.1. Emitting no key line at all lets resolution find the file
    # and keeps the secret out of a world-readable unit (david D8).
    # REG-8b (round-3.5): sanitize `agent` before the path join. `agentbus
    # service` writes the path into a systemd unit's EnvironmentFile line —
    # a traversal payload would READ <config>/operator.env and PERSIST that
    # path in a systemd unit, so a rogue service would auto-source the
    # operator credential on every start. bound_env_filename ensures the
    # generated unit only points inside keys/.
    from .. import sealing as _sealing

    default_key_file = _cfg_dir() / "keys" / _sealing.bound_env_filename(agent)
    if not env_file and default_key_file.exists():
        env_file = str(default_key_file)
    if manager == "systemd":
        # A unit file is not a secret store. Referencing an EnvironmentFile keeps
        # the key in one 0600 file instead of copying it into ~/.config/systemd,
        # which is the "one fact, two places" trap in credential form. With no
        # key available anywhere, emit NO key line — a missing key fails loudly
        # once, a placeholder fails forever.
        creds = (
            f"EnvironmentFile={env_file}"
            if env_file
            else (
                "# AGENTBUS_API_KEY resolved from ~/.config/agentbus/keys/"
                f"{agent}.env at runtime; run `agentbus signin` first"
            )
        )
        unit = f"""[Unit]
Description=AgentBus watcher for {agent}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
{creds}
Environment=AGENTBUS_BASE_URL={base}
Environment=AGENTBUS_AGENT={agent}
ExecStart={exe} watch --agent {agent}
Restart=always
RestartSec=5
RestartPreventExitStatus=2 3 8
StartLimitIntervalSec=300
StartLimitBurst=30

[Install]
WantedBy=default.target
"""
        print(unit)
        print(
            f"""# Install as a USER unit (no root, survives logout with lingering):
#   mkdir -p ~/.config/systemd/user
#   agentbus service --agent {agent} > ~/.config/systemd/user/agentbus-{agent}.service
#   systemctl --user daemon-reload
#   systemctl --user enable --now agentbus-{agent}.service
#   loginctl enable-linger $USER      # keeps it running when you are logged out
#
# Exit statuses that will NOT be restarted, because waiting cannot fix them:
#   8  authentication failed - the key is missing, malformed, unknown or revoked.
#      AgentBus has no transient 401, so a retry loop here is 17k requests a day
#      against a wall. Fix the key, then `systemctl --user start` it again.
#   3  misconfigured   2  bad arguments in the unit itself
# Every other failure (network, DNS, a bus deploy) still restarts forever.
#
# StartLimitBurst=30 / StartLimitIntervalSec=300 caps a CRASH loop on any other
# status: 30 starts inside 5 minutes puts the unit in `failed` so an operator
# sees it. A healthy watcher never reaches that, because it holds a network
# outage internally with persisted backoff rather than exiting — so repeated
# fast exits mean a real crash, not a blip. Recover with:
#   systemctl --user reset-failed agentbus-{agent}.service && systemctl --user start agentbus-{agent}.service
#
# Verify it is ACTUALLY attached, not merely 'active':
#   agentbus watch-status --agent {agent}
#   agentbus liveness""",
            file=sys.stderr,
        )
        return 0

    if manager == "launchd":
        label = f"co.uk.rodmena.agentbus.{agent}"
        exec_args = "".join(
            f"\n        <string>{part}</string>"
            for part in [*exe.split(), "watch", "--agent", agent]
        )
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>{label}</string>
    <key>ProgramArguments</key>
    <array>{exec_args}
    </array>
    <key>EnvironmentVariables</key>
    <dict>{_plist_key_line(key, agent)}
        <key>AGENTBUS_BASE_URL</key><string>{base}</string>
        <key>AGENTBUS_AGENT</key><string>{agent}</string>
    </dict>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>ThrottleInterval</key><integer>60</integer>
    <key>StandardOutPath</key><string>{_watch_logfile(agent)}</string>
    <key>StandardErrorPath</key><string>{_watch_logfile(agent)}</string>
</dict>
</plist>
"""
        print(plist)
        print(
            f"""# Install (macOS, no root):
#   agentbus service --agent {agent} > ~/Library/LaunchAgents/{label}.plist
#   launchctl load -w ~/Library/LaunchAgents/{label}.plist
#
# supervice is NOT an option here — it is Linux-only. launchd is the native
# equivalent and KeepAlive gives the same restart-on-death guarantee.
#
# LIMITATION, stated rather than implied: launchd has no per-exit-status restart
# suppression, so unlike the systemd unit this plist WILL keep restarting even
# when the key is revoked and every request returns 401 — a permanent condition
# that waiting cannot fix. ThrottleInterval=60 caps that at one attempt a minute
# instead of one every ten seconds. If `agentbus watch` here exits 8, unload the
# job and fix the credential:
#   launchctl unload ~/Library/LaunchAgents/{label}.plist
#
# Verify it is ACTUALLY attached:
#   agentbus watch-status --agent {agent}""",
            file=sys.stderr,
        )
        return 0

    if manager == "rc.d":
        # FreeBSD rc.d script, based on the working equivalent contributed by
        # auth-service-b080da (rodmena-vm-2, syntax-checked on 15.1) — their
        # daemon(8) notes are preserved as comments because each one encodes a
        # failure mode they anticipated.
        script = f"""#!/bin/sh
# /usr/local/etc/rc.d/agentbus_watch   — chmod 555
# enable: sysrc agentbus_watch_enable=YES
#         sysrc agentbus_watch_agent={agent}
#         service agentbus_watch start
#
# PROVIDE: agentbus_watch
# REQUIRE: NETWORKING
# KEYWORD: shutdown

. /etc/rc.subr

name=agentbus_watch
rcvar=agentbus_watch_enable

load_rc_config $name
: ${{agentbus_watch_enable:="NO"}}
: ${{agentbus_watch_agent:="{agent}"}}
: ${{agentbus_watch_bin:="{exe}"}}
: ${{agentbus_watch_envfile:="$HOME/.config/agentbus/keys/${{agentbus_watch_agent}}.env"}}

# Credentials come from the 0600 env file, NEVER inlined here — an rc script
# is world-readable and a copied key in it is the one-fact-two-places trap in
# credential form.
if [ -r "${{agentbus_watch_envfile}}" ]; then
    set -a; . "${{agentbus_watch_envfile}}"; set +a
fi
export AGENTBUS_BASE_URL="${{AGENTBUS_BASE_URL:-{base}}}"
export AGENTBUS_AGENT="${{agentbus_watch_agent}}"

pidfile="/var/run/${{name}}.pid"
command="/usr/sbin/daemon"
# -P is the SUPERVISOR pidfile, -p the child. Using only one means
# `service agentbus_watch stop` kills the wrong process and daemon(8)
# immediately restarts the watcher you just tried to stop.
# -r restarts on ANY exit including clean ones, and daemon(8) has no
# per-exit-status suppression — so a revoked key (exit 8) restarts forever here
# just as it would anywhere else. -R 60 caps that at one attempt a minute rather
# than twelve; a measured instance of the 5s version made 24,324 failed requests
# in a single day. If this job is looping, check `tail /var/log/${{name}}.log`
# for exit 8 and fix the credential before re-enabling.
command_args="-r -R 60 -P ${{pidfile}} -p /var/run/${{name}}.child.pid \\
              -o /var/log/${{name}}.log \\
              ${{agentbus_watch_bin}} watch --agent ${{agentbus_watch_agent}}"

run_rc_command "$1"
"""
        print(script)
        print(
            f"""# Install (FreeBSD, as root):
#   agentbus service --manager rc.d --agent {agent} > /usr/local/etc/rc.d/agentbus_watch
#   chmod 555 /usr/local/etc/rc.d/agentbus_watch
#   sysrc agentbus_watch_enable=YES agentbus_watch_agent={agent}
#   service agentbus_watch start
#
# `service ... start` returning 0 proves nothing — a watcher that gives up is
# indistinguishable from one that was never started. Verify ATTACHMENT:
#   agentbus watch-status --agent {agent}
#   agentbus liveness""",
            file=sys.stderr,
        )
        return 0

    print(
        f"unknown service manager '{manager}' (expected systemd, launchd, or rc.d)",
        file=sys.stderr,
    )
    return 2
