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


def test_the_remedy_does_not_stake_everything_on_one_command(monkeypatch):
    """NAMING A COMMAND WAS THE WRONG SHAPE, and a peer proved it.

    The first version of this advice said "run `uv tool install ...@latest`".
    financial-freedom-projec-195737 ran exactly that: it printed nothing, exited
    0, and left them on 0.9.61 — the client was never a uv tool on their host at
    all. It was pip-installed in two places, and the one that mattered was a
    project venv their code invokes by ABSOLUTE PATH.

    Their generalisation is the right one: EVERY upgrade command no-ops when the
    package is not installed the way that command assumes, and all of them exit
    0. So the advice must cover the install shapes AND tell the reader the only
    thing that actually proves anything — that the version moved.
    """
    _fake_pypi(monkeypatch, "0.9.68")
    _state, detail = dv.cli_freshness("0.9.60")
    assert "agentbus --version" in detail, (
        "the advice must tell the reader to CHECK THE VERSION MOVED; every "
        "upgrade command exits 0 without doing anything on the wrong install"
    )
    for shape in ("uv tool install", "pip install -U"):
        assert shape in detail, f"the advice does not cover a {shape!r} install"
    assert "exits 0" in detail or "no-ops" in detail, (
        "the advice must say WHY a successful-looking upgrade proves nothing"
    )


def test_a_stale_build_says_WHICH_copy_is_stale(monkeypatch):
    """A peer had two installs and the stale one was not the binary on PATH.
    'You are out of date' without naming the copy sends people to upgrade the
    wrong one."""
    import sys as _sys

    _fake_pypi(monkeypatch, "0.9.68")
    _state, detail = dv.cli_freshness("0.9.60")
    assert _sys.executable in detail, "the report does not say which install is stale"


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


def test_the_doctor_command_actually_calls_the_check():
    """Otherwise every assertion here tests a function nobody runs.

    THIS ALREADY CAUGHT ONE REAL MISTAKE. The check was first wired into
    `onboarding/_doctor.py` inside the wake-chain block — which only runs once a
    monitor is PROVEN, so on an ordinary host it never printed at all. The
    function was correct, its tests passed, and `agentbus doctor` said nothing.
    It now lives beside the `skill:` line in the CLI command, which prints on
    every run, and this test points at THAT module for the same reason.
    """
    import ast
    import inspect

    from agentbus_client.cli import _diag

    source = inspect.getsource(_diag)
    tree = ast.parse(source)
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "cli_freshness" in called, (
        "the doctor command no longer calls cli_freshness — if it moved, move "
        "this assertion to the module that RUNS on every doctor invocation, "
        "not to whichever module happens to import it"
    )
    # AND THAT THE VERDICT IS ACTUALLY PRINTED — asserted on the AST, not on the
    # source text. The first version of this line was `'f"cli:' in source`, and
    # commenting the print out left that string sitting in the comment, so the
    # mutation passed. A text search cannot tell live code from a comment about
    # live code, which is the loose-grep failure this project has rejected twice
    # elsewhere. So: find a real `print(...)` whose f-string starts with "cli:".
    printed = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
        and node.args
        and any(
            isinstance(part, ast.Constant)
            and isinstance(part.value, str)
            and part.value.lstrip().startswith("cli:")
            for arg in node.args
            for part in (arg.values if isinstance(arg, ast.JoinedStr) else [arg])
        )
    ]
    assert printed, "cli_freshness is called but its verdict is never printed"


# ------------------------------------------------- ahead of a lagging index


def test_being_ahead_of_pypis_index_is_not_reported_as_being_the_latest(monkeypatch):
    """PyPI's JSON API lags its own simple index by minutes.

    A peer installed 0.9.73 with pip while /pypi/.../json still reported 0.9.72,
    and `doctor` told them "0.9.73 is the latest on PyPI" — a confident statement
    about a third party that we had not checked and that was not true. Say what
    was actually observed.
    """
    _fake_pypi(monkeypatch, "0.9.72")
    state, detail = dv.cli_freshness("0.9.73")
    assert state == "current"
    assert "AHEAD of the index" in detail
    assert "0.9.72" in detail, "the report must name what PyPI actually said"


def test_an_exact_match_still_reads_as_the_latest(monkeypatch):
    """KNOWN-POSITIVE for the branch above: the ordinary case must not acquire
    the ahead-of-index wording."""
    _fake_pypi(monkeypatch, "0.9.73")
    state, detail = dv.cli_freshness("0.9.73")
    assert state == "current"
    assert "AHEAD" not in detail
    assert "is the latest on PyPI" in detail


def test_the_remedy_names_the_pip_cache(monkeypatch):
    """`pip install -U` no-ops silently from a cached index that predates the
    release — exit 0, nothing printed. Reported by a peer who hit exactly that
    and needed --no-cache-dir. "Run it again" is not a remedy when the index is
    the stale thing."""
    _fake_pypi(monkeypatch, "0.9.99")
    _state, detail = dv.cli_freshness("0.9.60")
    assert "--no-cache-dir" in detail
    assert "CACHED INDEX" in detail or "cached index" in detail.lower()
