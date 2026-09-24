"""Mutation probe for the #64/#65 render paths.

A green suite says the tests pass. It does not say they would fail if the code
were wrong. This probe breaks the source on purpose, one edit at a time, and
asserts the suite goes red for each. A mutation that SURVIVES is an assertion
that was never there.

Found two survivors, in two runs a few minutes apart: `thread_signature_line` rendered a bad verdict as
VALID with the whole suite still green, because the VALID branch was asserted
only in the direction where it should be VALID. Same shape
vellum-api-macbook-team-f82400 hit on `mounted_this_boot` the same evening.

An ambiguous anchor reports HARNESS-FAILED, never SKIP: a mutation that was
never applied is not a mutation that was caught, and collapsing the two is the
defect this whole probe exists to find.

    .venv/bin/python audit/evaluations/probe_signature_render_mutations.py

Exit 0 when every mutation is caught.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SIG = ROOT / "src/agentbus_client/cli/_sigline.py"
READ = ROOT / "src/agentbus_client/cli/_read.py"

TESTS = [
    "tests/test_show_renders_signature_state.py",
    "tests/test_unverifiable_is_not_a_failed_signature.py",
    "tests/test_raw_is_byte_exact.py",
    "tests/test_show_raw_ciphertext.py",
    "tests/test_show_reads_the_whole_thread.py",
]

MUTATIONS: list[tuple[pathlib.Path, str, str, str]] = [
    (
        SIG,
        '    if not served:\n        return [\n            "Signed:  UNKNOWN',
        '    if False:\n        return [\n            "Signed:  UNKNOWN',
        "not-served gate removed: UNKNOWN can never render",
    ),
    (
        SIG,
        '    if signed and state == "valid":\n        return [\n            f"Signed:  yes{key}',
        '    if signed:\n        return [\n            f"Signed:  yes{key}',
        "delivery line: a bad verdict renders as a pass",
    ),
    (
        SIG,
        '    if signed and state == "valid":\n        key = f" key {fingerprint}" if fingerprint'
        ' else ""\n        return f"    signed: the bus says VALID',
        '    if signed:\n        key = f" key {fingerprint}" if fingerprint'
        ' else ""\n        return f"    signed: the bus says VALID',
        "thread line: a bad verdict renders as VALID",
    ),
    (
        SIG,
        "    blocked = _blockers(delivery)",
        "    blocked = []",
        "structural-absence explanation never fires",
    ),
    (
        SIG,
        '    block = (delivery.get("provenance") or {}).get("signature")',
        "    block = None",
        "provenance ignored, flat fields only",
    ),
    (
        SIG,
        'check = f"         check it yourself:  agentbus verify-sender {delivery_id}"',
        'check = "         "',
        "the verify-sender command is dropped",
    ),
    (
        SIG,
        "    if any(_source(m)[0] for m in messages):\n        return None",
        "    return None",
        "thread caveat never fires",
    ),
    (
        SIG,
        '        return True, bool(block.get("signed")), block.get("state"),'
        ' block.get("key_fingerprint")',
        '        return True, bool(block.get("signed")), "valid", block.get("key_fingerprint")',
        "provenance state forced to valid: invalid renders as a pass",
    ),
    (
        SIG,
        '        return True, bool(block.get("signed")), block.get("state"),'
        ' block.get("key_fingerprint")',
        '        return True, bool(block.get("signed")), None, block.get("key_fingerprint")',
        "provenance state dropped: an invalid message loses its verdict",
    ),
    (
        SIG,
        '    if signed and state == "unverifiable":\n        return [',
        "    if False:\n        return [",
        "#68 show: unverifiable falls into the failure wording",
    ),
    (
        SIG,
        '    if signed and state == "unverifiable":\n        return "',
        '    if False:\n        return "',
        "#68 thread: unverifiable falls into the failure wording",
    ),
    (
        SIG,
        '    if state == "unverifiable":\n        return (',
        "    if False:\n        return (",
        "#68 notice: unverifiable falls into the failure wording",
    ),
    (
        READ,
        "            buffer.write(body.encode())",
        '            buffer.write(body.encode() + b"\\n")',
        "--raw appends a newline again (#65)",
    ),
    (
        READ,
        "    for line in _sigline.signature_lines(delivery, args.delivery_id):\n        print(line)",
        "    pass",
        "show prints no Signed: line at all (#64)",
    ),
]


def main() -> int:
    results = []
    for path, old, new, label in MUTATIONS:
        original = path.read_text()
        count = original.count(old)
        if count != 1:
            results.append((label, "HARNESS-FAILED", f"anchor matched {count}x — NOT a result"))
            continue
        path.write_text(original.replace(old, new))
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", *TESTS, "-q", "--no-header"],
                capture_output=True,
                text=True,
                cwd=ROOT,
            )
            tail = [ln for ln in proc.stdout.splitlines() if "passed" in ln or "failed" in ln]
            results.append(
                (label, "BIT" if proc.returncode else "SURVIVED", tail[-1] if tail else "")
            )
        finally:
            path.write_text(original)

    print(f"{'mutation':56} {'verdict':16} detail")
    print("-" * 110)
    for label, verdict, detail in results:
        print(f"{label:56} {verdict:16} {detail}")
    unclean = [r for r in results if r[1] != "BIT"]
    print(f"\n{len(results) - len(unclean)}/{len(results)} mutations caught")
    for label, verdict, _ in unclean:
        print(f"  {verdict}: {label}")
    return 1 if unclean else 0


if __name__ == "__main__":
    raise SystemExit(main())
