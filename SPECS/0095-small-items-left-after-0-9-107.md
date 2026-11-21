# 0095 — Small items left after 0.9.107

Ticket: issuedb #95.

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
5. MEASURED ruff 0.17.4 reports 11 ICN001 findings (`import datetime` wants an alias). CI pins ruff 0.16.9 through `uv.lock`, so this appears on the next lock upgrade.
6. MEASURED `rodmenalimited/ci-python` on Docker Hub answers for 3.12 and not for 3.10, 3.11, 3.13 or 3.14, while `.rodmena/ci.yml` asks for 3.10 to 3.13. Whether ci.rodmena.co.uk pulls from another registry was not checked. 3.14 is not in the CI matrix for that reason.

## Resolution (2026-11-21, release 0.9.109)

1. FIXED. `_dump_json` moved to `onboarding/_atomic_write.py`. `_paths.py` is now 489 lines. It writes a temp file in the same directory, fsyncs it, sets the mode and renames it over the target. If the write fails, the temp file is removed. A read-only target is refused with EACCES naming the file, never renamed over. A symlinked settings file keeps its link, because the write goes to the link's target. An existing mode is kept, a new file follows the umask, and private files are 0600 from the moment the temp file is created. Tests: tests/test_settings_write_is_atomic.py (8), plus the reworked ENOSPC test in test_setup_stops_plainly_on_unwritable_files.py. With the old writer, 4 of these fail. One of them, the SIGKILL-before-rename test, goes red on the old writer only because the old writer replaced the content: it never calls rename, so the kill never fires.
2. FIXED. A `.gitignore` OSError now gives one line: `gitignore: NOT updated (<reason>); add .agentbus/ to <path> by hand ...`. Identity setup still continues. tests/test_gitignore_failure_is_reported.py: the read-only case fails on the old code and the writable case passes on both.
3. FIXED. Both attachment settings go through `_env_numbers.env_int(minimum=1)`. An unusable value gives one warning per (name, value) on stderr and the default is used. An empty value stays silent, as before. tests/test_attachment_settings_warn.py: 10 tests, 8 of which fail on the old code.
4. FIXED. tests/conftest.py sets `PYTHONDONTWRITEBYTECODE=1` and `sys.dont_write_bytecode` at import, so child interpreters inherit them. tests/test_subprocess_tests_write_no_bytecode.py: 2 tests, both fail without the conftest change.
5. FIXED. 11 ICN001 sites now use `import datetime as dt`. Both `uvx ruff@0.17.4 check src tests` and the locked ruff report no findings.
6. NOT A CODE FIX. Re-measured 2026-11-21: Docker Hub answers 404 for `rodmenalimited/ci-python` and for each of its tags 3.10 to 3.14, including 3.12, which answered on 2026-11-06. The same query for `library/python:3.12` answered 200, so the check can detect an image that exists. Whether ci.rodmena.co.uk pulls from a private registry is a question for the CI team and was asked over AgentBus.
