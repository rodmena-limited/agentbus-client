"""#94 items 14 and 15: what `_signing.py` and `sealing.py` say about their copies.

`_signing.py` claimed byte identity with the server's module and a build that fails
on drift. `sealing.py` pointed at tests/test_age_format.py, which is a server test
that imports only the server's module. The tests below fail if a difference the
docstrings name stops being real, or if they name a test that is not in this repo.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

from agentbus_client import _signing, sealing

TESTS = Path(__file__).resolve().parent
NAMED_TEST = re.compile(r"tests/[\w/]+\.py")


def _flat(text: str | None) -> str:
    return " ".join((text or "").split())


def test_the_signing_docstring_drops_the_stale_claims() -> None:
    doc = _flat(_signing.__doc__).lower()
    assert "byte-identical" not in doc
    assert "fails the build" not in doc


def test_the_signing_docstring_names_differences_that_are_real() -> None:
    doc = _flat(_signing.__doc__)
    assert "raises `BadSignatureError`" in doc
    assert "refuses a signature or public key that is not text" in doc
    assert issubclass(_signing.BadSignatureError, Exception)
    assert not hasattr(_signing, "BadSignature")
    with pytest.raises(_signing.BadSignatureError, match="not text"):
        _signing.verify(None, b"payload", None)


def test_the_sealing_module_drops_the_stale_claims() -> None:
    source = inspect.getsource(sealing)
    assert "test_age_format" not in source
    assert "fails the build" not in source
    assert "committed vectors" not in source


def test_the_sealing_docstring_names_the_test_that_covers_it() -> None:
    assert "tests/test_round2_sealing_armor.py" in _flat(sealing.__doc__)
    assert "skips when age is not installed" in _flat(sealing.__doc__)


def test_that_test_really_drives_the_real_age() -> None:
    text = (TESTS / "test_round2_sealing_armor.py").read_text(encoding="utf-8")
    assert 'shutil.which("age")' in text
    assert "age(1) not installed" in text
    assert "_age_encrypt" in text


@pytest.mark.parametrize("module", [_signing, sealing], ids=["_signing", "sealing"])
def test_every_test_a_docstring_names_is_in_this_repo(module: object) -> None:
    named = NAMED_TEST.findall(inspect.getdoc(module) or "")
    assert named
    assert [path for path in named if not (TESTS.parent / path).is_file()] == []


def test_the_pattern_above_finds_a_known_positive() -> None:
    assert NAMED_TEST.findall("see tests/test_age_format.py, which") == ["tests/test_age_format.py"]
    assert not (TESTS / "test_age_format.py").exists()
