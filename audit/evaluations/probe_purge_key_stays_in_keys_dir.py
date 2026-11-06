#!/usr/bin/env python3
"""#92: a checkout cannot name a file for `teardown --purge-key` to delete.

Real subprocess under a throwaway HOME. The real ~/.config/agentbus is never touched.
On 0.9.105 and 0.9.106 the hostile name deletes operator.env.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = os.environ.get("AGENTBUS_CLIENT_SRC") or str(REPO / "src")
PY = os.environ.get("PYTHON") or sys.executable
TEARDOWN = (
    "from agentbus_client.cli import main; raise SystemExit(main(['teardown', '--purge-key']))"
)


def teardown(root: Path, name: str) -> tuple[int, Path, Path]:
    cfg = root / "cfg"
    (cfg / "keys").mkdir(parents=True)
    operator = cfg / "operator.env"
    operator.write_text("export AGENTBUS_API_KEY=ab_sk_OPERATOR\n")
    bound = cfg / "keys" / f"{name}.env"
    if "/" not in name:
        bound.write_text("export AGENTBUS_API_KEY=ab_sk_BOUND\n")
    project = root / "project"
    (project / ".agentbus").mkdir(parents=True)
    (project / ".agentbus" / "agent").write_text(f"{name}\n")
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(root),
        "PYTHONPATH": SRC,
        "AGENTBUS_CONFIG_DIR": str(cfg),
    }
    done = subprocess.run(
        [PY, "-c", TEARDOWN], capture_output=True, text=True, env=env, cwd=project, check=False
    )
    return done.returncode, operator, bound


def main() -> int:
    hostile = Path(tempfile.mkdtemp(prefix="probe-purge-hostile-"))
    honest = Path(tempfile.mkdtemp(prefix="probe-purge-honest-"))
    try:
        code, operator, _ = teardown(hostile, "../operator")
        refused = code != 0 and operator.exists()
        print(f"{'ok  ' if refused else 'FAIL'} ../operator exit={code} kept={operator.exists()}")
        code, operator, bound = teardown(honest, "real-agent")
        purged = code == 0 and not bound.exists() and operator.exists()
        print(f"{'ok  ' if purged else 'FAIL'} real-agent exit={code} purged={not bound.exists()}")
    finally:
        shutil.rmtree(hostile, ignore_errors=True)
        shutil.rmtree(honest, ignore_errors=True)
    return 0 if refused and purged else 1


if __name__ == "__main__":
    raise SystemExit(main())
