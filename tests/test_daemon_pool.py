"""#96: SDK pool workers are daemon threads on every supported Python.

Python 3.14 changed the private `_worker` entry point from four parameters to
three. The pool then used stock workers, and a call blocked in a read held the
interpreter open at exit, the hang #26 removed.
"""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, thread as stdlib_thread
from pathlib import Path
from typing import TYPE_CHECKING

import agentbus_client
from agentbus_client.client import _daemon_pool

if TYPE_CHECKING:
    import pytest

SOURCE = str(Path(agentbus_client.__file__).parent.parent)
BLOCKED_AT_EXIT = """
import threading, time
from agentbus_client.client._daemon_pool import _daemon_executor
pool = _daemon_executor(1, "probe")
started = threading.Event()
pool.submit(lambda: (started.set(), time.sleep(120)))
assert started.wait(10)
print("leaving")
"""


def test_a_worker_blocked_at_exit_does_not_hold_the_interpreter() -> None:
    done = subprocess.run(
        [sys.executable, "-c", BLOCKED_AT_EXIT],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "PYTHONPATH": SOURCE},
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "leaving"


def test_a_worker_of_an_unknown_shape_falls_back_to_the_stock_pool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unknown(_a: object, _b: object) -> None:
        return None

    monkeypatch.setattr(stdlib_thread, "_worker", unknown)
    pool = _daemon_pool._daemon_executor(1, "probe")
    try:
        assert type(pool) is ThreadPoolExecutor
        monkeypatch.undo()
        assert pool.submit(lambda: 7).result(timeout=5) == 7
    finally:
        pool.shutdown(wait=True)


def test_a_daemon_worker_is_reused_and_carries_results_back() -> None:
    pool = _daemon_pool._daemon_executor(1, "probe")
    try:
        assert pool.submit(lambda: 7).result(timeout=5) == 7
        assert pool.submit(sum, [1, 2]).result(timeout=5) == 3
        assert len(pool._threads) == 1
    finally:
        pool.shutdown(wait=True)
