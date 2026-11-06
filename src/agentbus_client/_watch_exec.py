from __future__ import annotations

import re
import string
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

_PLAIN = re.compile(r"[\w@%+=:,./-]+", re.ASCII)
_UNQUOTED = re.compile(r"""\\.|\$\(|[`'")]""", re.DOTALL)
_SCANNERS = {
    "'": re.compile("'"),
    '"': re.compile(r"""\\.|\$\(|[`"]""", re.DOTALL),
}
_CLOSERS = {"'": "'", '"': '"', "`": "`", "$(": ")"}


def _track_quotes(text: str, contexts: list[str]) -> None:
    position = 0
    while True:
        token = _SCANNERS.get(contexts[-1], _UNQUOTED).search(text, position)
        if token is None:
            return
        position = token.end()
        mark = token.group()
        if mark.startswith("\\"):
            continue
        if _CLOSERS.get(contexts[-1]) == mark:
            contexts.pop()
        elif mark != ")":
            contexts.append(mark)


def _reference(name: str, context: str) -> str:
    if context == "'":
        return f"""'"${{{name}}}"'"""
    if context == '"':
        return f"${{{name}}}"
    return f'"${{{name}}}"'


def expand(template: str, fields: Mapping[str, str]) -> tuple[str, dict[str, str]]:
    formatter = string.Formatter()
    command: list[str] = []
    passed: dict[str, str] = {}
    contexts = [""]
    for literal, field, spec, conversion in formatter.parse(template):
        command.append(literal)
        _track_quotes(literal, contexts)
        if field is None:
            continue
        value, _ = formatter.get_field(field, (), fields)
        text: str = formatter.format_field(
            formatter.convert_field(value, conversion), formatter.vformat(spec or "", (), fields)
        )
        if _PLAIN.fullmatch(text):
            command.append(text)
            continue
        name = f"AGENTBUS_FIELD_{len(passed)}"
        passed[name] = text
        command.append(_reference(name, contexts[-1]))
    return "".join(command), passed
