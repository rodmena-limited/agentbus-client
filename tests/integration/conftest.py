"""Integration tests: the real client against the real service.

WHY THESE EXIST SEPARATELY (#49). The unit suite is 58% covered and built on
fakes, and the least-covered modules are the onboarding ones — `_provision.py`
is 362 lines in ONE function, `_claude_setup.py` 408 in one — because they mutate
the filesystem, mint credentials and call the network together. Unit-mocking that
would manufacture exactly the vacuous tests this repo keeps finding: a fake that
returns what its author expected proves the author's convention, not the
service's behaviour.

So these run against a live workspace or they SKIP LOUDLY. They never fake the
counterparty.

Run:  AGENTBUS_INTEGRATION=1 pytest tests/integration -q
They are skipped by default so the ordinary suite stays hermetic and offline.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

OPERATOR_ENV = Path.home() / ".config" / "agentbus" / "operator.env"


def _operator_key() -> str | None:
    if not OPERATOR_ENV.is_file():
        return None
    for raw in OPERATOR_ENV.read_text().splitlines():
        entry = raw.strip().removeprefix("export ")
        key, _, value = entry.partition("=")
        if key.strip() == "AGENTBUS_API_KEY":
            return value.strip().strip("'\"") or None
    return None


_HERE = Path(__file__).parent.resolve()


def _is_integration(item) -> bool:
    """Only OUR items.

    `pytest_collection_modifyitems` is a GLOBAL hook: a conftest in a
    subdirectory is handed every item pytest collected, not just the ones
    beneath it. The first version of this file skipped indiscriminately and
    turned the whole suite into `792 skipped` — a green run that tested nothing,
    which is precisely the failure this directory exists to guard against, and
    it would have disabled CI silently.
    """
    try:
        return _HERE in Path(str(item.fspath)).resolve().parents
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    """Skip with a REASON, never silently.

    A suite that quietly collects nothing looks identical to one that passed,
    which is how an integration gate becomes decorative.
    """
    mine = [i for i in items if _is_integration(i)]
    if not mine:
        return
    if os.environ.get("AGENTBUS_INTEGRATION") != "1":
        skip = pytest.mark.skip(reason="set AGENTBUS_INTEGRATION=1 to run integration tests")
    elif _operator_key() is None:
        skip = pytest.mark.skip(reason=f"no operator credential at {OPERATOR_ENV}")
    else:
        return
    for item in mine:
        item.add_marker(skip)


@pytest.fixture(scope="session")
def operator_key() -> str:
    key = _operator_key()
    if key is None:
        pytest.skip("no operator credential")
    return key


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """A throwaway project directory with its own config home.

    Points AGENTBUS config at tmp_path so a test can never write to, or read
    from, the developer's real keys directory.
    """
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)
    monkeypatch.delenv("AGENTBUS_API_KEY", raising=False)
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.chdir(project)
    return project
