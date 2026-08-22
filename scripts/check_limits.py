"""House engineering limits that no linter enforces.

Both of these were breached and shipped in the days before CI existed:
`cli/_register.py` reached 569 lines (past the 550 HARD cap) and nothing
noticed, because the only check was somebody happening to look.

Exit non-zero on breach. Kept dependency-free so it runs anywhere.
"""

from __future__ import annotations

import sys
from pathlib import Path

SOFT, HARD = 500, 550
ROOT = Path(__file__).resolve().parents[1]

# Generated artifacts, fixtures, lockfiles and data blobs are out of scope —
# the cap is about code a person has to read.
SKIP_PARTS = {".venv", "__pycache__", "build", "dist", ".git", "node_modules"}


def main() -> int:
    over_hard: list[tuple[int, Path]] = []
    over_soft: list[tuple[int, Path]] = []
    for path in sorted(ROOT.rglob("*.py")):
        if SKIP_PARTS & set(path.parts):
            continue
        try:
            n = len(path.read_text().splitlines())
        except OSError:
            continue
        rel = path.relative_to(ROOT)
        if n > HARD:
            over_hard.append((n, rel))
        elif n > SOFT:
            over_soft.append((n, rel))

    for n, rel in over_soft:
        print(f"warning: {rel} is {n} lines (soft cap {SOFT}) — refactor before {HARD}")
    for n, rel in over_hard:
        print(f"ERROR: {rel} is {n} lines, past the {HARD}-line HARD cap")

    if over_hard:
        print(f"\n{len(over_hard)} file(s) over the hard cap.")
        return 1
    print(f"file sizes OK ({len(over_soft)} over soft cap, 0 over hard cap)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
