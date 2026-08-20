"""The `agentbus` command line client."""

from __future__ import annotations

import argparse
import os
import sys

from . import _common
from ._common import _accept_common_flags_after_subcommand, _cfg_dir, _print
from ._watch_runtime import _watch_logfile


def cmd_retire(args: argparse.Namespace) -> int:
    """Stand an agent down. REVERSIBLE — re-registering restores everything.

    Documented in /llms.txt since the withdrawal section was written and never
    actually implemented, which a CLI-parity probe caught on its first run. A doc
    naming a command the binary lacks is worse than no doc: the reader concludes
    their install is broken.
    """
    bus = _common._bus(args)
    name = args.name or args.agent or os.environ.get("AGENTBUS_AGENT")
    if not name:
        print("which agent? pass a name, --agent, or set AGENTBUS_AGENT", file=sys.stderr)
        return 2
    result = bus._request("POST", f"/v1/agents/{name}/retire")
    if args.json:
        _print(result, True)
    else:
        print(f"retired {name}")
        print(
            "  reversible: re-register with the same name to restore the same "
            "identity, address, inbox and history"
        )
    return 0


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
# A watcher that gives up is indistinguishable from one that was never started.
StartLimitIntervalSec=0

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
# -r restarts on ANY exit including clean ones; -R 5 paces it so a config
# error cannot become a hot loop.
command_args="-r -R 5 -P ${{pidfile}} -p /var/run/${{name}}.child.pid \\
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


def add_commands(sub: argparse._SubParsersAction) -> None:
    """Wire this module's subcommands into the shared subparser."""

    p = sub.add_parser("retire", help="stand an agent down (reversible)")
    p.add_argument("name", nargs="?", default=None)
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_retire)

    p = sub.add_parser(
        "service",
        help="emit a systemd unit (Linux) or launchd plist (macOS) "
        "so the watcher is supervised, not merely detached",
    )
    p.add_argument(
        "--manager",
        default=None,
        choices=["systemd", "launchd", "rc.d"],
        help="override the auto-detected service manager (rc.d for FreeBSD, #153)",
    )
    p.add_argument(
        "--env-file",
        default=None,
        help="reference this env file for credentials instead of "
        "inlining the key into the unit (recommended)",
    )
    _accept_common_flags_after_subcommand(p)
    p.set_defaults(func=cmd_service)
