from __future__ import annotations

import pytest

from agentbus_client import _env_numbers
from agentbus_client.client import models

SETTINGS = [
    ("AGENTBUS_MAX_ATTACHMENT_BYTES", models._max_attachment_bytes, 52_428_800),
    ("AGENTBUS_SERVER_MAX_ATTACHMENT_BYTES", models._server_max_attachment_bytes, 10_485_760),
]


@pytest.fixture(autouse=True)
def _fresh_reports(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_env_numbers, "_REPORTED", set())


@pytest.mark.parametrize("setting", SETTINGS)
@pytest.mark.parametrize("raw", ["lots", "-3", "0", "1.5"])
def test_an_unusable_value_falls_back_with_one_plain_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    setting: tuple[str, object, int],
    raw: str,
) -> None:
    name, read, default = setting
    monkeypatch.setenv(name, raw)
    assert callable(read)
    assert read() == default
    assert read() == default
    err = capsys.readouterr().err
    assert err == (
        f"agentbus: {name}={raw!r} is not a whole number of 1 or more. The client is using "
        f"{default}. Set it to 1 or higher, or unset it for the default of {default}.\n"
    )


@pytest.mark.parametrize(("name", "read", "default"), SETTINGS)
def test_a_usable_value_is_used_silently(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
    read: object,
    default: int,
) -> None:
    assert callable(read)
    monkeypatch.setenv(name, "2048")
    assert read() == 2048
    monkeypatch.setenv(name, "")
    assert read() == default
    monkeypatch.delenv(name)
    assert read() == default
    assert capsys.readouterr().err == ""
