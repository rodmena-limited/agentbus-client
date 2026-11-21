from __future__ import annotations

import errno
import json
import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from ._settings_shape import _names_the_file

PRIVATE_MODE = stat.S_IRUSR | stat.S_IWUSR


def _current_umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


def _target_mode(target: Path, *, private: bool) -> int:
    if private:
        return PRIVATE_MODE
    try:
        return stat.S_IMODE(target.stat().st_mode)
    except FileNotFoundError:
        return 0o666 & ~_current_umask()


def _refuse_read_only(target: Path, shown: Path) -> None:
    if target.exists() and not os.access(target, os.W_OK):
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(shown))


def write_text_atomic(path: Path, text: str, *, private: bool = False) -> None:
    target = path.resolve() if path.is_symlink() else path
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        _refuse_read_only(target, path)
        mode = _target_mode(target, private=private)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            tmp.chmod(mode)
            tmp.replace(target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    except OSError as exc:
        exc.filename = str(path)
        exc.filename2 = None
        raise


def _dump_json(path: Path, data: dict[str, Any], *, private: bool = False) -> None:
    text = json.dumps(data, indent=2) + "\n"
    with _names_the_file(path):
        write_text_atomic(path, text, private=private)
