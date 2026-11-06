# 0089 — Full ruff and mypy --strict over the whole repo

Ticket: issuedb #89.

EARS SPEC:
R1. The ruff configuration shall set select = ["ALL"] and target-version = "py310" (matching requires-python).
R2. Every rule in ruff ignore, per-file-ignores, flake8-self ignore-names, and every inline noqa or type-ignore shall have its reason recorded in SPECS/0089, classified as: tool conflict, house rule, public contract, or no compliant form.
R3. When `ruff check .` runs on the repository root, ruff shall report 0 findings; `ruff format --check .` shall report 0 files to reformat.
R4. The mypy configuration shall set strict = true with no disable_error_code, and shall check src, tests, scripts and audit; `mypy` shall report 0 errors.
R5. Inline type-ignores in src shall not exceed the 18 present at baseline, and each shall name its error code.
R6. If a change would alter a public name, signature or exit code, then that change shall not be made: inspect.signature of every public AgentBus and AsyncAgentBus method and the __all__ of agentbus_client shall show 0 differences against the 0.9.105 wheel, and the 0.9.94 CLI parser oracle test shall pass unmodified.
R7. The test suite shall collect >= 3576 tests outside tests/integration, with 0 failures, and with no existing test deleted or skipped.
R8. Where a scoped mutation module is refactored, a forkserver mutmut run after the change shall report, per module, a killed count ratio no lower than the baseline run on 0.9.105, and any new survivor shall be classified.
R9. scripts/check_limits.py shall exit 0: 0 files over the 550-line hard cap.
R10. The CI file .rodmena/ci.yml shall run the same ruff and mypy commands as R3 and R4, with the per-tree disable-error-code list removed.
R11. When the gates pass, version 0.9.106 shall be published to PyPI, and the wheel fetched back from PyPI into a clean virtualenv shall pass a CLI smoke test.

TECHNICAL PROBLEMS:
1. Policy: separating findings that are real from findings a rule cannot express correctly here, without a blanket ignore.
2. Re-export surface: mypy strict no_implicit_reexport against modules kept for import compatibility.
3. Late binding: tests patch helpers on the defining module, so moving lazy imports to module scope changes what patches see.
4. Complexity: about 40 functions over C901/PLR09xx, some in the guard gate and retry core, which must keep their exact failure modes.
5. Types in tests: 2427 strict errors; doubles stand in for real types.
6. CI types job is not running: `mypy tests` stops at a duplicate-module error on conftest, so tests have had no type check.

SOLUTION DOMAINS:
- Static analysis policy: ruff rule catalogue and its documented formatter conflicts (COM812).
- Python packaging: PEP 561 re-export rules (__all__ is the explicit-export form); mypy explicit_package_bases.
- Refactoring: extract-function with mutation testing as the behaviour oracle (audit/mutation, tickets #73 and #80).
- Typing test doubles: typing.cast at the injection boundary, typing.Protocol.
- Durable specs applied: file_size_cap, null_is_absence_not_default, resilience_breaker_classifier.

ALTERNATIVES:
- Rule selection: ALL minus recorded exceptions [CHOSEN: R2 makes each exception auditable] vs extending the curated list [REJECTED: new rule families stay off by default, opposite of the request].
- Preview rules: off [CHOSEN: DOC* is the bulk of the delta (373 of 963 in src alone), which is the same docstring mandate the house rule rejects; preview rules also change between ruff patch releases] vs preview = true [REJECTED].
- Re-exports: explicit imports plus __all__ [CHOSEN: removes the F401/F403 per-file ignores and satisfies no_implicit_reexport] vs per-module implicit_reexport = true [REJECTED: it is a strict flag switched off] vs rewriting importers to defining modules only [REJECTED: tests and consumers import from the package].
- Lazy imports: keep, PLC0415 off [CHOSEN: late binding for patches on defining modules, and import cost: httpx is 63 ms of the 98 ms `import agentbus_client.cli` measured 2026-11-06; the repo has no recorded start-time budget] vs moving 417 imports to module scope [REJECTED: changes what a patch on the defining module reaches, and start time].
- Complexity: extract functions at default thresholds [CHOSEN] vs raising max-complexity [REJECTED: a raised threshold is an ignore under another name]. PLR0913 on the public SDK cannot be fixed without breaking R6, so it stays as a recorded public-contract exception.
- Blind except: narrow where the raisers are known, keep at a process boundary with a line-level noqa [CHOSEN: rule stays live for new code in the file] vs narrowing all [REJECTED: changes fail-open and fail-closed behaviour in hooks] vs per-file ignore [REJECTED: hides new sites].
- Test doubles: cast at the injection boundary [CHOSEN: the double stays a double, nothing subclasses production classes] vs disabled error codes for tests [REJECTED: that is what R4 removes].
- tests as a package: add __init__.py [CHOSEN: resolves the conftest duplicate and INP001] vs mypy --explicit-package-bases alone [REJECTED if __init__.py passes the suite; audit/ and scripts/ stay plain scripts because probes import _common as a sibling].
