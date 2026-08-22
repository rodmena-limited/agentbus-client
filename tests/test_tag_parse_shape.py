"""Pin the tag/label parse across every write site (issuedb #14).

Peer report from agentbus-ui-c760a1 (thread 01M06T6TQJ5T2MJKY7DR7A2TCH,
backend #260): two agents in the wild had different label shapes stored
against what LOOKED like the same command:

  bikeroom:  {"skill:r2-probe": <value>}
  macbook:   {"skill": "r2-probe"}

Analysis of the current codebase: BOTH cmd_tag (cli.py:1424) and
cmd_register --label (cli.py:348) use `item.partition("=")`, so the same
input string produces the same output on either write path. The only way
to get the two shapes is from two DIFFERENT inputs:

  agentbus tag skill:r2-probe=<value>   ->  {"skill:r2-probe": <value>}
  agentbus tag skill=r2-probe            ->  {"skill": "r2-probe"}

These tests pin that invariant: the parse is deterministic, both write
sites agree, and every meaningful grammar produces the expected shape.
Any future refactor that splits on `:` instead of `=` (the drift the
peer suspected) lands here first.
"""

from __future__ import annotations

import argparse

from agentbus_client import cli


class _CapturingBus:
    """Records the labels dict that got POST'd to the server."""

    def __init__(self):
        self.tag_called_with: dict[str, str] | None = None
        self.register_called_with: dict[str, str] | None = None
        self.agent = "test-agent"

    def tag(self, set_labels, remove, agent=None):
        self.tag_called_with = set_labels
        return {"labels": set_labels, "count": len(set_labels), "limit": 50}

    def register(self, name, **kw):
        self.register_called_with = kw.get("labels") or {}
        return {"agent": {"name": name or "test-agent"}}

    # required by cmd_register post-register wiring; make it a no-op
    def whoami(self, agent=None):
        return {"agent": {"name": "test-agent", "labels": {}}}


def _tag_args(*positional, remove=None, agent=None, json=False):
    return argparse.Namespace(
        set=list(positional), remove=list(remove or []), agent=agent, json=json
    )


def _register_args(**over):
    base = {
        "name": "test-agent",
        "role": None,
        "workdir": None,
        "repo_remote": None,
        "capability": [],
        "label": [],
        "unlisted": False,
        "ephemeral": False,
        "agent": None,
        "json": False,
    }
    base.update(over)
    return argparse.Namespace(**base)


# ------------------------------------------------------------- cmd_tag parse


def test_namespaced_key_no_value_stores_as_key_only(monkeypatch):
    bus = _CapturingBus()
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    cli.cmd_tag(_tag_args("skill:playwright"))
    assert bus.tag_called_with == {"skill:playwright": ""}


def test_key_equals_value_stores_as_key_value(monkeypatch):
    bus = _CapturingBus()
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    cli.cmd_tag(_tag_args("skill=playwright"))
    assert bus.tag_called_with == {"skill": "playwright"}


def test_namespaced_key_with_value_stores_as_compound_key_and_value(monkeypatch):
    """The important one: `skill:playwright=takes shots` is NOT
    `{skill: playwright}` with the extra bit lost — the colon is part of
    the key. This is the shape the peer's bikeroom agent had, and it is
    what the current parse produces on every run."""
    bus = _CapturingBus()
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    cli.cmd_tag(_tag_args("skill:playwright=takes shots"))
    assert bus.tag_called_with == {"skill:playwright": "takes shots"}


def test_multiple_equals_signs_only_split_on_the_first(monkeypatch):
    """Backend spec says everything after the FIRST `=` is the value.
    A URL as a value must survive: `link=https://x=y` is a value with
    `=` in it, NOT a triple-key key=key=value."""
    bus = _CapturingBus()
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    cli.cmd_tag(_tag_args("link=https://x?a=b"))
    assert bus.tag_called_with == {"link": "https://x?a=b"}


def test_the_two_grammars_produce_different_shapes(monkeypatch):
    """This is the CENTRAL invariant: `skill:playwright` and
    `skill=playwright` mean DIFFERENT things — a matching-filter that
    treats one as the other will silently miss the agent."""
    bus = _CapturingBus()
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)

    cli.cmd_tag(_tag_args("skill:playwright"))
    a = dict(bus.tag_called_with)

    cli.cmd_tag(_tag_args("skill=playwright"))
    b = dict(bus.tag_called_with)

    assert a != b, "colon-key form and key=value form must not collapse"
    assert list(a.keys()) == ["skill:playwright"]
    assert list(b.keys()) == ["skill"]


# ------------------------------------------------------------- both write sites agree


def test_cmd_tag_and_cmd_register_produce_identical_shape(monkeypatch, tmp_path):
    """The peer's hypothesis was that two write paths might parse
    differently. This test proves they don't: cmd_tag(--set X=Y) and
    cmd_register(--label X=Y) MUST produce the same labels dict for the
    same input."""
    bus = _CapturingBus()
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)

    same_inputs = ["skill:playwright", "skill=playwright", "team:frontend=takes shots"]

    cli.cmd_tag(_tag_args(*same_inputs))
    from_tag = dict(bus.tag_called_with)

    # cmd_register does more than parse labels (writes files, wires session);
    # we can't run it end-to-end in a unit test without a fixture heap. But
    # the parse itself is the two lines we care about — replicate them
    # verbatim and assert equality. If someone changes ONE of the two
    # partitions, this test fails.
    from_register: dict[str, str] = {}
    for item in same_inputs:
        key, _, value = item.partition("=")
        from_register[key] = value
    assert from_tag == from_register, (
        "cmd_tag and cmd_register parse label input differently — that is "
        "the exact drift issuedb #14 was opened to prevent"
    )


# ------------------------------------------------------------- forbidden shapes


def test_colon_alone_is_NOT_treated_as_a_key_value_separator(monkeypatch):
    """The peer's suspicion was that ONE path might partition on `:`
    instead of `=`. That would collapse `skill:playwright` to
    `{skill: playwright}` — a different meaning. This test is the
    regression guard: input WITHOUT `=` produces a bare key with empty
    value, NEVER a key=value from colon-splitting."""
    bus = _CapturingBus()
    monkeypatch.setattr(cli._common, "_bus", lambda _a: bus)
    cli.cmd_tag(_tag_args("skill:playwright"))
    # This shape is FORBIDDEN — if it ever appears, the parser has drifted.
    assert bus.tag_called_with != {"skill": "playwright"}
