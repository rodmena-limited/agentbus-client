# 0079 — attachment --all silently overwrites attachments that share a filename

Ticket: issuedb #79. Found by the #73 mutation-testing round.

## SPEC AND EVIDENCE (from the ticket)

EARS SPEC:
- If two or more attachments on a delivery would be written to the same path by --all (same name, or the same name after sanitising), then the CLI shall refuse before fetching or writing anything, exit 1, and name the clashing indexes.
- The refusal shall hold with --force, which governs files already on disk, not attachments of the same message.
REPRODUCED 2026-10-04 via cli.cmd_attachment(--all) with two attachments named report.pdf: rc 0, 'wrote report.pdf' twice, '2 attachment(s) written', one file on disk holding the SECOND attachment; the first was lost.
FOUND BY: #73 triage (cli/_read group).
SYNTHESIS (localised bugfix): CLI file output; concept: detect duplicate targets in the planning pass that already checks pre-existing files. Alternative rejected: auto-suffix (report-1.pdf) — renames silently, against _safe_attachment_name's refuse-rather-than-rename stance.

## FIX

The planning pass of `--all` groups targets by path and refuses on any shared path, before fetching.

## VERIFICATION

tests/test_attachment_all_refuses_shared_names.py (same name with and without --force, collision after sanitising, distinct names written). Each new test was run against the unfixed code and failed there.
