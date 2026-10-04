from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from agentbus_client import identity as identity_mod

DEMO_FP = "476bab3fd782"
PROJ_HASH = "783cc1983e91460e"
ROOT_HASH = "8a5edab282632443"


def _sha(text: str, n: int) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:n]


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(path: Path, origin: str | None) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", ".", cwd=path)
    if origin is not None:
        _git("remote", "add", "origin", origin, cwd=path)
    return path


def test_known_answer_constants_are_sha256_of_the_normalized_forms() -> None:
    assert _sha("github.com/ex/demo", 12) == DEMO_FP
    assert _sha("/w/proj", 16) == PROJ_HASH
    assert _sha("/", 16) == ROOT_HASH


@pytest.mark.parametrize(
    "remote",
    [
        "git@github.com:Ex/Demo.git",
        "https://github.com/ex/demo/",
        "ssh://git@github.com/ex/demo.git",
        "  HTTPS://GitHub.com/Ex/Demo.git \n",
        "git+ssh://git@github.com/ex/demo",
        "http://github.com/ex/demo",
        "github.com/ex/demo",
    ],
)
def test_every_spelling_of_one_remote_has_the_same_fingerprint(remote: str) -> None:
    assert identity_mod.repo_fingerprint(remote) == DEMO_FP


@pytest.mark.parametrize(
    ("remote", "expected"),
    [
        ("https://github.com/ex/other", "31b8fcad82e7"),
        ("ssh://git@host:2222/ex/demo.git", "558d3007eee9"),
        ("a@host:x/y", "e4f719a0f97a"),
        ("@host:x/y", "df6e2ebb4c04"),
        ("git@gitlab.com:grp/sub:odd.git", "ea0c943cb4a9"),
        ("http://github.com/ex/demo.git/", "29fec6ef7885"),
    ],
)
def test_fingerprint_known_answers_for_edge_shapes(remote: str, expected: str) -> None:
    assert identity_mod.repo_fingerprint(remote) == expected


@pytest.mark.parametrize(
    ("remote", "normalized"),
    [
        ("https://github.com/ex/other", "github.com/ex/other"),
        ("ssh://git@host:2222/ex/demo.git", "host/2222/ex/demo"),
        ("a@host:x/y", "a@host/x/y"),
        ("@host:x/y", "@host:x/y"),
        ("git@gitlab.com:grp/sub:odd.git", "gitlab.com/grp/sub:odd"),
        ("http://github.com/ex/demo.git/", "github.com/ex/demo.git"),
    ],
)
def test_edge_shape_answers_are_the_hash_of_the_server_normalization(
    remote: str, normalized: str
) -> None:
    assert identity_mod.repo_fingerprint(remote) == _sha(normalized, 12)


def test_a_different_repo_has_a_different_fingerprint() -> None:
    assert identity_mod.repo_fingerprint("git@github.com:ex/other.git") != DEMO_FP
    assert identity_mod.repo_fingerprint("git@github.com:ex/demo2.git") != DEMO_FP


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/w/proj", PROJ_HASH),
        ("/w/proj/", PROJ_HASH),
        ("/w/proj\\", PROJ_HASH),
        ("/w/proj//", PROJ_HASH),
        ("/w/projX", "32a62f32d65df6f7"),
        ("/w/proj ", "2d06e04c133507ca"),
        ("", ROOT_HASH),
        ("/", ROOT_HASH),
        ("///", ROOT_HASH),
    ],
)
def test_path_hash_known_answers(path: str, expected: str) -> None:
    assert identity_mod.path_hash(path) == expected


def test_trailing_letters_are_not_stripped_so_sibling_checkouts_differ() -> None:
    assert identity_mod.path_hash("/w/projX") != identity_mod.path_hash("/w/proj")
    assert identity_mod.path_hash("/w/projX") == _sha("/w/projX", 16)


def test_path_hash_is_sixteen_lowercase_hex() -> None:
    value = identity_mod.path_hash("/w/proj")
    assert len(value) == 16
    assert value == value.lower()
    int(value, 16)


@pytest.mark.parametrize(
    ("device", "repo", "path", "expected"),
    [
        ("dev-1", "5f1c", "/w/proj", "758208b0e6cfa9a1"),
        ("dev-1", DEMO_FP, "/w/proj", "310b0b92429ff65e"),
        ("dev-1", None, "/w/proj", "5bbccd9a745adfe9"),
        ("dev-1", None, None, "0388fb626ca89a12"),
        (None, None, "/w/proj", "a5e858dc4570f06e"),
    ],
)
def test_session_key_known_answers(
    device: str | None, repo: str | None, path: str | None, expected: str
) -> None:
    assert identity_mod.session_key(device, repo, path) == expected


def test_session_key_is_sha256_of_colon_joined_present_parts() -> None:
    assert identity_mod.session_key("dev-1", "5f1c", "/w/proj") == _sha(
        f"dev-1:5f1c:{PROJ_HASH}", 16
    )
    assert identity_mod.session_key("dev-1", None, "/w/proj") == _sha(f"dev-1:{PROJ_HASH}", 16)
    assert identity_mod.session_key("dev-1") == _sha("dev-1", 16)


def test_session_key_of_nothing_is_none() -> None:
    assert identity_mod.session_key() is None
    assert identity_mod.session_key(None, None, None) is None
    assert identity_mod.session_key("", "", "") is None


def test_session_key_never_hashes_a_missing_path_as_root() -> None:
    assert identity_mod.session_key("dev-1", None, None) != _sha(f"dev-1:{ROOT_HASH}", 16)


def test_git_remote_reads_the_origin_of_the_given_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _repo(tmp_path / "a", "git@github.com:Ex/Demo.git")
    b = _repo(tmp_path / "b", "https://example.invalid/other.git")
    monkeypatch.chdir(b)
    assert identity_mod.git_remote(str(a)) == "git@github.com:Ex/Demo.git"
    assert identity_mod.git_remote(str(b)) == "https://example.invalid/other.git"


def test_git_remote_without_a_path_reads_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    b = _repo(tmp_path / "b", "https://example.invalid/other.git")
    monkeypatch.chdir(b)
    assert identity_mod.git_remote() == "https://example.invalid/other.git"
    assert identity_mod.git_remote(None) == "https://example.invalid/other.git"


def test_git_remote_is_none_without_origin_or_outside_a_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare = _repo(tmp_path / "noorigin", None)
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    assert identity_mod.git_remote(str(bare)) is None
    assert identity_mod.git_remote(str(plain)) is None
    assert identity_mod.git_remote(str(tmp_path / "missing")) is None


def test_git_remote_is_none_when_git_cannot_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(identity_mod.subprocess, "run", _boom)
    assert identity_mod.git_remote("/w/proj") is None


def test_git_remote_is_bounded_by_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def _record(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        seen["argv"] = argv
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="  u://x  \n", stderr="")

    monkeypatch.setattr(identity_mod.subprocess, "run", _record)
    assert identity_mod.git_remote("/w/proj") == "u://x"
    assert seen["argv"] == ["git", "-C", "/w/proj", "remote", "get-url", "origin"]
    assert seen["timeout"] == 5
    assert seen["capture_output"] is True
    assert seen["text"] is True


def test_describe_reports_the_workdir_repo_not_the_cwd_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTBUS_DEVICE_ID", "dev-1")
    a = _repo(tmp_path / "a", "git@github.com:Ex/Demo.git")
    b = _repo(tmp_path / "b", "https://example.invalid/other.git")
    monkeypatch.chdir(b)
    env = identity_mod.describe(str(a))
    workdir = str(a.resolve())
    assert env["device_id"] == "dev-1"
    assert env["workdir"] == workdir
    assert env["repo_remote"] == "git@github.com:Ex/Demo.git"
    assert env["repo_fingerprint"] == DEMO_FP
    assert env["session_key"] == _sha(f"dev-1:{DEMO_FP}:{_sha(workdir, 16)}", 16)
    assert set(env) == {
        "device_id",
        "workdir",
        "repo_remote",
        "repo_fingerprint",
        "session_key",
        "ephemeral",
    }


def test_describe_without_workdir_uses_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTBUS_DEVICE_ID", "dev-1")
    b = _repo(tmp_path / "b", "https://example.invalid/other.git")
    monkeypatch.chdir(b)
    env = identity_mod.describe()
    workdir = str(Path.cwd())
    fp = _sha("example.invalid/other", 12)
    assert env["workdir"] == workdir
    assert env["repo_remote"] == "https://example.invalid/other.git"
    assert env["repo_fingerprint"] == fp
    assert env["session_key"] == _sha(f"dev-1:{fp}:{_sha(workdir, 16)}", 16)


def test_describe_of_a_fixed_path_outside_any_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTBUS_DEVICE_ID", "dev-1")
    monkeypatch.chdir(_repo(tmp_path / "b", "https://example.invalid/other.git"))
    env = identity_mod.describe("/w/proj")
    assert env["workdir"] == "/w/proj"
    assert env["repo_remote"] is None
    assert env["repo_fingerprint"] is None
    assert env["session_key"] == "5bbccd9a745adfe9"


def test_describe_of_a_fixed_path_with_a_known_remote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTBUS_DEVICE_ID", "dev-1")
    calls: list[str | None] = []

    def _remote(path: str | None = None) -> str:
        calls.append(path)
        return "ssh://git@github.com/ex/demo.git"

    monkeypatch.setattr(identity_mod, "git_remote", _remote)
    env = identity_mod.describe("/w/proj")
    assert calls == ["/w/proj"]
    assert env["repo_remote"] == "ssh://git@github.com/ex/demo.git"
    assert env["repo_fingerprint"] == DEMO_FP
    assert env["session_key"] == "310b0b92429ff65e"


def test_two_checkouts_of_one_remote_share_a_fingerprint_but_not_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTBUS_DEVICE_ID", "dev-1")
    one = _repo(tmp_path / "proj", "git@github.com:Ex/Demo.git")
    two = _repo(tmp_path / "projX", "https://github.com/ex/demo/")
    first = identity_mod.describe(str(one))
    second = identity_mod.describe(str(two))
    assert first["repo_fingerprint"] == DEMO_FP
    assert second["repo_fingerprint"] == DEMO_FP
    assert first["session_key"] != second["session_key"]
