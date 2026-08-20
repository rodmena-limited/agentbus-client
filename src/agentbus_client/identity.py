"""Derivable session identity — so a reopened session does not have to remember.

The zombie problem had one cause: an agent's identity lived only in a name a
human or a prompt had to recall. Get it wrong, or omit it, and a brand new
identity was minted silently. Five sessions reopened ten times became fifty
agents, forty-five of them permanently unread and all holding routable
addresses.

Identity is derived instead from where the session actually is:

    session_key = sha256(device_id : repo_fingerprint : sha256(abs_path))[:16]
    identity    = (workspace, session_key, role)

Everything here is computed locally and cheaply, so `register(role=...)` needs
no arguments a caller could get wrong.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import uuid
from pathlib import Path

# Deliberately a generated UUID persisted on disk, NOT a hostname and NOT
# /etc/machine-id. Hostnames change (and leak the owner), and machine-id is
# stable but is also a fingerprint other software keys off — reusing it would
# make AgentBus identities correlatable with unrelated systems. A UUID we mint
# ourselves carries nothing and is ours to rotate.
_DEVICE_FILE = "device-id"


def config_dir() -> Path:
    root = os.environ.get("AGENTBUS_CONFIG_DIR")
    return Path(root) if root else Path.home() / ".config" / "agentbus"


def device_id() -> str:
    """This machine's stable id, created on first use.

    `AGENTBUS_DEVICE_ID` overrides it, which is how a fleet of identical
    containers can deliberately share one identity instead of minting a new
    agent per container.
    """
    override = os.environ.get("AGENTBUS_DEVICE_ID")
    if override:
        return override.strip()

    path = config_dir() / _DEVICE_FILE
    try:
        existing = path.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass

    generated = str(uuid.uuid4())
    try:
        from .sealing import create_secret_exclusive

        # Atomic and 0600 from birth (review #23, #30/S2): two first-run
        # processes must agree on ONE device id, and it identifies this machine
        # to a shared workspace, so it is never world-readable even briefly.
        if not create_secret_exclusive(path, generated + "\n"):
            existing = path.read_text().strip()
            if existing:
                return existing
    except OSError:
        # A read-only home must not stop an agent registering. It degrades to a
        # per-process device id, which means an ephemeral identity — correct,
        # because a machine that cannot persist this genuinely is one.
        pass
    return generated


def is_ephemeral() -> bool:
    """Is this a throwaway environment whose device id will never recur?

    CI runners and fresh containers mint a new device id per run, so without
    this they would grow identities faster than any sweep reclaims them. Erring
    toward True is safe: an ephemeral agent is reclaimed in hours, and
    re-registering brings it straight back.
    """
    if os.environ.get("AGENTBUS_EPHEMERAL", "").lower() in ("1", "true", "yes"):
        return True
    # The near-universal convention, set by GitHub Actions, GitLab CI, CircleCI,
    # Travis, Buildkite and others.
    if os.environ.get("CI", "").lower() in ("1", "true", "yes"):
        return True
    for marker in (
        "GITHUB_ACTIONS",
        "GITLAB_CI",
        "BUILDKITE",
        "JENKINS_URL",
        "CIRCLECI",
        "TF_BUILD",
    ):
        if os.environ.get(marker):
            return True
    # A container with no persisted device file: the id was generated this run
    # and will not survive it.
    return bool(Path("/.dockerenv").exists() and not (config_dir() / _DEVICE_FILE).exists())


def git_remote(path: str | None = None) -> str | None:
    """The origin URL of the checkout containing `path`, if any."""
    try:
        result = subprocess.run(
            ["git", "-C", str(path or Path.cwd()), "remote", "get-url", "origin"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    url = result.stdout.strip()
    return url or None


def repo_fingerprint(remote_url: str) -> str:
    """Must match agentbus.ids.repo_fingerprint exactly.

    Two implementations of one hash is a drift bug waiting to happen; this is
    kept byte-identical to the server's on purpose, and the shape is asserted by
    the test suite rather than trusted.
    """
    normalized = remote_url.strip().lower()
    normalized = re.sub(r"^(https?://|ssh://|git\+ssh://)", "", normalized)
    normalized = re.sub(r"^git@", "", normalized)
    normalized = normalized.replace(":", "/", 1) if "@" not in normalized[:1] else normalized
    normalized = normalized.removesuffix(".git").rstrip("/")
    return hashlib.sha256(normalized.encode()).hexdigest()[:12]


def path_hash(absolute_path: str) -> str:
    normalized = (absolute_path or "").rstrip("/\\") or "/"
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def session_key(
    device: str | None = None, repo: str | None = None, path: str | None = None
) -> str | None:
    """The local half of a derivable identity. Mirrors agentbus.ids.session_key."""
    parts = [p for p in (device, repo, path_hash(path) if path else None) if p]
    if not parts:
        return None
    return hashlib.sha256(":".join(parts).encode()).hexdigest()[:16]


def describe(workdir: str | None = None) -> dict[str, object]:
    """Everything needed to register, computed from the environment.

    Returns the RAW workdir too — the client sends it and the server hashes it,
    so the hashing rule lives in exactly one place. It is never stored raw.
    """
    path = str(Path(workdir).resolve() if workdir else Path.cwd())
    remote = git_remote(path)
    device = device_id()
    fingerprint = repo_fingerprint(remote) if remote else None
    return {
        "device_id": device,
        "workdir": path,
        "repo_remote": remote,
        "repo_fingerprint": fingerprint,
        "session_key": session_key(device, fingerprint, path),
        "ephemeral": is_ephemeral(),
    }
