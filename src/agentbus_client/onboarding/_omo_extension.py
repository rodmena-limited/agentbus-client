from __future__ import annotations

import hashlib
import os
import tempfile
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING

from agentbus_client._omo import omo_extension_dir

if TYPE_CHECKING:
    from collections.abc import Callable

EXTENSION_REPO = "rodmena-limited/agentbus-omo-plugin"
EXTENSION_COMMIT = "181469caf0ce0ca88b39da131e9002afa6e7002a"
EXTENSION_FILES = {
    "index.ts": "937862ca5e0d174d7f151abc79c147315084f007ff370f672c078c2d22e461f8",
    "package.json": "c0b1e3c49f0f152772d0f7e0cfdca2dcb9d96c83d3e9d7c41b2c3e8756695268",
}
PINNED = "pinned"
DIFFERS = "differs"
MISSING = "missing"


def extension_url(filename: str) -> str:
    return f"https://raw.githubusercontent.com/{EXTENSION_REPO}/{EXTENSION_COMMIT}/{filename}"


def _sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def extension_state(directory: Path | None = None) -> tuple[str, list[str]]:
    directory = directory or omo_extension_dir()
    if not (directory / "index.ts").exists():
        return MISSING, []
    differing = [
        name for name, digest in EXTENSION_FILES.items() if _sha256(directory / name) != digest
    ]
    return (DIFFERS, differing) if differing else (PINNED, [])


def _http_fetch(url: str) -> bytes:
    import httpx

    resp = httpx.get(url, timeout=20, follow_redirects=True)
    if resp.status_code != HTTPStatus.OK:
        msg = f"{url} answered {resp.status_code}"
        raise OSError(msg)
    return resp.content


def _write_atomic(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        Path(tmp).chmod(0o644)
        Path(tmp).replace(path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def install_extension(
    report: list[str],
    fetch: Callable[[str], bytes] = _http_fetch,
    directory: Path | None = None,
) -> bool:
    directory = directory or omo_extension_dir()
    short = EXTENSION_COMMIT[:7]
    state, differing = extension_state(directory)
    if state == PINNED:
        report.append(f"extension: {directory} is already at {short}; left as is")
        return True
    if state == DIFFERS:
        report.append(
            f"extension: NOT replaced. {directory} holds files that differ from {short} "
            f"({', '.join(differing)}). To install {short}, move that directory aside and "
            "run `agentbus setup omo` again."
        )
        return False
    fetched: dict[str, bytes] = {}
    for name, digest in EXTENSION_FILES.items():
        url = extension_url(name)
        try:
            data = fetch(url)
        except OSError as exc:
            report.append(f"extension: NOT installed; fetching {url} failed: {exc}")
            return False
        actual = hashlib.sha256(data).hexdigest()
        if actual != digest:
            report.append(
                f"extension: NOT installed; {url} has sha256 {actual}, expected {digest}. "
                "Nothing was written."
            )
            return False
        fetched[name] = data
    directory.mkdir(parents=True, exist_ok=True)
    for name, data in fetched.items():
        _write_atomic(directory / name, data)
    report.append(
        f"extension: installed {EXTENSION_REPO} at {short} into {directory} "
        "(sha256 checked). Restart any omo session that was already running."
    )
    return True
