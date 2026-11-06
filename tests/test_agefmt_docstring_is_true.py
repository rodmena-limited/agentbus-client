"""#93 item 19: `_agefmt.py` describes the file it is.

Its docstring claimed byte identity with the server's module and a build that
fails on drift. Neither was true: the files differ and no test reads both. The
second test fails if the difference the docstring now names stops being real.
"""

from __future__ import annotations

from agentbus_client import _agefmt, sealing


def _doc() -> str:
    return " ".join((_agefmt.__doc__ or "").split())


def test_the_stale_claims_are_gone() -> None:
    doc = _doc()
    assert "byte-identical" not in doc
    assert "fails the build" not in doc
    assert "tests/test_age_format.py, which refuses" not in doc


def test_the_named_difference_from_the_server_copy_is_real() -> None:
    assert "has no `armor()` or `repad_armor()`" in _doc()
    assert not hasattr(_agefmt, "armor")
    assert not hasattr(_agefmt, "repad_armor")
    assert callable(sealing._armor)
