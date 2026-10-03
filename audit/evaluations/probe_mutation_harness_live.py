from __future__ import annotations

import pathlib
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
MUTMUT = ROOT / ".venv" / "bin" / "mutmut"
TARGET = "src/agentbus_client/cli/_read.py"
PATTERN = "agentbus_client.cli._read.x__safe_attachment_name__mutmut_*"
LOCATION_TEST = "tests/test_mutation_run_imports_the_mutants.py"
WEAK_TEST = "tests/test_zz_weak_canary.py"
WEAK_BODY = (
    "from agentbus_client.cli._read import _safe_attachment_name\n\n\n"
    "def test_calls_without_asserting():\n"
    '    _safe_attachment_name("report.pdf", 0)\n'
)


def _mutmut_section(selection: list[str]) -> str:
    quoted = ", ".join(f'"{s}"' for s in selection)
    return (
        "[tool.mutmut]\n"
        'source_paths = ["src"]\n'
        f'only_mutate = ["{TARGET}"]\n'
        'also_copy = [".gitignore"]\n'
        'process_isolation = "forkserver"\n'
        f"pytest_add_cli_args_test_selection = [{quoted}]\n"
    )


def _scratch(selection: list[str], extra: dict[str, str]) -> pathlib.Path:
    work = pathlib.Path(tempfile.mkdtemp(prefix="mutmut-canary-"))
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(ROOT / "src", work / "src", ignore=ignore)
    shutil.copytree(ROOT / "tests", work / "tests", ignore=ignore)
    for name in (".gitignore", "uv.lock"):
        shutil.copy2(ROOT / name, work / name)
    pyproject = (ROOT / "pyproject.toml").read_text()
    head, sep, _ = pyproject.partition("[tool.mutmut]")
    if not sep:
        raise SystemExit("HARNESS-FAILED: pyproject.toml has no [tool.mutmut] section")
    (work / "pyproject.toml").write_text(head + _mutmut_section(selection))
    for rel, body in extra.items():
        (work / rel).write_text(body)
    return work


def _run(selection: list[str], extra: dict[str, str]) -> tuple[dict[str, int], str]:
    work = _scratch(selection, extra)
    try:
        run = subprocess.run(
            [str(MUTMUT), "run", "--max-children", "4", PATTERN],
            cwd=work,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        res = subprocess.run(
            [str(MUTMUT), "results", "--all", "true"],
            cwd=work,
            capture_output=True,
            text=True,
            timeout=300,
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    counts: dict[str, int] = {}
    for line in res.stdout.splitlines():
        m = re.match(r"\s*(\S*x__safe_attachment_name__mutmut_\d+): (.+)$", line)
        if m:
            counts[m.group(2)] = counts.get(m.group(2), 0) + 1
    log = f"run rc={run.returncode}\n{run.stdout[-1500:]}\n{run.stderr[-1500:]}"
    if run.returncode != 0:
        counts["harness-error"] = 1
    return counts, log


def main() -> int:
    strong, strong_log = _run(["tests/test_attachment_verb.py", LOCATION_TEST], {})
    weak, weak_log = _run([WEAK_TEST, LOCATION_TEST], {WEAK_TEST: WEAK_BODY})
    print(f"strong: {strong}")
    print(f"weak:   {weak}")
    failures = []
    if "harness-error" in strong or "harness-error" in weak:
        failures.append("mutmut exited non-zero (clean run or location check failed)")
    if sum(strong.values()) == 0 or sum(weak.values()) == 0:
        failures.append("no _safe_attachment_name mutants were reported; NOT a result")
    if strong.get("killed", 0) < 1:
        failures.append("strong run killed nothing: the harness cannot report a kill")
    if weak.get("survived", 0) < 1:
        failures.append("weak run had no survivor: the harness cannot report a survivor")
    if weak.get("killed", 0) >= strong.get("killed", 0):
        failures.append("weak run killed as many as the strong run: verdicts ignore the tests")
    if failures:
        print(strong_log)
        print(weak_log)
        for f in failures:
            print(f"FAIL: {f}")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
