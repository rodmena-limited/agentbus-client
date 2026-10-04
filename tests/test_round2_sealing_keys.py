from __future__ import annotations

import os
import stat
import threading
import time

import pytest

from agentbus_client import _signing, sealing
from agentbus_client.identity import config_dir


@pytest.fixture
def umask_022():
    previous = os.umask(0o022)
    yield
    os.umask(previous)


@pytest.fixture
def umask_000():
    previous = os.umask(0)
    yield
    os.umask(previous)


def _mode(path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Ops", "Ops"),
        ("Xps", "Xps"),
        ("AGENTBUS-UI-C760A1", "AGENTBUS-UI-C760A1"),
        ("Ops-Team.1_x", "Ops-Team.1_x"),
        ("a/b", "a_b"),
        ("../x", "__x"),
        ("a..b", "a_b"),
        ("/", "_"),
        ("a b\\c", "a_b_c"),
    ],
)
def test_agent_slug_known_answers(name, slug):
    assert sealing._agent_slug(name) == slug
    assert sealing.agent_slug(name) == slug


def test_uppercase_agents_get_distinct_key_files():
    assert sealing.key_path("Ops") != sealing.key_path("Xps")
    assert sealing.signing_key_path("Ops") != sealing.signing_key_path("Xps")
    ops = sealing.ensure_keypair("Ops")
    xps = sealing.ensure_keypair("Xps")
    assert ops[0] != xps[0]
    ops_sig = sealing.ensure_signing_keypair("Ops")
    xps_sig = sealing.ensure_signing_keypair("Xps")
    assert ops_sig[0] != xps_sig[0]


def test_agent_slug_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("AGENTBUS_AGENT", "Env-Agent")
    assert sealing._agent_slug(None) == "Env-Agent"
    assert sealing._agent_slug("Explicit") == "Explicit"


def test_agent_slug_without_any_agent_raises():
    with pytest.raises(ValueError, match="AGENTBUS_AGENT"):
        sealing._agent_slug(None)


def test_key_paths_are_exact():
    keys = config_dir() / "keys"
    assert sealing.key_path("Ops-1") == keys / "sealing-Ops-1.key"
    assert sealing.signing_key_path("Ops-1") == keys / "signing-Ops-1.key"
    assert sealing.signing_key_path("a/b") == keys / "signing-a_b.key"
    assert sealing.bound_env_filename("a/b") == "a_b.env"


def test_signing_key_lands_in_keys_dir():
    private, _public = sealing.ensure_signing_keypair("ops-1")
    path = config_dir() / "keys" / "signing-ops-1.key"
    assert path.read_text() == private + "\n"
    assert sealing.load_signing_key("ops-1") == private


def test_create_secret_exclusive_born_0600_under_open_umask(tmp_path, umask_000):
    path = tmp_path / "d" / "secret.key"
    assert sealing.create_secret_exclusive(path, "S1\n") is True
    assert _mode(path) == 0o600
    assert path.read_text() == "S1\n"


def test_create_secret_exclusive_refuses_existing(tmp_path):
    path = tmp_path / "secret.key"
    assert sealing.create_secret_exclusive(path, "first\n") is True
    assert sealing.create_secret_exclusive(path, "second\n") is False
    assert path.read_text() == "first\n"


def test_ensure_keypair_hardens_file_and_directory(umask_022):
    private, _public = sealing.ensure_keypair("ops-1")
    path = sealing.key_path("ops-1")
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700
    assert path.read_text() == private + "\n"


def test_ensure_signing_keypair_hardens_file_and_directory(umask_022):
    private, _public = sealing.ensure_signing_keypair("ops-1")
    path = sealing.signing_key_path("ops-1")
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700
    assert path.read_text() == private + "\n"


def test_ensure_keypair_stores_an_age_secret_key():
    private, public = sealing.ensure_keypair("ops-1")
    stored = sealing.key_path("ops-1").read_text().strip()
    assert stored == private
    assert stored.startswith("AGE-SECRET-KEY-1")
    assert public.startswith("age1")
    assert sealing.public_from_private(stored) == public


def test_ensure_signing_keypair_stores_uppercase_absigsec():
    private, public = sealing.ensure_signing_keypair("ops-1")
    stored = sealing.signing_key_path("ops-1").read_text().strip()
    assert stored == private
    assert stored.startswith("ABSIGSEC1")
    assert stored == stored.upper()
    assert _signing.public_from_private(stored) == public


def test_ensure_keypair_is_stable_across_calls():
    assert sealing.ensure_keypair("ops-1") == sealing.ensure_keypair("ops-1")
    assert sealing.ensure_signing_keypair("ops-1") == sealing.ensure_signing_keypair("ops-1")


def test_race_loser_returns_the_key_on_disk(monkeypatch):
    winner_private = sealing.generate_keypair()[0]
    path = sealing.key_path("ops-1")
    real_generate = sealing.generate_keypair

    def loser_generate():
        candidate = real_generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(winner_private + "\n")
        return candidate

    monkeypatch.setattr(sealing, "generate_keypair", loser_generate)
    private, public = sealing.ensure_keypair("ops-1")
    assert private == winner_private
    assert path.read_text().strip() == winner_private
    assert public == sealing.public_from_private(winner_private)


def test_concurrent_first_users_all_hold_the_disk_key():
    barrier = threading.Barrier(8)
    results: list[str] = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        private, _public = sealing.ensure_keypair("ops-1")
        with lock:
            results.append(private)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    on_disk = sealing.key_path("ops-1").read_text().strip()
    assert results == [on_disk] * 8


def test_race_loser_waits_for_a_winner_mid_write():
    path = sealing.key_path("ops-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    winner_private = sealing.generate_keypair()[0]

    def finish_write():
        time.sleep(0.05)
        path.write_text(winner_private + "\n")

    writer = threading.Thread(target=finish_write)
    started = time.monotonic()
    writer.start()
    private, _public = sealing.ensure_keypair("ops-1")
    elapsed = time.monotonic() - started
    writer.join()
    assert private == winner_private
    assert elapsed < 0.9


def test_never_finished_writer_raises_oserror_within_bound():
    path = sealing.key_path("ops-1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    started = time.monotonic()
    with pytest.raises(OSError, match="another writer never finished"):
        sealing.ensure_keypair("ops-1")
    elapsed = time.monotonic() - started
    assert 0.9 < elapsed < 5.0
    assert path.read_text() == ""


def test_load_private_keys_explicit_agent_without_env_includes_superseded():
    current, _ = sealing.ensure_keypair("ops-1")
    old = sealing.generate_keypair()[0]
    keys_dir = sealing.key_path("ops-1").parent
    (keys_dir / "sealing-ops-1-aaaa.key.superseded").write_text(old + "\n")
    assert "AGENTBUS_AGENT" not in os.environ
    assert sealing.load_private_keys("ops-1") == [current, old]


def test_load_private_keys_skips_unreadable_and_keeps_later_ones():
    current, _ = sealing.ensure_keypair("ops-1")
    later = sealing.generate_keypair()[0]
    keys_dir = sealing.key_path("ops-1").parent
    (keys_dir / "sealing-ops-1-0000.key.superseded").mkdir()
    (keys_dir / "sealing-ops-1-zzzz.key.superseded").write_text(later + "\n")
    assert sealing.load_private_keys("ops-1") == [current, later]


def test_load_private_keys_drops_empty_and_duplicate_superseded():
    current, _ = sealing.ensure_keypair("ops-1")
    old = sealing.generate_keypair()[0]
    keys_dir = sealing.key_path("ops-1").parent
    (keys_dir / "sealing-ops-1-a.key.superseded").write_text("\n")
    (keys_dir / "sealing-ops-1-b.key.superseded").write_text(current + "\n")
    (keys_dir / "sealing-ops-1-c.key.superseded").write_text(old + "\n")
    (keys_dir / "sealing-ops-1-d.key.superseded").write_text(old + "\n")
    assert sealing.load_private_keys("ops-1") == [current, old]


def test_load_private_keys_ignores_other_agents_superseded():
    current, _ = sealing.ensure_keypair("ops-1")
    keys_dir = sealing.key_path("ops-1").parent
    (keys_dir / "sealing-intruder-x.key.superseded").write_text(
        sealing.generate_keypair()[0] + "\n"
    )
    assert sealing.load_private_keys("ops-1") == [current]


def test_load_signing_key_none_without_agent_or_file():
    assert sealing.load_signing_key(None) is None
    assert sealing.load_signing_key("nobody") is None
    assert sealing.load_private_key(None) is None
    assert sealing.load_private_keys(None) == []
