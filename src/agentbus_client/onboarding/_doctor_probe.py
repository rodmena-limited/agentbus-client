from __future__ import annotations

import os
import subprocess
from typing import TYPE_CHECKING, Any

from agentbus_client.client import AgentBusError

if TYPE_CHECKING:
    from pathlib import Path

    from agentbus_client.client import AgentBus

_REWAKE_EXIT_NEW_MAIL = 2


def _scan_unread(bus: AgentBus, sent: dict[str, Any]) -> tuple[str | None, int]:
    probe_delivery = None
    bystanders = 0
    for delivery in bus.inbox(limit=200, unread=True):
        if delivery.message_id == sent["id"]:
            probe_delivery = delivery.delivery_id
        else:
            bystanders += 1
    return probe_delivery, bystanders


def _probe_rewake_cycle(
    bus: AgentBus, name: str, rewake_path: Path, prod_ledger: Path, prod_before: str
) -> list[str]:
    import tempfile

    from ._paths import _say

    failures: list[str] = []
    try:
        sent = bus.send(
            name, subject="doctor --wake probe", text="Wake-chain probe; read and forget."
        )
        probe_delivery, bystanders = _scan_unread(bus, sent)
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
            if (
                run1.returncode == _REWAKE_EXIT_NEW_MAIL
                and probe_delivery
                and probe_delivery in run1.stdout
            ):
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
    return failures
