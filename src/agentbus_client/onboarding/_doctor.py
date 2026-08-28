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
import contextlib
import json
import os
import re
import subprocess
from pathlib import Path

from ..client import AgentBus, AgentBusError
from ..identity import config_dir as identity_config_dir
from ._identity import _agent_key, _resolve_agent_name
from ._paths import (
    _MARKER_HOOK,
    _MARKER_REWAKE,
    _config_dir,
    _keys_dir,
    _load_json,
    _plugin_provides_wake,
    _say,
)

# ---------------------------------------------------------------- doctor --wake


def _monitor_pids(agent: str | None = None, session: str | None = None) -> list[str]:
    """PIDs of the plugin monitor FOR THIS AGENT, or [].

    Matching merely on the script name was E2, and it was a check that could
    not go red: on a multi-agent host every agent saw the same pid, so an agent
    whose own monitor had died still reported green as long as ANY other
    agent's monitor was alive. The streamer child carries `--agent <name>` and
    `--state monitor-<name>.json`, so scope to those.
    """
    try:
        # `-axwwo`, NOT `-eo` (#117). On FreeBSD, `ps -eo` without a controlling
        # terminal lists almost nothing (gettys only — 21 lines on a box running
        # a live monitor) and BSD ps truncates args without `-ww`, so doctor
        # reported "monitor is NOT running" while three matching processes ran.
        # A detection that cannot see the thing it detects is the vacuous-check
        # class again, this time varying by OS. `-axwwo pid,args` is accepted by
        # procps (Linux) and BSD ps alike and shows every process, full args —
        # verified on both before this change.
        out = subprocess.run(
            ["ps", "-axwwo", "pid,args"], capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    pids = []
    for line in out.splitlines():
        if "grep" in line:
            continue
        if agent:
            # ONLY the monitor's own state file. `--agent <name>` was also
            # accepted here, and that was a false green of the same class as E2
            # one layer further out: a SUPERVISED WATCHER carries `--agent
            # <name>` too, so on a host running both, doctor reported "plugin
            # MONITOR, running" while pointing at the watcher's pid. The two do
            # not do the same job — a watcher only CAPTURES to a file and can
            # never start a turn — so if the monitor died the session was
            # genuinely deaf and doctor still said green. The check could not go
            # red on the one failure it exists to catch. `monitor-<agent>.json`
            # is the name the plugin picked precisely so the two would not
            # collide, so it discriminates exactly. Reported by david, who runs
            # both; that combination gets more common, not less, while the docs
            # still recommend a watcher for wake_channel.
            # TWO QUESTIONS, TWO SCOPES. `doctor` asks "is a monitor running
            # for me at all", which is per-AGENT. `session-end` asks "is THIS
            # session's monitor running", which is per-SESSION — and answering
            # the first when you meant the second is how a headless `claude -p`
            # came to reap an interactive session's monitor. Identity is
            # device+repo+path, so agent scope cannot separate two sessions on
            # one checkout.
            want = f"monitor-{agent}-{session}.json" if session else f"monitor-{agent}-"
            # BOTH the state-file name AND an exact `--agent <name>` token (review
            # #23, S8): `monitor-agentbus-` is a prefix of `monitor-agentbus-ui-...`,
            # so the name alone gave a false green for prefix-sharing agents.
            agent_token = re.compile(
                rf"(?:^|\s)--agent(?:=|\s+)['\"]?{re.escape(agent)}['\"]?(?:\s|$)"
            )
            if want in line and agent_token.search(line):
                pids.append(line.split(None, 1)[0])
        elif "agentbus-monitor.sh" in line:
            pids.append(line.split(None, 1)[0])
    return pids


def _installed_version() -> str | None:
    """What is installed on disk — deliberately a different question from what a
    running process loaded, which is the whole point of the comparison below."""
    try:
        from .. import __version__

        return str(__version__)
    except Exception:
        return None


def _running_watcher_version(agent: str) -> str | None:
    """The client version the LIVE watcher imported, or None if it cannot be told.

    Read from the watcher's own state file, which the process rewrites on every
    checkpoint — so it reflects what is RUNNING, not what is installed. Returns
    None for a watcher old enough not to stamp it, and None is not a pass: the
    caller must say "cannot confirm" rather than assume a match.
    """
    import glob

    newest, newest_mtime = None, -1.0
    for path in glob.glob(str(identity_config_dir() / f"monitor-{agent}-*.json")) + glob.glob(
        str(identity_config_dir() / f"watch-*-{agent}.json")
    ):
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        if mtime > newest_mtime:
            newest, newest_mtime = path, mtime
    if newest is None:
        return None
    try:
        return json.loads(Path(newest).read_text()).get("client_version") or None
    except (OSError, ValueError):
        return None


def _finish_wake_report(
    failures: list[str], plugin_wake: bool = False, agent: str | None = None
) -> int:
    if failures:
        _say("")
        for f in failures:
            _say(f"  FAIL: {f}")
        return 1
    _say("")
    if plugin_wake:
        # DO NOT CERTIFY A BUILD THAT WAS NOT CONFIRMED.
        #
        # This said PROVEN on the strength of a process EXISTING. bob upgraded the
        # client, did not restart, and got a green wake report from a watcher still
        # running the pre-upgrade code — the diagnostic certifying the exact
        # configuration the upgrade existed to fix. "A process is running" and "the
        # running process has the fix" are different claims and only the first was
        # ever checked.
        loaded = _running_watcher_version(agent) if agent else None
        installed = _installed_version()
        # A SOURCE CHECKOUT IS NOT EVIDENCE OF A STALE WATCHER.
        #
        # Running from a working tree reports 0.0.0+source, so comparing it to a
        # properly installed watcher would fail this check on every developer
        # machine — a false "your wake path is broken" is how a real one stops
        # being believed. Only two real, differing versions are grounds to refuse.
        comparable = (
            loaded
            and installed
            and "source" not in loaded
            and "source" not in installed
            and loaded != "unknown"
            and installed != "unknown"
        )
        if comparable and loaded != installed:
            _say(
                f"wake chain NOT PROVEN: the running watcher loaded client "
                f"{loaded}, but {installed} is installed."
            )
            _say("A process that started before the upgrade is still serving this")
            _say("session's wake path, so any fix in the newer client is NOT active")
            _say("here. RESTART THIS SESSION, then re-run this check.")
            return 1
        _say("wake chain PROVEN as far as this host can see: the plugin monitor is")
        _say("running and holds a live stream. Measured on two hosts, a peer's")
        _say("message woke an idle session in 13-19s with no human input.")
        if loaded:
            _say(f"The running watcher reports client {loaded}, matching what is installed.")
        else:
            _say("NOTE: this watcher does not report which client build it loaded")
            _say("(pre-0.4.40), so 'running the current code' is assumed, not shown.")
    return 0


def doctor_wake(args: argparse.Namespace) -> int:
    """Prove the wake chain, link by link, ending with the honest sentence.

    `doctor` without --wake proves auth/quota/SMTP — all of which were green on
    a host that was structurally incapable of answering anyone. This half
    tests the one failure that actually costs outages.
    """
    failures: list[str] = []
    # SAY WHICH SOURCE CHOSE THE AGENT. `setup` has always explained itself;
    # doctor printed only the name, so an operator who exported AGENTBUS_AGENT
    # and got a different agent back had no way to see that this project's
    # settings.local.json had legitimately outranked it. david read that as
    # "the environment variable is dead" and filed it as a defect — a
    # reasonable conclusion from output that states a decision and hides its
    # reason. The precedence is correct and deliberate (project identity beats
    # a shell that happens to have something exported, which was E1); what was
    # missing is that it says so.
    #
    # The old `or os.environ.get("AGENTBUS_AGENT")` was dead code AND
    # misleading: it implied the environment is a last-resort fallback when it
    # is precedence 2 inside _resolve_agent_name.
    provenance: list[str] = []
    name = _resolve_agent_name(explain=provenance)
    if not name:
        _say(
            "wake: cannot tell which agent this project is (no AGENTBUS_AGENT, no setup). Run `agentbus setup claude` first."
        )
        return 1
    _say(f"wake: agent {name}")
    for line in provenance:
        _say(f"  ({line})")

    key = _agent_key(name)
    if key:
        _say(f"  [ok] credential: {_keys_dir()}/{name}.env")
    else:
        failures.append(f"no readable key file for {name}")

    settings = _load_json(Path.home() / ".claude" / "settings.json")
    hooks = settings.get("hooks", {})

    def _has(event: str, marker: str) -> bool:
        return any(
            marker in str(h.get("command", ""))
            for g in hooks.get(event, [])
            for h in g.get("hooks", [])
        )

    # THE PLUGIN CASE FIRST. When the plugin owns the wake, setup has correctly
    # REMOVED our hooks — so a doctor that only looks at settings.json reports
    # "PASSIVE ONLY" on the recommended configuration, and tells the user to run
    # the very command that produced it. That is a diagnostic looking for the
    # mechanism IT understands instead of the one that actually wakes the
    # session, on a host where the monitor had woken the agent 13 seconds
    # earlier. Consult the same predicate setup uses, then verify the monitor is
    # genuinely RUNNING rather than merely declared.
    if _plugin_provides_wake(settings):
        _say("  [ok] passive hooks: provided by the agentbus PLUGIN")
        running = _monitor_pids(name)
        if running:
            _say(
                f"  [ok] active trigger: plugin MONITOR, running (pid {running[0]}) "
                "— a stream held for the whole session"
            )
        else:
            failures.append(
                "the agentbus plugin is enabled but its monitor is NOT running. "
                "Monitors start at SESSION START — restart this session. If it "
                "still does not appear, check `claude plugin list`."
            )
        return _finish_wake_report(failures, plugin_wake=True, agent=name)

    passive = _has("SessionStart", _MARKER_HOOK) and _has("UserPromptSubmit", _MARKER_HOOK)
    active = _has("Stop", _MARKER_REWAKE)
    _say(f"  [{'ok' if passive else '!!'}] passive hooks (SessionStart + UserPromptSubmit)")
    _say(f"  [{'ok' if active else '!!'}] active trigger (Stop re-waker)")
    rewake_path = _config_dir() / "stop-rewake.sh"
    executable = rewake_path.exists() and os.access(rewake_path, os.X_OK)
    _say(f"  [{'ok' if executable else '!!'}] {rewake_path} executable")

    # D9: a client upgrade leaves the SCRIPT on disk stale, so the ledger-
    # isolation override silently no-ops and the doctor poisons the very ledger
    # it claims to protect while printing green. Refuse to trust a script that
    # predates the override rather than run a check that cannot go red.
    script_current = False
    if rewake_path.exists():
        body = rewake_path.read_text()
        marker = "agentbus-rewake-version:"
        ver = 0
        if marker in body:
            with contextlib.suppress(ValueError):
                ver = int(body.split(marker, 1)[1].split("\n", 1)[0].strip())
        script_current = ver >= 2
        if not script_current:
            failures.append(
                f"stop-rewake.sh is STALE (version {ver or 'unstamped'} < 2); a "
                "client upgrade did not refresh it, so its ledger isolation is a "
                "no-op. Run `agentbus setup claude` to reinstall it before trusting "
                "this check."
            )

    # D7 (david): the Stop chain passing is NOT the same as being notifiable.
    # Report the platform's own wake_channel fact so a green re-waker and a
    # send-time "no_wake_channel" warning can never contradict unnoticed.
    if key:
        try:
            probe = AgentBus(api_key=key, base_url=args.base_url, agent=name)
            roster = probe.phonebook()
            me = next((a for a in roster if a.get("name") == name), None)
            wc = me.get("wake_channel") if me else None
            # #49: wake_channel is now 4-state (live | stale | webhook | none),
            # replacing the boolean. Truthy means "something attached"; stale is
            # an attached subscriber with no recent sign of life — still not
            # "none", but worth saying so.
            if wc in (True, "live", "webhook"):
                _say(
                    "  [ok] wake_channel: a live subscriber/webhook is attached "
                    "(peers can be notified)"
                )
            elif wc == "stale":
                _say(
                    "  [--] wake_channel: STALE — a subscriber is attached but "
                    "nothing has moved in a while. It may be an orphaned stream. "
                    "The Stop re-waker still answers at turn boundaries, but "
                    "peers' send responses may warn 'no_wake_channel'."
                )
            elif wc in (False, "none", None):
                _say(
                    "  [--] wake_channel: NONE attached. The Stop re-waker makes "
                    "THIS session answer at turn boundaries, but peers' send "
                    "responses will warn 'no_wake_channel'. For always-attached "
                    "reachability run a supervised watcher: agentbus service."
                )
        except AgentBusError:
            pass

    if key and active and executable and script_current:
        # The live half: a self-addressed probe must surface through the
        # re-waker exactly once. A check that cannot go green cannot go red —
        # and a diagnostic must never CONSUME evidence: the first version of
        # this marked every listed delivery read, which silently ate three of
        # a peer's real messages (including a key-rotation confirmation) the
        # first time it ran on a host with a backlog. Now it touches exactly
        # one delivery — its own probe — runs the re-waker against an
        # ISOLATED seen-ledger so the production Stop hook still wakes for
        # everything else, and reports the foreign unread it deliberately
        # left alone.
        import tempfile

        from .. import rewake as _rewake

        # ASK THE RE-WAKER WHERE ITS LEDGER IS — do not rebuild the path here.
        #
        # This line used to be `_config_dir() / f"rewake-seen-{name}.txt"`,
        # which diverged from `rewake._ledger_path()` in THREE ways:
        #   * rewake honours $AGENTBUS_REWAKE_STATE; this ignored it
        #   * rewake's dir comes from $AGENTBUS_WAKE_DIR, this one from
        #     $AGENTBUS_CONFIG_DIR — different env vars entirely
        #   * rewake sanitizes the agent through sealing.agent_slug (REG-8c);
        #     this did not
        #
        # Whenever any of those diverged, `prod_before`/`prod_after` sampled a
        # file the re-waker never writes, so the "production wake-ledger
        # untouched (isolation verified)" assertion below could only ever go
        # GREEN. A check that cannot go red is not evidence — and this one is
        # load-bearing: it is what proves `doctor --wake` did not eat the
        # operator's real pending wakes.
        prod_ledger = _rewake._ledger_path(name)
        prod_before = prod_ledger.read_text() if prod_ledger.exists() else ""

        bus = AgentBus(api_key=key, base_url=args.base_url, agent=name)
        try:
            sent = bus.send(
                name, subject="doctor --wake probe", text="Wake-chain probe; read and forget."
            )
            probe_delivery = None
            bystanders = 0
            for delivery in bus.inbox(limit=200, unread=True):
                if delivery.message_id == sent["id"]:
                    probe_delivery = delivery.delivery_id
                else:
                    bystanders += 1
            if probe_delivery is None:
                failures.append("probe message did not appear in the unread list")

            with tempfile.NamedTemporaryFile(prefix="rewake-doctor-") as ledger:
                # WINDOW=0 -> one deterministic pass, not a 10-minute hold; the
                # isolated ledger keeps the production seen-list untouched.
                env = dict(
                    os.environ,
                    AGENTBUS_AGENT=name,
                    AGENTBUS_REWAKE_STATE=ledger.name,
                    AGENTBUS_REWAKE_WINDOW="0",
                )
                run1 = subprocess.run(
                    [str(rewake_path)],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
                if run1.returncode == 2 and probe_delivery and probe_delivery in run1.stdout:
                    _say("  [ok] probe surfaced by the re-waker (exit 2)")
                else:
                    failures.append(f"re-waker did not surface the probe (exit {run1.returncode})")
                if probe_delivery:
                    bus.read(probe_delivery)
                run2 = subprocess.run(
                    [str(rewake_path)],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    check=False,
                )
            if run2.returncode == 0 and not run2.stdout:
                _say("  [ok] re-waker silent on second run (dedupe holds)")
            else:
                failures.append("re-waker fired twice for one probe — dedupe broken")
            # D9 verification: prove the override actually took, by confirming
            # the PRODUCTION ledger did not move. This is the by-hand check that
            # caught the stale-script no-op; now it runs every time.
            prod_after = prod_ledger.read_text() if prod_ledger.exists() else ""
            if prod_after == prod_before:
                _say("  [ok] production wake-ledger untouched (isolation verified)")
            else:
                failures.append(
                    "doctor POISONED the production wake-ledger — the "
                    "isolation override did not take (stale script?)"
                )
            if bystanders:
                _say(
                    f"  [ok] {bystanders} other unread message(s) UNTOUCHED — "
                    "read them: agentbus inbox --unread"
                )
        except (AgentBusError, OSError, subprocess.SubprocessError) as exc:
            failures.append(f"probe cycle failed: {exc}")

    if not active or not executable or not script_current:
        _say("")
        if not script_current and active:
            _say("STALE re-waker — reinstall with `agentbus setup claude`, then re-run.")
        else:
            _say("PASSIVE ONLY — this agent answers only when a human prompts it.")
            _say("Run `agentbus setup claude` to wire the active trigger.")
        return 1
    if failures:
        _say("")
        for f in failures:
            _say(f"  FAIL: {f}")
        return 1
    _say("")
    _say("wake chain PROVEN up to the harness boundary. Remaining assumption,")
    _say("stated rather than rounded up: your harness invokes Stop hooks with")
    _say("asyncRewake — exit 2 then pulls the session back into a turn.")
    return 0
