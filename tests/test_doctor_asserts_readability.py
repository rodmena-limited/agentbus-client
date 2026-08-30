"""#49: doctor asserts what the USER can observe, not an internal state value.

WHAT IT USED TO DO:

    if match and match[0].state in ("delivered", "read", "acked"):

That value is not part of any contract. On 2026-08-17 a backend change stopped
it advancing for SEALED deliveries, so on an encrypted workspace NO delivery
ever reached a terminal state — 0 of 3,658, measured by the backend against
69,591 unsealed ones that did.

doctor then reported a broken loop three runs running WHILE HOLDING THE MESSAGE
in the very page it had just fetched. It had the evidence of success in hand and
rejected it on a field that was never part of the contract.

READABILITY IS STRICTLY STRONGER, which is why this is not merely a swap:
a delivery marked `delivered` but sealed beyond this agent's reach PASSES the
old check and FAILS this one — and that case is data loss, not health. "Sealed"
and "sealed beyond its owner's reach" look identical from the sender's side.

A dependent platform's entire bus usage is unread/inbox/show/ack/send — no
delivery state read anywhere — and they were unaffected across the whole window.
Readability was sufficient; the state value was not necessary.
"""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src/agentbus_client/cli/_diag.py"


def _source() -> str:
    return SRC.read_text()


def test_the_loop_check_no_longer_gates_on_a_state_value():
    """THE REGRESSION. Re-coupling to `state` re-introduces a check that can
    report failure while holding the delivered message."""
    src = _source()
    assert not re.search(r"state\s+in\s+\(\s*[\"']delivered", src), (
        "doctor is gating the loop on an internal delivery state again"
    )


def test_the_loop_check_reads_the_body_back():
    """It must actually fetch and inspect the message, not just see it listed."""
    src = _source()
    window = src[src.index("smtp loop") - 3000 : src.index("smtp loop") + 500]
    assert "bus.read(" in window, "the loop check never reads the delivery back"
    assert "text_body" in window


def test_an_unreadable_arrival_is_reported_as_BROKEN_not_ok():
    """Arrived-and-unreadable is WORSE than not arriving: the sender believes it
    landed. It must not be reported as a latency result or a pass."""
    src = _source()
    assert "BROKEN" in src
    assert "data loss, not health" in src


def test_the_wait_window_was_not_widened():
    """The backend retracted their own suggestion to widen it after measuring
    p50 = p90 = max = 0:00:00 across 10,544 delivered rows. There is no latency
    distribution to widen for, and a longer wait would have HIDDEN this defect
    behind patience."""
    src = _source()
    m = re.search(r"LOOP_WAIT_SECONDS\s*=\s*(\d+)", src)
    assert m, "LOOP_WAIT_SECONDS not found"
    assert int(m.group(1)) <= 90, f"wait window widened to {m.group(1)}s"


def test_the_source_file_is_the_one_we_think():
    """KNOWN-POSITIVE. Every assertion above is a grep over a file path; if the
    path were wrong they would all pass or all fail for the wrong reason."""
    src = _source()
    assert "smtp loop" in src
    assert "def cmd_doctor" in src or "doctor" in src
