"""Typed base for the watcher mixins (#48).

Same reasoning as `client/_mixin_base.py`: `watch.py`'s Watcher is assembled
from mixins split out for the file-size cap, so each mixin's references to
`self.state_path`, `self.cursor`, `self.bus` and the locks read as
`has no attribute` when mypy sees the mixin alone.

TYPE_CHECKING-only. At runtime the mixins still inherit `object`.
"""

from __future__ import annotations

from typing import Any


class WatcherBase:
    """What every watcher mixin may assume the assembled Watcher provides."""

    bus: Any
    agent: str | None
    workspace: str | None
    state_path: Any
    cursor: int
    on_message: Any
    _state_lock: Any
    _drain_lock: Any
    _drain_thread: Any
    _failures: int
    _last_failure_at: float

    def _save_cursor(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    def _load_state(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise NotImplementedError
