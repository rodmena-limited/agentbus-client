from __future__ import annotations

import shutil
import subprocess

import pytest

from agentbus_client import sealing

AGE = shutil.which("age") or "/usr/bin/age"
AGE_KEYGEN = shutil.which("age-keygen") or "/usr/bin/age-keygen"
HEADER = "-----BEGIN AGE ENCRYPTED FILE-----"
FOOTER = "-----END AGE ENCRYPTED FILE-----"

needs_age = pytest.mark.skipif(shutil.which("age") is None, reason="age(1) not installed")


def _age_decrypt(armored: bytes, keyfile) -> subprocess.CompletedProcess:
    return subprocess.run(
        [AGE, "-d", "-i", str(keyfile)], input=armored, capture_output=True, timeout=30
    )


def _age_encrypt(plaintext: bytes, public: str, *, armor: bool) -> bytes:
    args = [AGE, "-r", public] + (["-a"] if armor else [])
    done = subprocess.run(args, input=plaintext, capture_output=True, timeout=30, check=True)
    return done.stdout


def test_armor_lines_are_exactly_64_columns():
    raw = bytes(range(256)) * 3 + bytes(32)
    text = sealing._armor(raw)
    lines = text.split("\n")
    assert lines[0] == HEADER
    assert lines[-2] == FOOTER
    assert lines[-1] == ""
    body = lines[1:-2]
    assert len(body) == 17
    assert all(len(line) == 64 for line in body[:-1])
    assert len(body[-1]) == 1068 - 64 * 16
    assert sealing._dearmor(text) == raw


def test_armor_of_exact_multiple_has_no_short_line():
    text = sealing._armor(bytes(96))
    assert text == HEADER + "\n" + "A" * 64 + "\n" + "A" * 64 + "\n" + FOOTER + "\n"


@needs_age
def test_real_age_decrypts_seal_for_output():
    _private, public = sealing.ensure_keypair("ops-1")
    plaintext = "the quarterly ledger balances to the penny\n" * 40
    armored = sealing.seal_for(plaintext, [public])
    assert max(len(line) for line in armored.splitlines()) == 64
    done = _age_decrypt(armored.encode(), sealing.key_path("ops-1"))
    assert done.returncode == 0, done.stderr
    assert done.stdout == plaintext.encode()


@needs_age
def test_real_age_decrypts_seal_for_bytes_output():
    _private, public = sealing.ensure_keypair("ops-1")
    payload = bytes(range(256)) * 50
    armored = sealing.seal_for_bytes(payload, [public])
    done = _age_decrypt(armored, sealing.key_path("ops-1"))
    assert done.returncode == 0, done.stderr
    assert done.stdout == payload


@needs_age
def test_real_age_decrypts_for_every_recipient():
    _a, alpha = sealing.ensure_keypair("alpha")
    _b, beta = sealing.ensure_keypair("beta")
    armored = sealing.seal_for("for both", [alpha, beta]).encode()
    for agent in ("alpha", "beta"):
        done = _age_decrypt(armored, sealing.key_path(agent))
        assert done.returncode == 0, done.stderr
        assert done.stdout == b"for both"


@needs_age
def test_real_age_keygen_derives_the_same_public_key():
    _private, public = sealing.ensure_keypair("ops-1")
    done = subprocess.run(
        [AGE_KEYGEN, "-y", str(sealing.key_path("ops-1"))],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == public


@needs_age
def test_body_sealed_by_real_age_is_detected_and_opened():
    _private, public = sealing.ensure_keypair("ops-1")
    armored = _age_encrypt(b"from real age", public, armor=True).decode()
    assert sealing.is_sealed(armored) is True
    assert sealing.is_sealed("\n\n  " + armored) is True
    assert sealing.unseal_with_any(armored, "ops-1") == "from real age"
    binary = _age_encrypt(b"binary age", public, armor=False)
    assert sealing.is_sealed(binary.decode("latin-1")) is True


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (None, False),
        ("", False),
        ("hello", False),
        ("hello\n" + HEADER, False),
        (HEADER + "\nYWJj\n" + FOOTER + "\n", True),
        ("\n \t" + HEADER + "\nYWJj\n" + FOOTER, True),
        ("age-encryption.org/v1\n-> X25519 abc\n", True),
        ("  age-encryption.org/v1\n", True),
        ("-----BEGIN PGP MESSAGE-----\n", False),
    ],
)
def test_is_sealed_known_answers(body, expected):
    assert sealing.is_sealed(body) is expected


def test_is_sealed_on_own_seal_for_output():
    _private, public = sealing.ensure_keypair("ops-1")
    assert sealing.is_sealed(sealing.seal_for("x", [public])) is True
    assert sealing.is_sealed(sealing.seal_for_bytes(b"x", [public]).decode()) is True


def test_unseal_bytes_with_any_uses_the_named_agent_not_env(monkeypatch):
    _a, alpha_public = sealing.ensure_keypair("alpha")
    sealing.ensure_keypair("beta")
    monkeypatch.setenv("AGENTBUS_AGENT", "beta")
    sealed = sealing.seal_for_bytes(b"alpha only", [alpha_public])
    assert sealing.unseal_bytes_with_any(sealed, "alpha") == b"alpha only"
    with pytest.raises(sealing.CannotDecrypt):
        sealing.unseal_bytes_with_any(sealed, "beta")
    with pytest.raises(sealing.CannotDecrypt):
        sealing.unseal_bytes_with_any(sealed)


def test_unseal_with_any_uses_the_named_agent_not_env(monkeypatch):
    _a, alpha_public = sealing.ensure_keypair("alpha")
    sealing.ensure_keypair("beta")
    monkeypatch.setenv("AGENTBUS_AGENT", "beta")
    sealed = sealing.seal_for("alpha only", [alpha_public])
    assert sealing.unseal_with_any(sealed, "alpha") == "alpha only"
    with pytest.raises(sealing.CannotDecrypt):
        sealing.unseal_with_any(sealed)


def test_unseal_with_any_opens_mail_sealed_to_a_superseded_key():
    sealing.ensure_keypair("ops-1")
    old_private, old_public = sealing.generate_keypair()
    keys_dir = sealing.key_path("ops-1").parent
    (keys_dir / "sealing-ops-1-old.key.superseded").write_text(old_private + "\n")
    sealed = sealing.seal_for("yesterday", [old_public])
    assert sealing.unseal_with_any(sealed, "ops-1") == "yesterday"


def test_no_key_at_all_says_so():
    _p, public = sealing.generate_keypair()
    sealed = sealing.seal_for("x", [public])
    with pytest.raises(sealing.CannotDecrypt) as info:
        sealing.unseal_with_any(sealed, "ops-1")
    assert str(info.value) == "this machine holds no sealing key"
    assert not isinstance(info.value, sealing.MalformedSealed)


def test_keys_exist_but_none_fit_says_so_and_chains_cause():
    sealing.ensure_keypair("ops-1")
    _p, stranger = sealing.generate_keypair()
    sealed = sealing.seal_for("x", [stranger])
    with pytest.raises(sealing.CannotDecrypt) as info:
        sealing.unseal_with_any(sealed, "ops-1")
    assert str(info.value) == "no key on this machine opens this"
    assert not isinstance(info.value, sealing.MalformedSealed)
    assert isinstance(info.value.__cause__, sealing.CannotDecrypt)


def test_damaged_payload_is_malformed_and_tried_once(monkeypatch):
    sealing.ensure_keypair("ops-1")
    old_private = sealing.generate_keypair()[0]
    keys_dir = sealing.key_path("ops-1").parent
    (keys_dir / "sealing-ops-1-old.key.superseded").write_text(old_private + "\n")
    calls: list[str] = []
    real_unseal = sealing.unseal_bytes

    def counting(raw, key):
        calls.append(key)
        return real_unseal(raw, key)

    monkeypatch.setattr(sealing, "unseal_bytes", counting)
    with pytest.raises(sealing.MalformedSealed) as info:
        sealing.unseal_bytes_with_any(b"%%% never age %%%", "ops-1")
    assert len(calls) == 1
    assert "not readable as age v1" in str(info.value)
    assert info.value.__cause__ is not None


def test_round_trip_seal_for_and_unseal_body():
    private, public = sealing.ensure_keypair("ops-1")
    text = "line one\nline two é\n" * 30
    assert sealing.unseal_body(sealing.seal_for(text, [public]), private) == text
    blob = bytes(range(256)) * 9
    assert sealing.unseal_bytes(sealing.seal_for_bytes(blob, [public]), private) == blob
