from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentbus_client.onboarding import _credentials


def _write_global_opencode(home: Path, name: str, text: str) -> None:
    target = home / ".config" / "opencode" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def _opencode_lines(monkeypatch: pytest.MonkeyPatch, home: Path) -> list[str]:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(_credentials, "_scope_of_bearer", lambda header, base_url=None: "send")
    return [
        line
        for line in _credentials.doctor_credential_scope(base_url="http://127.0.0.1:9")
        if line.startswith("opencode ")
    ]


SETUP_SHAPED = {
    "mcp": {
        "agentbus": {
            "type": "remote",
            "url": "https://agentbus.rodmena.co.uk/mcp",
            "enabled": True,
            "headers": {"Authorization": "Bearer ab_sk_fake"},
            "oauth": False,
        }
    }
}


def test_a_setup_written_config_with_an_https_url_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_global_opencode(tmp_path, "opencode.json", json.dumps(SETUP_SHAPED, indent=2))
    lines = _opencode_lines(monkeypatch, tmp_path)
    assert lines and lines[0].startswith("opencode opencode.json agentbus MCP: send"), lines


def test_jsonc_comments_and_trailing_commas_are_still_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = (
        "/* opencode */\n{\n"
        '  "mcp": { // servers\n'
        '    "agentbus": {"url": "https://x.invalid/mcp", '
        '"headers": {"Authorization": "Bearer ab_sk_fake"},},\n'
        "  },\n}\n"
    )
    _write_global_opencode(tmp_path, "opencode.jsonc", text)
    lines = _opencode_lines(monkeypatch, tmp_path)
    assert lines and lines[0].startswith("opencode opencode.jsonc agentbus MCP: send"), lines


def test_an_unparseable_config_is_reported_not_silently_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_global_opencode(tmp_path, "opencode.json", '{"mcp": {"agentbus": ')
    lines = _opencode_lines(monkeypatch, tmp_path)
    assert lines and "NOT CHECKED" in lines[0], lines


def test_comment_markers_inside_strings_survive() -> None:
    text = '{"a": "x/*not*/y", "b": "say \\"//hi\\"", "c": "a, }"}'
    assert json.loads(_credentials._strip_jsonc(text)) == {
        "a": "x/*not*/y",
        "b": 'say "//hi"',
        "c": "a, }",
    }
