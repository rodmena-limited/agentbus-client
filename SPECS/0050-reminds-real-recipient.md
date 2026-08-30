# 0050 — reminds shows the real recipient

Ticket #50. Server fields from agentbus-8dc08d (#316), reported by
financial-freedom-projec-195737 who set reminders for household members.

EARS SPEC:
- When `agentbus reminds` lists a reminder, the client SHALL render the recipient using the server's `self_addressed` flag: "(you)" when true, the `target` name when false.
- If `target` is null, then the client SHALL say the recipient is unknown and SHALL NOT fall back to "(you)" — guessing the sender is the defect the field exists to remove.
- The client SHALL NOT compare `target` against its own identity to decide self-addressing; the server computes it.

TECHNICAL PROBLEMS:
1. A renderer inferring a fact the payload now states. `or "(you)"` is an
   inference that is right in the common case and silently wrong in the two
   that matter: a targeted reminder (before the field existed) and a null
   target (recipient gone).
2. Client-side recomputation of a server-computed fact. Comparing `target` to
   whoami would put an identity comparison on the hot path of every listing and
   get it wrong exactly once.

SOLUTION DOMAINS:
- The delivery_vs_readability memory already records the governing principle:
  each side asserts what only it can know. `self_addressed` is the server's to
  compute; the client's job is to render it, not to re-derive it.
- This repo's own rule from #48/#49: null means UNKNOWN, never a default.

ALTERNATIVES:
- SELF-DETECTION: compare `target` to whoami [REJECTED: recomputes a fact the
  payload states, costs an identity lookup per row, and the backend explicitly
  computed the flag to remove this] vs USE `self_addressed` [CHOSEN].
- NULL TARGET: fall back to "(you)" [REJECTED: it is the defect — the reminder
  is for someone who no longer exists, and claiming it is yours is worse than
  saying nothing] vs SAY IT IS UNKNOWN [CHOSEN].
