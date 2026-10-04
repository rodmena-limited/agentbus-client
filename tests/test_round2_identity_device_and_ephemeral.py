from __future__ import annotations

import pathlib
import uuid
from pathlib import Path

import pytest

from agentbus_client import identity as identity_mod
from agentbus_client import sealing

MARKERS = (
    "AGENTBUS_EPHEMERAL",
    "CI",
    "GITHUB_ACTIONS",
    "GITLAB_CI",
    "BUILDKITE",
    "JENKINS_URL",
    "CIRCLECI",
    "TF_BUILD",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in (*MARKERS, "AGENTBUS_DEVICE_ID"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _dockerenv(monkeypatch: pytest.MonkeyPatch, present: bool) -> None:
    real_exists = pathlib.Path.exists

    def _exists(self: pathlib.Path, *args: object, **kwargs: object) -> bool:
        if str(self) == "/.dockerenv":
            return present
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "exists", _exists)


def _device_file() -> Path:
    return identity_mod.config_dir() / "device-id"


def test_config_dir_honours_the_override_and_defaults_under_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(tmp_path / "cfg"))
    assert identity_mod.config_dir() == tmp_path / "cfg"
    monkeypatch.delenv("AGENTBUS_CONFIG_DIR")
    assert identity_mod.config_dir() == Path.home() / ".config" / "agentbus"


def test_env_override_wins_and_is_stripped(clean_env: pytest.MonkeyPatch) -> None:
    _device_file().parent.mkdir(parents=True, exist_ok=True)
    _device_file().write_text("on-disk-id\n")
    clean_env.setenv("AGENTBUS_DEVICE_ID", "  fleet-7 \n")
    assert identity_mod.device_id() == "fleet-7"
    assert _device_file().read_text() == "on-disk-id\n"


def test_env_override_does_not_create_a_device_file(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("AGENTBUS_DEVICE_ID", "fleet-7")
    assert identity_mod.device_id() == "fleet-7"
    assert not _device_file().exists()


def test_fresh_config_dir_mints_a_uuid_persisted_with_one_newline(
    clean_env: pytest.MonkeyPatch,
) -> None:
    assert not _device_file().exists()
    first = identity_mod.device_id()
    assert str(uuid.UUID(first)) == first
    assert _device_file().read_text() == first + "\n"
    assert _device_file().stat().st_mode & 0o777 == 0o600
    assert identity_mod.device_id() == first
    assert identity_mod.device_id() == first


def test_two_fresh_machines_mint_different_ids(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    clean_env.setenv("AGENTBUS_CONFIG_DIR", str(tmp_path / "m1"))
    one = identity_mod.device_id()
    clean_env.setenv("AGENTBUS_CONFIG_DIR", str(tmp_path / "m2"))
    two = identity_mod.device_id()
    assert one != two
    assert str(uuid.UUID(two)) == two


def test_an_existing_file_is_read_back_stripped(clean_env: pytest.MonkeyPatch) -> None:
    _device_file().parent.mkdir(parents=True, exist_ok=True)
    _device_file().write_text("  persisted-id \n\n")
    assert identity_mod.device_id() == "persisted-id"


def test_losing_the_creation_race_returns_the_winners_id(
    clean_env: pytest.MonkeyPatch,
) -> None:
    offered: list[str] = []

    def _winner_got_there_first(path: Path, content: str) -> bool:
        offered.append(content)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("winner-id\n")
        return False

    clean_env.setattr(sealing, "create_secret_exclusive", _winner_got_there_first)
    assert identity_mod.device_id() == "winner-id"
    assert len(offered) == 1
    assert offered[0].endswith("\n")
    assert offered[0].count("\n") == 1
    assert str(uuid.UUID(offered[0][:-1])) == offered[0][:-1]
    assert _device_file().read_text() == "winner-id\n"


def test_creation_offers_exactly_the_returned_id_and_newline(
    clean_env: pytest.MonkeyPatch,
) -> None:
    offered: list[tuple[Path, str]] = []
    real = sealing.create_secret_exclusive

    def _spy(path: Path, content: str) -> bool:
        offered.append((path, content))
        return real(path, content)

    clean_env.setattr(sealing, "create_secret_exclusive", _spy)
    value = identity_mod.device_id()
    assert offered == [(_device_file(), value + "\n")]


def test_an_unwritable_config_dir_degrades_to_a_per_process_id(
    tmp_path: Path, clean_env: pytest.MonkeyPatch
) -> None:
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x")
    clean_env.setenv("AGENTBUS_CONFIG_DIR", str(blocker / "agentbus"))
    one = identity_mod.device_id()
    two = identity_mod.device_id()
    assert str(uuid.UUID(one)) == one
    assert one != two


@pytest.mark.parametrize("marker", MARKERS)
def test_each_marker_makes_describe_ephemeral(marker: str, clean_env: pytest.MonkeyPatch) -> None:
    _dockerenv(clean_env, False)
    clean_env.setenv("AGENTBUS_DEVICE_ID", "dev-1")
    clean_env.setenv(marker, "true")
    assert identity_mod.describe("/w/proj")["ephemeral"] is True
    assert identity_mod.is_ephemeral() is True


def test_a_clean_environment_is_not_ephemeral(clean_env: pytest.MonkeyPatch) -> None:
    _dockerenv(clean_env, False)
    assert identity_mod.describe("/w/proj")["ephemeral"] is False
    assert identity_mod.is_ephemeral() is False
    clean_env.setenv("AGENTBUS_DEVICE_ID", "dev-1")
    assert identity_mod.describe("/w/proj")["ephemeral"] is False


@pytest.mark.parametrize("name", ["AGENTBUS_EPHEMERAL", "CI"])
@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes"])
def test_truthy_flag_spellings_are_ephemeral(
    name: str, value: str, clean_env: pytest.MonkeyPatch
) -> None:
    _dockerenv(clean_env, False)
    clean_env.setenv(name, value)
    assert identity_mod.is_ephemeral() is True


@pytest.mark.parametrize("name", ["AGENTBUS_EPHEMERAL", "CI"])
@pytest.mark.parametrize("value", ["0", "false", "no", "", "y", "on"])
def test_other_flag_spellings_are_not_ephemeral(
    name: str, value: str, clean_env: pytest.MonkeyPatch
) -> None:
    _dockerenv(clean_env, False)
    clean_env.setenv(name, value)
    assert identity_mod.is_ephemeral() is False


@pytest.mark.parametrize("marker", MARKERS[2:])
@pytest.mark.parametrize("value", ["false", "0", "x"])
def test_provider_markers_count_by_presence(
    marker: str, value: str, clean_env: pytest.MonkeyPatch
) -> None:
    _dockerenv(clean_env, False)
    clean_env.setenv(marker, value)
    assert identity_mod.is_ephemeral() is True


@pytest.mark.parametrize("marker", MARKERS[2:])
def test_an_empty_provider_marker_is_absent(marker: str, clean_env: pytest.MonkeyPatch) -> None:
    _dockerenv(clean_env, False)
    clean_env.setenv(marker, "")
    assert identity_mod.is_ephemeral() is False


def test_a_container_without_a_device_file_is_ephemeral(clean_env: pytest.MonkeyPatch) -> None:
    _dockerenv(clean_env, True)
    assert not _device_file().exists()
    assert identity_mod.is_ephemeral() is True


def test_a_container_with_a_persisted_device_file_is_not(clean_env: pytest.MonkeyPatch) -> None:
    _dockerenv(clean_env, True)
    _device_file().parent.mkdir(parents=True, exist_ok=True)
    _device_file().write_text("persisted\n")
    assert identity_mod.is_ephemeral() is False


def test_describe_in_a_container_with_a_fleet_device_id_is_ephemeral(
    clean_env: pytest.MonkeyPatch,
) -> None:
    _dockerenv(clean_env, True)
    clean_env.setenv("AGENTBUS_DEVICE_ID", "fleet-7")
    assert identity_mod.describe("/w/proj")["ephemeral"] is True


def test_describe_in_a_fresh_container_is_ephemeral(clean_env: pytest.MonkeyPatch) -> None:
    _dockerenv(clean_env, True)
    assert not _device_file().exists()
    assert identity_mod.describe("/w/proj")["ephemeral"] is True
