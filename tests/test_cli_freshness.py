"""`doctor` must be able to say the CLI itself is stale (agentbus #342).

THE FAILURE THIS ANSWERS SUCCEEDS. `uv tool upgrade rodmena-agentbus` printed
"Nothing to upgrade" and exited 0 while leaving the binary a release behind,
because the tool had been installed with an exact version pin. Nothing was
wrong, nothing was reported, and the new verb simply was not there. The same
shape as `uv pip install -U` upgrading a venv that PATH does not resolve to.

A command that achieves nothing and exits 0 can only be caught by comparing
versions, so `doctor` now does — and the interesting assertions here are not
"it spots a stale build" but the two ways a freshness check goes quietly wrong:

  * IT MUST NOT SAY "CURRENT" WHEN IT COULD NOT CHECK. An unreachable PyPI is
    `unknown`, not `current`. Collapsing those is the unearned negative this
    project has a standing rule about — and it is the more likely bug, because
    the offline path is the one nobody exercises.
  * IT MUST NOT NAG A DEVELOPER. A source checkout reports 0.0.0+source and has
    no meaningful comparison; a false "you are out of date" on every developer
    machine is how a real warning stops being read.
"""

from __future__ import annotations

from agentbus_client.onboarding import _doctor_version as dv


def _fake_pypi(monkeypatch, version, detail="ok"):
    monkeypatch.setattr(dv, "latest_on_pypi", lambda timeout=4.0: (version, detail))


# ------------------------------------------------------------------- stale


def test_a_behind_build_is_stale(monkeypatch):
    _fake_pypi(monkeypatch, "0.9.68")
    state, detail = dv.cli_freshness("0.9.67")
    assert state == "stale"
    assert "0.9.67" in detail and "0.9.68" in detail


def test_the_remedy_names_the_command_that_actually_works(monkeypatch):
    """`uv tool upgrade` is the command that DID NOT WORK. If the advice says
    only that, this check has reproduced the bug it exists to prevent."""
    _fake_pypi(monkeypatch, "0.9.68")
    _state, detail = dv.cli_freshness("0.9.60")
    assert "uv tool install rodmena-agentbus@latest" in detail
    assert "Nothing to upgrade" in detail, "the advice must warn that upgrade exits 0"
    assert "uv pip install -U" in detail, "and that pip -U hits the wrong environment"


# ----------------------------------------------------------------- current


def test_the_latest_build_is_current(monkeypatch):
    """KNOWN-POSITIVE. Without this, a function that answered "stale" for every
    input would satisfy every staleness assertion above."""
    _fake_pypi(monkeypatch, "0.9.68")
    assert dv.cli_freshness("0.9.68")[0] == "current"


def test_a_build_ahead_of_pypi_is_not_reported_stale(monkeypatch):
    """The release machine runs the new version before PyPI has it."""
    _fake_pypi(monkeypatch, "0.9.68")
    assert dv.cli_freshness("0.9.69")[0] == "current"


def test_component_order_not_string_order(monkeypatch):
    """`"0.9.9" > "0.9.68"` is TRUE as strings and false as versions.

    A string comparison would have called 0.9.68 stale against 0.9.9, told
    everyone to downgrade, and looked right in every test written with
    single-digit versions.
    """
    _fake_pypi(monkeypatch, "0.9.68")
    assert dv.cli_freshness("0.9.9")[0] == "stale"
    _fake_pypi(monkeypatch, "0.10.0")
    assert dv.cli_freshness("0.9.68")[0] == "stale"


# ---------------------------------------------------- cannot check != current


def test_an_unreachable_pypi_is_unknown_never_current(monkeypatch):
    """THE ONE THAT MATTERS. Offline is the path nobody runs, and reporting
    "current" from it is a false all-clear."""
    _fake_pypi(monkeypatch, None, "could not reach PyPI (timed out)")
    state, detail = dv.cli_freshness("0.9.60")
    assert state == "unknown"
    assert "NOT the same as being up to date" in detail


def test_an_unparseable_version_is_unknown_not_stale(monkeypatch):
    _fake_pypi(monkeypatch, "not-a-version")
    assert dv.cli_freshness("0.9.68")[0] == "unknown"
    _fake_pypi(monkeypatch, "0.9.68")
    assert dv.cli_freshness("weird-build")[0] == "unknown"


def test_latest_on_pypi_never_raises(monkeypatch):
    """doctor is a diagnostic; a network error must not make it explode."""

    def _boom(*_a, **_k):
        raise OSError("network is unreachable")

    monkeypatch.setattr(dv.urllib.request, "urlopen", _boom)
    version, detail = dv.latest_on_pypi(timeout=0.1)
    assert version is None
    assert "could not" in detail


# ------------------------------------------------------------------ source


def test_a_source_checkout_is_never_nagged(monkeypatch):
    def _fail(*_a, **_k):
        raise AssertionError("a source build must not even ask PyPI")

    monkeypatch.setattr(dv, "latest_on_pypi", _fail)
    assert dv.cli_freshness("0.0.0+source")[0] == "source"


def test_a_build_that_reports_nothing_is_unknown():
    assert dv.cli_freshness(None)[0] == "unknown"
    assert dv.cli_freshness("unknown")[0] == "unknown"


# ------------------------------------------------------- doctor uses it


def test_doctor_actually_calls_the_check():
    """Otherwise every assertion here tests a function nobody runs — the exact
    way an extraction-for-testability rots."""
    import ast
    import inspect

    from agentbus_client.onboarding import _doctor

    tree = ast.parse(inspect.getsource(_doctor))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "cli_freshness" in called, "_doctor no longer calls cli_freshness"
