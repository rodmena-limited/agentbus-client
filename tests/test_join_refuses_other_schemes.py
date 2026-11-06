"""#93 item 24: `join` talks to a bus over http or https, and says so.

`cmd_join` is the one command that uses urllib, which opens file: and ftp: URLs,
and it carries the one-time token. A URL with no scheme raised ValueError and a
malformed http one raised InvalidURL, both as tracebacks.
"""

from __future__ import annotations

import io
import json
import urllib.request
from typing import Any

import pytest

from agentbus_client import cli

ADVICE = (
    "join accepts only http:// and https:// URLs with a host. Fix --base-url or $AGENTBUS_BASE_URL."
)


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> list[str]:
    urls: list[str] = []

    def _urlopen(request: urllib.request.Request, timeout: float = 0) -> Any:
        urls.append(request.full_url)
        return io.BytesIO(json.dumps({"api_key": "ab_sk_new", "agent": {"name": "n"}}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen)
    monkeypatch.setenv("AGENTBUS_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("AGENTBUS_BASE_URL", raising=False)
    return urls


REFUSED = [
    "file:///etc/passwd",
    "file:/etc/passwd",
    "ftp://host/bus",
    "gopher://host",
    "data:text/plain,hi",
    "bus.example.com",
    "//bus.example.com",
    "http://",
    "https:bus.example.com",
    "http://[bad",
]


@pytest.mark.parametrize("url", REFUSED)
def test_anything_else_is_refused_before_a_request_is_made(
    url: str, opened: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--base-url", url, "join", "TOKEN", "newname"]) == 2
    assert opened == []
    assert capsys.readouterr().err.splitlines() == [
        f"agentbus join: error: base URL {url.rstrip('/')!r}: {ADVICE}",
        "  help:  agentbus join --help",
    ]


def test_the_environment_variable_is_held_to_the_same_rule(
    monkeypatch: pytest.MonkeyPatch, opened: list[str]
) -> None:
    monkeypatch.setenv("AGENTBUS_BASE_URL", "file:///etc/passwd")
    assert cli.main(["join", "TOKEN", "newname"]) == 2
    assert opened == []


def test_a_json_caller_gets_the_refusal_as_json(
    opened: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert cli.main(["--json", "--base-url", "ftp://x", "join", "TOKEN", "newname"]) == 2
    error = json.loads(capsys.readouterr().err)["error"]
    assert error == {
        "code": "invalid_input",
        "detail": f"base URL 'ftp://x': {ADVICE}",
        "exit_code": 2,
    }


@pytest.mark.parametrize(
    "url",
    ["http://x:abc", "http://a b", "http://exa\nmple.com"],
    ids=["port", "space", "newline"],
)
def test_a_url_urllib_rejects_late_gets_the_same_plain_refusal(
    url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Real urlopen: each of these raises InvalidURL before any socket opens."""
    assert cli.main(["--base-url", url, "join", "TOKEN", "newname"]) == 2
    assert capsys.readouterr().err.splitlines()[0] == (
        f"agentbus join: error: base URL {url!r}: {ADVICE}"
    )


@pytest.mark.parametrize(
    ("url", "reached"),
    [
        ("https://bus.example.com", "https://bus.example.com/v1/agents/join"),
        ("http://127.0.0.1:8080/", "http://127.0.0.1:8080/v1/agents/join"),
        ("HTTPS://BUS.example.com", "HTTPS://BUS.example.com/v1/agents/join"),
        ("https://bus.example.com/prefix", "https://bus.example.com/prefix/v1/agents/join"),
    ],
)
def test_http_and_https_still_join(url: str, reached: str, opened: list[str]) -> None:
    assert cli.main(["--base-url", url, "join", "TOKEN", "newname"]) == 0
    assert opened == [reached]


def test_the_default_url_still_joins(opened: list[str]) -> None:
    assert cli.main(["join", "TOKEN", "newname"]) == 0
    assert opened == ["https://agentbus.rodmena.co.uk/v1/agents/join"]
