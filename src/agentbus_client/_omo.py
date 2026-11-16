from __future__ import annotations

import contextlib
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from agentbus_client.identity import config_dir
from agentbus_client.sealing import agent_slug

START_TOLERANCE_SECONDS = 2.0
_ETIME_FIELDS = 3
LIVE = "live"
STALE = "stale"
ABSENT = "absent"
UNREADABLE = "unreadable"


@dataclass(frozen=True)
class OmoHolder:
    state: str
    path: Path | None
    pid: int | None = None
    reason: str = ""

    @property
    def live(self) -> bool:
        return self.state == LIVE


def omo_lock_path(agent: str) -> Path | None:
    if not agent or agent_slug(agent) != agent:
        return None
    return config_dir() / f"omo-watch-{agent}.lock"


def omo_extension_dir() -> Path:
    return Path.home() / ".omo" / "agent" / "extensions" / "agentbus"


def omo_skill_path() -> Path:
    return Path.home() / ".omo" / "agent" / "skills" / "agentbus" / "SKILL.md"


def _boot_epoch() -> float | None:
    with contextlib.suppress(OSError, ValueError):
        for line in Path("/proc/stat").read_text().splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    return None


def _proc_start_epoch(pid: int) -> float | None:
    boot = _boot_epoch()
    if boot is None:
        return None
    with contextlib.suppress(OSError, ValueError, IndexError):
        raw = Path(f"/proc/{pid}/stat").read_text()
        fields = raw[raw.rindex(")") + 2 :].split()
        ticks = int(fields[19])
        return boot + ticks / os.sysconf("SC_CLK_TCK")
    return None


def _parse_etime(text: str) -> int | None:
    text = text.strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        day_part, text = text.split("-", 1)
        days = int(day_part)
    parts = [int(p) for p in text.split(":")]
    while len(parts) < _ETIME_FIELDS:
        parts.insert(0, 0)
    hours, minutes, seconds = parts[-_ETIME_FIELDS:]
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def _ps_start_epoch(pid: int) -> float | None:
    try:
        out = subprocess.run(
            ["ps", "-o", "etime=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        ).stdout
        elapsed = _parse_etime(out)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return None if elapsed is None else time.time() - elapsed


def process_start_epoch(pid: int) -> float | None:
    started = _proc_start_epoch(pid)
    return started if started is not None else _ps_start_epoch(pid)


def _pid_state(pid: int) -> str:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "gone"
    except PermissionError:
        return "foreign"
    except OSError:
        return "gone"
    return "running"


def _read_lock(path: Path) -> tuple[int, float] | OmoHolder:
    try:
        text = path.read_text()
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return OmoHolder(ABSENT, path)
    except OSError as exc:
        return OmoHolder(UNREADABLE, path, reason=f"cannot read the lock: {exc}")
    try:
        pid = int(text.strip())
    except ValueError:
        return OmoHolder(UNREADABLE, path, reason=f"lock holds {text.strip()[:40]!r}, not a pid")
    if pid <= 0:
        return OmoHolder(UNREADABLE, path, reason=f"lock holds pid {pid}")
    return pid, mtime


def _judge(path: Path, pid: int, mtime: float) -> OmoHolder:
    state = _pid_state(pid)
    if state == "gone":
        return OmoHolder(STALE, path, pid, f"pid {pid} is not running")
    if state == "foreign":
        return OmoHolder(STALE, path, pid, f"pid {pid} belongs to another user")
    started = process_start_epoch(pid)
    if started is not None and started > mtime + START_TOLERANCE_SECONDS:
        return OmoHolder(
            STALE, path, pid, f"pid {pid} started after the lock was written, so it was reused"
        )
    return OmoHolder(LIVE, path, pid)


def omo_holder(agent: str) -> OmoHolder:
    path = omo_lock_path(agent)
    if path is None:
        return OmoHolder(ABSENT, None, reason="agent name cannot name an omo lock file")
    read = _read_lock(path)
    if isinstance(read, OmoHolder):
        return read
    return _judge(path, *read)
