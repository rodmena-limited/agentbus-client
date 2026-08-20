"""Client-side sealing for encrypted workspaces (#189).

THE PRIVATE KEY NEVER LEAVES THIS MACHINE. It is generated here, written 0600
inside a 0700 directory, and read only by this process. Nothing in this module
transmits it, and there is no server endpoint that would accept it — if either
of those changes, the product has stopped being what it claims.

The sealing itself is age v1, implemented in `agentbus.agefmt` on the server
side and duplicated here so the client has no dependency on the server package.
Both are checked against the real age(1) and against committed vectors it
produced; see tests/test_age_format.py.

WHY THE CLIENT SEALS RATHER THAN THE SERVER: if the server sealed, the server
would have held the plaintext, and the whole claim collapses to "we promise we
did not keep it". The one place that is unavoidable is external mail arriving
from someone with no key, which is sealed at ingress and marked
`sealed_by: platform` precisely so the two are never confused.
"""

from __future__ import annotations

import os
import re
import stat
import time
from pathlib import Path
from typing import Any

# ALWAYS the vendored copy, never a conditional import of the server package.
#
# An earlier draft tried the server's module first and fell back to this one.
# That made the implementation in use depend on whether the server happened to
# be importable — so a developer checkout and a pip install could run different
# code, and only one of them is what customers get. The SDK ships alone; it
# uses its own copy always. tests/test_age_format.py fails the build if the two
# files drift, which is the guard that makes one copy safe rather than two
# implementations that agree by luck.
from ._agefmt import (
    CannotDecrypt,
    fingerprint,
    generate_keypair,
    public_from_private,
    seal,
    unseal,
)
from .identity import config_dir

__all__ = [
    "SEALED_MARKER",
    "CannotDecrypt",
    "MalformedSealed",
    "ensure_keypair",
    "fingerprint",
    "generate_keypair",
    "key_path",
    "load_private_key",
    "load_private_keys",
    "public_from_private",
    "seal",
    "seal_for_bytes",
    "unseal",
    "unseal_bytes",
    "unseal_bytes_with_any",
    "unseal_with_any",
]

SEALED_MARKER = "age-encryption.org/v1"


def _agent_slug(agent: str | None) -> str:
    """The acting agent's name, or the one this project is wired to.

    Falls back to $AGENTBUS_AGENT so every caller does not have to thread it,
    and raises rather than guessing when there is no agent at all: a sealing key
    with no owner is exactly the shared key this design replaced.
    """
    name = agent or os.environ.get("AGENTBUS_AGENT")
    if not name:
        raise ValueError(
            "no acting agent: a sealing key belongs to ONE agent, so it cannot be "
            "created or read without knowing which. Pass agent=..., or set "
            "AGENTBUS_AGENT (`agentbus setup` writes it into the project)."
        )
    # Separators become underscores, so nothing can traverse. `..` is then
    # collapsed too: it cannot escape the directory once `/` is gone, but a
    # filename containing `..` invites somebody to "fix" it later with a join
    # that does not sanitise.
    slug = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return slug.replace("..", "_") or "_"


# REG-8c (round-3.6 re-audit, bikeroom): the same sanitizer that decides
# credential filenames must also decide STATE-FILE filenames — wake, notify,
# gate-degraded, session-claim, rewake ledger, etc. Every one of them
# interpolates the acting agent's name into `os.path.join(dir, f"prefix-{agent}.ext")`,
# and every one of them read from the same `.agentbus/agent` file the REG-8
# threat model already named as attacker-controllable. Promoting the underlying
# slug function to a PUBLIC name (no leading underscore) so every filename
# builder in the client — credential OR state — shares one sanitizer at the
# interpolation point. Without this, the round-3 helper `bound_env_filename`
# was structurally .env-only, and the sibling state-file sites could not reuse
# it — that shape mismatch is what let the class survive round-3.5.
agent_slug = _agent_slug


def bound_env_filename(agent: str) -> str:
    """The traversal-safe filename for an agent's bound-key .env file.

    Named and exported so every call site that resolves keys/<agent>.env can
    share one sanitizer instead of remembering to inline _agent_slug themselves.

    REG-8 (round-3 audit) fixed client._key_from_disk to route the agent name
    through _agent_slug. Macbook's re-audit (round-3.5) found FOUR sibling
    call sites doing the same unsanitized path build:
      cli.py:_key_for_agent    (READ; called from _bus() on env-reversal path)
      cli.py join --name       (WRITE; hostile --name clobbers operator.env)
      cli.py setup             (WRITE; second onboarding path)
      cli.py service           (READ; passes into systemd unit EnvironmentFile)
      hooks/claude_code.py:_adopt_credential_for   (READ; MUTATES os.environ)
    Every one of them could be reached by a hostile .agentbus/agent, a hostile
    --name flag on `agentbus join`, or a hostile $AGENTBUS_AGENT. The correct
    class-of-bugs fix is one shared helper, not five parallel calls. Now:
      keys_dir / bound_env_filename(agent)     # traversal-safe
    A slug that matches no real file returns "_.env" or similar — every read
    path answers "no such file" and every write path lands inside keys/, so a
    hostile name cannot escape into operator.env or /etc/passwd.
    """
    return f"{_agent_slug(agent)}.env"


def key_path(agent: str | None = None) -> Path:
    """Where THIS AGENT's private key lives.

    PER AGENT, NOT PER MACHINE. This used to be a single `sealing.key` shared by
    every agent on the box, and both agents on one machine registered the SAME
    public key — so `agentbus-ui` could decrypt mail sealed to `agentbus-279ca7`
    with its own key material. Isolation rested entirely on the API refusing to
    hand over the ciphertext; anything that reached the bytes another way (a DB
    dump, a backup, an operator-scope fetch) read everything.

    That also made a claim we published false: "even agents joining after you
    won't see this" was untrue for any agent registered later on the same
    machine.

    Beside the per-agent API keys signin already writes, because an operator
    looking for "what secrets did agentbus put on this box" should find them in
    one place rather than two.
    """
    return config_dir() / "keys" / f"sealing-{_agent_slug(agent)}.key"


def _harden(path: Path) -> None:
    """0600 on the file, 0700 on its directory.

    Both, and the directory matters more than people expect: a 0600 file inside
    a world-readable directory is still a file whose existence, name and size
    anyone can see, and on some systems a directory they can traverse.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, stat.S_IRWXU)
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)


def create_secret_exclusive(path: Path, content: str) -> bool:
    """Create `path` holding `content`, mode 0600 FROM BIRTH, or return False if it exists.

    O_CREAT|O_EXCL is the only atomic "create if absent" POSIX offers. The old
    exists()-then-write_text()-then-chmod sequence (review #23, issuedb #30) let
    eight concurrent first users each generate a key and overwrite one another —
    seven of them then held a private key that no longer existed on disk, and
    anything sealed to their published public half was unreadable forever. It
    also left the secret world-readable between write and chmod (S2).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, stat.S_IRUSR | stat.S_IWUSR)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(content)
    return True


def _ensure_secret(path: Path, generate: Any) -> str:
    """The secret at `path`, generating it EXACTLY ONCE across concurrent first users.

    The loser of a creation race re-reads what the winner wrote. A file that
    exists but is still empty is a winner mid-write (microseconds); wait for it
    rather than treat it as absent.
    """
    for _ in range(200):
        if path.exists():
            existing = path.read_text().strip()
            if existing:
                return existing
            time.sleep(0.005)
            continue
        candidate = generate()
        if create_secret_exclusive(path, candidate + "\n"):
            _harden(path)
            return candidate
    raise OSError(f"could not create or read {path}: another writer never finished")


def ensure_keypair(agent: str | None = None) -> tuple[str, str]:
    """THIS AGENT's sealing keypair, generating it on first use.

    Returns (private, public). Generation is LOCAL and unconditional — there is
    no path where a key is fetched, uploaded, escrowed or recovered. A lost key
    means the messages sealed to it are unreadable forever, which is the
    property, not a shortcoming — and exactly why creation must be atomic.
    """
    private = _ensure_secret(key_path(agent), lambda: generate_keypair()[0])
    return private, public_from_private(private)


def load_private_key(agent: str | None = None) -> str | None:
    try:
        path = key_path(agent)
    except ValueError:
        return None
    if not path.exists():
        return None
    return path.read_text().strip() or None


def load_private_keys(agent: str | None = None) -> list[str]:
    """This machine's current key FIRST, then any superseded ones.

    ROTATION MUST NOT STRAND YESTERDAY'S MAIL. `agentbus keys rotate` keeps the
    old private key beside the new one, and telling a user "keep a copy of that
    file" is empty advice if nothing ever reads it again. A message sealed to
    the old key is still that agent's mail; only the key changed.

    Current key first because it is the one almost every message uses, so the
    common path does one attempt and stops. Order is otherwise arbitrary and
    deliberately not trusted: a wrong key raises CannotDecrypt, which is how we
    tell them apart.
    """
    keys = []
    current = load_private_key(agent)
    if current:
        keys.append(current)
    try:
        directory = key_path(agent).parent
    except ValueError:
        return keys
    if directory.exists():
        # SCOPED TO THIS AGENT'S OWN SUPERSEDED KEYS. This used to glob
        # `*.superseded` across the whole directory, which on a box with several
        # agents handed each of them every other agent's retired keys — the same
        # cross-agent read the per-agent split exists to stop, arriving by the
        # back door. A rotation must not become a disclosure.
        #
        # BOTH SHAPES for this agent. New rotations write
        # sealing-<agent>-<fingerprint>.key.superseded; the prefix is what keeps
        # one agent's history out of another's hands.
        prefix = f"sealing-{_agent_slug(agent)}"
        for old in sorted(directory.glob(f"{prefix}*.superseded")):
            try:
                value = old.read_text().strip()
            except OSError:
                continue
            if value and value not in keys:
                keys.append(value)
    return keys


class MalformedSealed(CannotDecrypt):
    """The payload is not openable because it is DAMAGED or not age at all.

    A subclass of CannotDecrypt on purpose. The facades below promise "opens it
    with whichever key fits, or tells you it cannot", so a caller writing
    `except CannotDecrypt` must not then be hit by binascii.Error or InvalidTag
    from three layers down — which is exactly what macbook-admin-bd8e86
    measured escaping 0.5.2 on a truncated attachment, a flipped base64
    character, and a file that was never age at all.

    Still a distinct TYPE, because "not for me" and "damaged" call for
    completely different actions: find the old key, versus re-fetch the file.
    Collapsing them into one message is the failure this docstring's parent
    class was written to avoid; this keeps both properties.
    """


def _open_with_each(attempt: Any, agent: str | None = None) -> Any:
    """Try every key this machine holds; normalise everything that escapes.

    ONLY CannotDecrypt continues the loop. A corrupt payload must fail at once
    rather than be retried against every key — retrying is pointless and slower,
    and it would report a damaged file as "no key fits", sending the reader to
    look for a key that would not have helped.
    """
    last: Exception | None = None
    keys = load_private_keys(agent)
    for key in keys:
        try:
            return attempt(key)
        except MalformedSealed:
            # FIRST, because MalformedSealed IS-A CannotDecrypt and the clause
            # below would CONTINUE the loop — retrying a damaged payload against
            # every key, which is exactly what this function's docstring says
            # must not happen.
            #
            # Today nothing below raises it: the primitives raise raw
            # binascii.Error / InvalidTag and normalisation happens only here.
            # So this is safe by an accident of layering, and the obvious next
            # tidy-up — normalising inside unseal_body — would silently make the
            # docstring false with no test failing. macbook-admin-bd8e86 went
            # looking for exactly this and found it does not bite YET.
            raise
        except CannotDecrypt as exc:
            last = exc
        except Exception as exc:
            raise MalformedSealed(
                f"this payload is not readable as age v1 ({type(exc).__name__}: "
                f"{exc}). It is damaged or was never sealed — a different key "
                f"would not help."
            ) from exc
    raise CannotDecrypt(
        "no key on this machine opens this" if keys else "this machine holds no sealing key"
    ) from last


def unseal_with_any(body: str, agent: str | None = None) -> str:
    """Open a sealed body with whichever of this machine's keys fits.

    Raises CannotDecrypt only if NONE of them work — which is the honest
    answer, and distinguishable from "this machine has no key at all" by
    load_private_keys() being empty.
    """
    return str(_open_with_each(lambda key: unseal_body(body, key), agent))


def unseal_bytes_with_any(raw: bytes, agent: str | None = None) -> bytes:
    return bytes(_open_with_each(lambda key: unseal_bytes(raw, key), agent))


def seal_for(plaintext: str, public_keys: list[str]) -> str:
    """Seal a body to every recipient's key, as armored text.

    ARMORED because the body travels as JSON and through an SMTP mirror, and
    binary in either is a corruption waiting to happen. age's own armor is PEM,
    which survives both.
    """
    sealed = seal(plaintext.encode(), public_keys)
    return _armor(sealed)


def seal_for_bytes(raw: bytes, public_keys: list[str]) -> bytes:
    """Seal arbitrary bytes (an attachment) rather than text.

    Returns the ARMORED form as bytes, so the caller can base64 it onto the
    wire unchanged. Armor rather than raw binary for the same reason bodies
    are armored: the payload crosses JSON and an SMTP mirror.
    """
    return _armor(seal(raw, public_keys)).encode()


def unseal_bytes(sealed_bytes: bytes, private_key: str) -> bytes:
    return unseal(_dearmor(sealed_bytes.decode()), private_key)


def unseal_body(body: str, private_key: str) -> str:
    return unseal(_dearmor(body), private_key).decode()


def is_sealed(body: str | None) -> bool:
    if not body:
        return False
    return body.lstrip().startswith(("-----BEGIN AGE ENCRYPTED FILE-----", SEALED_MARKER))


# ------------------------------------------------------------------ armor

_ARMOR_HEADER = "-----BEGIN AGE ENCRYPTED FILE-----"
_ARMOR_FOOTER = "-----END AGE ENCRYPTED FILE-----"


def _armor(raw: bytes) -> str:
    import base64
    import textwrap

    encoded = base64.b64encode(raw).decode()
    return "\n".join([_ARMOR_HEADER, *textwrap.wrap(encoded, 64), _ARMOR_FOOTER]) + "\n"


def _dearmor(text: str) -> bytes:
    import base64

    stripped = text.strip()
    if stripped.startswith(SEALED_MARKER):
        return stripped.encode()
    lines = [line for line in stripped.splitlines() if line and not line.startswith("-----")]
    return base64.b64decode("".join(lines))


# ------------------------------------------------------------- signing (#173)
#
# A SEPARATE KEYPAIR FROM SEALING, stored beside it. age X25519 does key
# agreement and cannot sign; Ed25519 signs and cannot seal. Deriving one from
# the other is possible and wrong here, because it would couple "who can read my
# mail" to "who can prove I wrote it" — rotating one would silently rotate the
# other, and those are decisions an operator makes for different reasons.


def signing_key_path(agent: str | None = None) -> Path:
    """Where THIS AGENT's signing key lives.

    PER AGENT, NOT PER MACHINE — the same correction `key_path` above already
    carries for sealing, which was made and then NOT made here, one file over.

    A single `signing.key` per box meant every agent on that box signed with the
    same Ed25519 key and published the same fingerprint. Measured on the
    operator's own machine, straight off the server:

        agentbus-8dc08d      7b310df47c7de439
        agentbus-ui-c760a1   7b310df47c7de439   <- the same key

    A signature answers exactly one question, "did THIS agent send this", and a
    key shared by two agents cannot answer it. Either could sign as the other,
    the bus could not tell them apart, and `verify-sender` returned
    `verified: true` while citing a fingerprint that belonged to both — which is
    how a peer found it: the evidence named the wrong agent.

    An operator ruling, not an inference: each agent holds its own keypair, even
    on one machine. That was stated for sealing and applies for the same reason
    here.

    NO MIGRATION FROM THE OLD SHARED FILE, deliberately. Adopting it would give
    every agent on the box the colliding key again and re-create exactly the
    state this fixes. A fresh per-agent key is generated instead, and until it
    is published `verify-sender` reports `unverifiable` - "I hold no key for
    this fingerprint" - never `invalid`. Run `agentbus keys sign` to publish.
    """
    return config_dir() / "keys" / f"signing-{_agent_slug(agent)}.key"


def ensure_signing_keypair(agent: str | None = None) -> tuple[str, str]:
    """THIS AGENT's signing keypair, generated on first use.

    Same properties as the sealing key: generated LOCALLY, written 0600 in a
    0700 directory, never transmitted. There is no path that uploads it and no
    endpoint that would accept it.
    """
    from . import _signing

    private = _ensure_secret(signing_key_path(agent), lambda: _signing.generate_keypair()[0])
    return private, _signing.public_from_private(private)


def load_signing_key(agent: str | None = None) -> str | None:
    """THIS AGENT's signing key, or None when it holds none.

    None is a normal answer: signing is opt-in and unsigned mail is
    first-class, so a caller treats None as "send unsigned", never as an error.
    """
    try:
        path = signing_key_path(agent)
    except ValueError:
        # No acting agent at all. A signing key belongs to ONE agent, so there
        # is nothing to load - and falling back to a machine-wide file here is
        # precisely the shared key this change removes.
        return None
    if not path.exists():
        return None
    return path.read_text().strip() or None
