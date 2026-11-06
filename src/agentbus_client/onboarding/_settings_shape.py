from __future__ import annotations

import contextlib
import functools
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable, Iterable, Iterator
    from pathlib import Path


def _object_field(data: dict[str, Any], field: str, path: Path) -> dict[str, Any]:
    value = data.get(field)
    if value is None:
        value = data[field] = {}
    if not isinstance(value, dict):
        msg = (
            f'refusing to touch {path}: "{field}" is not a JSON object. '
            "Fix that value, then re-run setup."
        )
        raise SystemExit(msg)
    return value


def _command_hooks(hooks: object, event: str) -> list[dict[str, Any]]:
    groups = hooks.get(event) if isinstance(hooks, dict) else None
    handlers: list[dict[str, Any]] = []
    for group in groups if isinstance(groups, list) else []:
        listed = group.get("hooks") if isinstance(group, dict) else None
        if isinstance(listed, list):
            handlers.extend(handler for handler in listed if isinstance(handler, dict))
    return handlers


def _read_object_or_empty(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _append_ignore_entries(gitignore: Path, entries: Iterable[str]) -> None:
    existing = gitignore.read_bytes() if gitignore.exists() else b""
    separator = b"\n" if existing and not existing.endswith(b"\n") else b""
    lines = "".join(f"{entry}\n" for entry in entries)
    with gitignore.open("ab") as fh:
        fh.write(separator + lines.encode())


@contextlib.contextmanager
def _names_the_file(path: Path) -> Iterator[None]:
    try:
        yield
    except OSError as exc:
        exc.filename = exc.filename or str(path)
        raise


def _stops_plainly(
    verb: str,
) -> Callable[[Callable[[argparse.Namespace], int]], Callable[[argparse.Namespace], int]]:
    def decorate(
        command: Callable[[argparse.Namespace], int],
    ) -> Callable[[argparse.Namespace], int]:
        @functools.wraps(command)
        def run(args: argparse.Namespace) -> int:
            try:
                return command(args)
            except OSError as exc:
                where = f" at {exc.filename}" if exc.filename else ""
                msg = (
                    f"{verb} stopped{where}: {exc.strerror or exc}. "
                    f"Fix that, then run {verb} again."
                )
                raise SystemExit(msg) from exc

        return run

    return decorate
