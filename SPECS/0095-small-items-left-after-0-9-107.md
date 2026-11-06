# 0095 — Small items left after 0.9.107

Ticket: issuedb #95. Open.

EARS SPEC:
- If a settings write is interrupted, then the client shall leave the previous file intact, and it shall not overwrite a file the operator made read-only.
- If `.gitignore` cannot be written during worktree identity setup, then the client shall say so in one line.
- If an attachment size setting in `client/models.py` is unusable, then the client shall fall back to its default with one plain warning.
- The test suite shall run with no stale bytecode in subprocess tests.

SYNTHESIS: chore-sized. The solution domain is robustness at process boundaries. None of these crashes or leaks a key. `onboarding/_paths.py` sits at 499 lines, so `_dump_json` has to move before it can change.

ITEMS:
1. READ `_dump_json` writes in place, so an interrupted write can truncate a settings file.
2. READ `_write_worktree_identity` suppresses a `.gitignore` OSError with no message.
3. READ the attachment settings in `client/models.py` fall back with no warning, unlike the settings in `_env_numbers.py`.
4. MEASURED `PYTHONDONTWRITEBYTECODE` does not reach subprocess tests. A same-size edit within one mtime second keeps stale bytecode, which affects mutation proofs and nothing else.
