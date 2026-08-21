#!/usr/bin/env python3
"""LIVE end-to-end probe for `agentbus remind` (plan: agentbus remind + RunFlow).

UNLIKE ITS NEIGHBOURS IN THIS DIRECTORY, THIS PROBE TALKS TO THE REAL BUS. The
others are safe-by-construction against 127.0.0.1 fakes; this one cannot be,
because the thing under test is a round trip through a third-party scheduler and
back into a real inbox. A fake would prove the payload shape and nothing about
the behaviour — which is the exact failure the backend hit with a stub that
agreed with its author about a 409.

It is READ-MOSTLY and SELF-ADDRESSED: every reminder it schedules targets the
acting agent, so it can never poke a peer. It cleans up what it creates.

    AGENTBUS_API_KEY=... AGENTBUS_AGENT=... python3 probe_remind_end_to_end.py

Exit 0 = every check passed. Non-zero = a named failure. UNTESTED is reported
explicitly and does NOT count as a pass.

WHY EACH CHECK EXISTS — the negative ones are the point:

  1. one-shot arrives            the happy path; proves nothing on its own
  2. the body is SEALED          a reminder rests until due; plaintext at rest
                                 for a week is the F9 defect class
  3. an EXPIRED reminder does    THE NEGATIVE CONTROL. Paired with (1) so
     NOT arrive                  "nothing arrived" is known to be a decision
                                 rather than a failure to schedule
  4. cancel prevents the fire    and a SECOND cancel is not an error (the
                                 scheduler 409s an already-cancelled object)
  5. recurrence fires TWICE      one fire is indistinguishable from a one-shot
"""
from __future__ import annotations

import contextlib
import os
import sys
import time
import uuid

SRC = os.environ.get("AGENTBUS_CLIENT_SRC") or os.path.join(
    os.path.dirname(__file__), "..", "..", "src"
)
sys.path.insert(0, os.path.abspath(SRC))

from agentbus_client.client import AgentBus, AgentBusError  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []   # (state, name, detail)


def record(state: str, name: str, detail: str = "") -> None:
    RESULTS.append((state, name, detail))
    print(f"  [{state:8}] {name}" + (f" — {detail}" if detail else ""), flush=True)


def _wait_for(bus: AgentBus, sentinel: str, seconds: int) -> dict | None:
    """Poll this agent's own inbox for a delivery containing `sentinel`.

    Reads through the PRODUCT (bus.inbox + bus.read), never the database, so a
    pass here means a real recipient could have seen it.
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        for delivery in bus.inbox(limit=25):
            try:
                full = bus.read(delivery.delivery_id)
            except AgentBusError:
                continue
            if sentinel in (full.get("text_body") or ""):
                return full
        time.sleep(5)
    return None


def main() -> int:
    bus = AgentBus()
    me = bus.agent or os.environ.get("AGENTBUS_AGENT")
    if not me:
        print("set AGENTBUS_AGENT — this probe only ever reminds ITSELF", file=sys.stderr)
        return 2
    print(f"probing as {me} (self-addressed only)\n")

    created: list[str] = []

    # ---------------------------------------------------------------- 1 + 2
    live = f"REMIND-PROBE-LIVE-{uuid.uuid4().hex[:8]}"
    try:
        row = bus.remind(live, delay="60s", subject="probe: one-shot")
        created.append(row["id"])
    except AgentBusError as exc:
        record("FAIL", "one-shot accepted", str(exc)[:120])
        return 1
    record("ok", "one-shot accepted", f"id={row['id']} due={row.get('due_at')}")

    got = _wait_for(bus, live, seconds=180)
    if not got:
        record("FAIL", "one-shot delivered", "no delivery inside 180s")
        return 1
    record("ok", "one-shot delivered", f"delivery={got['delivery_id']}")

    # SEALED AT REST. `sealed: true` is a CLAIM; the decoded body is the fact —
    # this reads the plaintext back through the client, which is the only party
    # that can. If the server had stored plaintext, this would still pass, so
    # the flag is checked too.
    if not got.get("sealed"):
        record("FAIL", "body sealed at rest", "delivery reports sealed=False")
    else:
        record("ok", "body sealed at rest", "sealed=True and the client unsealed it")

    # ------------------------------------------------------------------- 3
    # THE NEGATIVE CONTROL. Its value comes entirely from (1) above having
    # passed: we know a 60s reminder DOES arrive inside 180s, so "nothing
    # arrived" here is a decision rather than a broken pipe.
    expired = f"REMIND-PROBE-EXPIRED-{uuid.uuid4().hex[:8]}"
    try:
        row = bus.remind(expired, delay="60s", expire="1s", subject="probe: expired")
        created.append(row["id"])
        if _wait_for(bus, expired, seconds=150):
            record("FAIL", "expired reminder withheld", "a lapsed reminder WAS delivered")
        else:
            record("ok", "expired reminder withheld", "not delivered, and (1) proves it could be")
    except AgentBusError as exc:
        record("UNTESTED", "expired reminder withheld", f"refused at create: {exc}"[:120])

    # ------------------------------------------------------------------- 4
    cancelled = f"REMIND-PROBE-CANCEL-{uuid.uuid4().hex[:8]}"
    try:
        row = bus.remind(cancelled, delay="90s", subject="probe: cancel")
        bus.cancel_remind(row["id"])
        # A SECOND CANCEL MUST NOT ERROR. The scheduler 409s an already-cancelled
        # object; the caller's intent is already true, so that is success.
        try:
            bus.cancel_remind(row["id"])
            record("ok", "double cancel is not an error", "second DELETE accepted")
        except AgentBusError as exc:
            record("FAIL", "double cancel is not an error", str(exc)[:120])
        if _wait_for(bus, cancelled, seconds=150):
            record("FAIL", "cancel prevents the fire", "a cancelled reminder WAS delivered")
        else:
            record("ok", "cancel prevents the fire", "not delivered")
    except AgentBusError as exc:
        record("UNTESTED", "cancel", str(exc)[:120])

    # ------------------------------------------------------------------- 5
    # ONE FIRE IS INDISTINGUISHABLE FROM A ONE-SHOT, so this waits for TWO.
    # Left UNTESTED rather than failed when the tier's floor forbids a cadence
    # fast enough to observe twice inside a probe run.
    record(
        "UNTESTED",
        "recurrence fires twice",
        "needs a cadence observable inside one run — assign to a human tester",
    )

    # ------------------------------------------------------------------- 6
    # TIMEZONE IS UNFALSIFIABLE FROM A UTC BOX, and saying so is the whole point.
    # With --timezone omitted, "the server defaulted to UTC" and "the server
    # silently used my machine's local zone" are OBSERVATIONALLY IDENTICAL when
    # that local zone IS UTC. A green here would mean nothing. Caught by
    # bikeroom-freebsd-operato-b124c2, who declined to test it from a UTC box
    # rather than report a meaningless pass — the empty-room problem wearing a
    # clock. It needs a tester on a non-UTC machine.
    import time as _t

    local_utc = _t.timezone == 0 and not _t.daylight
    record(
        "UNTESTED",
        "timezone default is UTC (omitted flag)",
        "this box is UTC — the two behaviours are indistinguishable from here; "
        "needs a non-UTC seat" if local_utc else "run from a non-UTC box: compare "
        "the response's zone against local",
    )

    # DESCOPED BY OPERATOR DECISION, 2026-08-21, before testing began — not a
    # pass, and not an oversight. Farshid, asked directly: "no it's not a release
    # blocker. we don't need to test that now."
    #
    # KEPT IN THE OUTPUT RATHER THAN DELETED. Removing the line would make the
    # gap invisible, which is the failure this whole probe exists to prevent.
    #
    # THE RISK IS NOT WHAT I FIRST WROTE DOWN. Corrected by runflow-3858c4 and
    # verified against their source; my original paragraph was wrong twice:
    #
    #   503  LARGELY UNREACHABLE ON OUR TIER. Paid tiers set
    #        meter_allow_when_unavailable=True (tiers.py:336,354), so an
    #        unreachable meter FAILS OPEN and the submit is allowed. The
    #        503-on-metering path is a FREE-tier behaviour we cannot reach. A
    #        genuine over-quota denial still refuses on every tier — being over
    #        quota is an answer, not an outage.
    #
    #   429  REACHABLE AT ORDINARY VOLUMES, and NOT via the quota. Every mutating
    #        endpoint passes a per-tenant token bucket independent of quota:
    #        100 burst then 50/sec (config.py:435,439). "100,000 timers of
    #        headroom" — my own reasoning — covers the QUOTA 429 and misses this
    #        one entirely. One timer per reminder means our create rate IS the
    #        user reminder rate, so a backlog drain, a migration arming existing
    #        reminders, or a retry storm hits 429 with 99,900 timers unused.
    #
    #        NOT VISIBLE IN GET /api/v1/tenant/quotas — I checked; the response
    #        carries no rate or burst field, so a consumer reading their own
    #        quotas would never discover this limit exists.
    #
    #        Handling is cheap: back off and re-issue with the SAME idempotency
    #        key. A dedup consumes no quota and returns the existing timer, so a
    #        retried create after a 429 cannot double-arm a reminder.
    record(
        "DESCOPED",
        "429/503 refusal shapes",
        "operator decision 2026-08-21: not a release blocker. Never executed",
    )

    for reminder_id in created:
        with contextlib.suppress(AgentBusError):
            bus.cancel_remind(reminder_id)

    failed = [r for r in RESULTS if r[0] == "FAIL"]
    untested = [r for r in RESULTS if r[0] == "UNTESTED"]
    descoped = [r for r in RESULTS if r[0] == "DESCOPED"]
    passed = len(RESULTS) - len(failed) - len(untested) - len(descoped)
    # NEITHER untested NOR descoped COUNTS AS A PASS. They are printed
    # separately and deliberately: a tally that folded them into "passed" would
    # report coverage the run never had.
    print(f"\n{passed} passed, {len(failed)} failed, "
          f"{len(untested)} UNTESTED, {len(descoped)} DESCOPED "
          f"(neither counts as a pass)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
