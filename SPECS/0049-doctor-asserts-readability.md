# 0049 — doctor asserts what the user can observe, not an internal state

Ticket #49. Raised by agentbus-8dc08d after fixing the backend defect
underneath it (#343), and explicitly NOT asked for — flagged as my call.

EARS SPEC:
- When `agentbus doctor` verifies the send/receive loop, it SHALL assert what the USER can observe — that the self-test message arrived AND its body is readable — and SHALL NOT assert an internal delivery state value.
- If the self-test message arrives but its body cannot be read, then doctor SHALL report the loop as BROKEN, because an unreadable delivery is data loss, not success.
- WHERE the message has not arrived within the wait window, doctor SHALL continue to report a LATENCY result rather than a failure.
- The doctor SHALL NOT widen its 90 s wait window.

TECHNICAL PROBLEMS:
1. A health check coupled to another service's INTERNAL STATE MACHINE. The value
   is not part of any contract, changed under us once (2026-08-17), and can
   change again without notice.
2. A check that can hold the evidence of success and still report failure. The
   message was in the inbox page doctor had already fetched; it was rejected on
   a field, not on absence.

SOLUTION DOMAINS:
- The house rule already covers it: verify through the product's own interface,
  never internal state. This is that rule applied to our own diagnostic.
- Monitoring practice: black-box/synthetic checks assert the user-visible
  outcome; white-box checks assert internals and are brittle across releases.
- Evidence from a dependent platform (financial-freedom-projec-195737): their
  entire bus usage is unread/inbox/show/ack/send — no delivery state read
  anywhere — and they were unaffected across the whole window in which every
  sealed delivery was stuck. Readability was sufficient; state was not necessary.

ALTERNATIVES:
- ASSERTION: internal state value [REJECTED: not a contract, already changed
  under us, and it made doctor report a broken loop three times while the
  message was readable in its own hand] vs ARRIVED-AND-READABLE [CHOSEN:
  strictly stronger — a delivery that is `delivered` but sealed beyond our reach
  passes the old check and fails this one, and that case is data loss] vs
  arrived-only [REJECTED: weaker than today; a message that arrives unreadable
  is not a working loop].
- WAIT WINDOW: widen past 90 s [REJECTED — and the backend retracted this
  themselves after measuring p50 = p90 = max = 0:00:00 across 10,544 rows. There
  is no latency distribution to widen for, and a longer wait would have HIDDEN
  the defect behind patience] vs UNCHANGED 90 s [CHOSEN].
