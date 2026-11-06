from __future__ import annotations

import inspect
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor, thread as _cft
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from queue import SimpleQueue

_WORKER_TAKES_CONTEXT = 3
_WORKER_TAKES_INITIALIZER = 4


def _worker_parameters(worker: object) -> int:
    if worker is None:
        return 0
    try:
        return len(inspect.signature(worker).parameters)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _shape_is_known(parameters: int) -> bool:
    if parameters == _WORKER_TAKES_CONTEXT:
        return hasattr(ThreadPoolExecutor, "prepare_context")
    return parameters == _WORKER_TAKES_INITIALIZER


def _daemon_executor(max_workers: int, prefix: str) -> ThreadPoolExecutor:
    """A ThreadPoolExecutor whose workers are DAEMON threads and are not joined at exit.

    A stock executor's workers are non-daemon and registered for a join in
    concurrent.futures' shutdown hook, so a worker blocked in socket.recv (Ctrl-C one
    second into a 30s stall) held the interpreter open for the whole read timeout.
    Closing the httpx client does not wake a blocked read (issuedb #26). Daemon
    workers are abandoned at exit instead. The SDK's own shutdown hook has already
    flagged their sequences as abandoned.

    Relies on the private `_worker` entry point. It takes four arguments from 3.7
    through 3.13 and three from 3.14. The shape is CHECKED: any other shape gets the
    stock executor (issuedb #96).
    """
    worker = getattr(_cft, "_worker", None)
    parameters = _worker_parameters(worker)
    if not _shape_is_known(parameters):
        return ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=prefix)

    class _DaemonExecutor(ThreadPoolExecutor):
        def _worker_args(self, reference: object) -> tuple[object, ...]:
            pool: Any = self
            if parameters == _WORKER_TAKES_CONTEXT:
                return (reference, pool._create_worker_context(), pool._work_queue)  # noqa: SLF001
            return (reference, pool._work_queue, pool._initializer, pool._initargs)  # noqa: SLF001

        def _adjust_thread_count(self) -> None:
            if self._idle_semaphore.acquire(timeout=0):
                return
            queue = self._work_queue

            def _weakref_cb(_: object, q: SimpleQueue[Any] = queue) -> None:
                q.put(None)

            num_threads = len(self._threads)
            if num_threads < self._max_workers:
                t = threading.Thread(
                    name=f"{self._thread_name_prefix}_{num_threads}",
                    target=worker,
                    args=self._worker_args(weakref.ref(self, _weakref_cb)),
                    daemon=True,
                )
                t.start()
                self._threads.add(t)  # type: ignore[attr-defined]

    return _DaemonExecutor(max_workers=max_workers, thread_name_prefix=prefix)
