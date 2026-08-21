"""The client half of the SPEC 186 surface-parity guard (issuedb #37, SPECS/0025).

The backend restored the matrix for the four surfaces IT owns (llms.txt, MCP,
SKILL.claude, SKILL.opencode) in their fc0a2d2. The split we agreed on thread
01M0GTSGPQNYHG2C7G0D39VJP8: the matrix belongs where the artifact changes, so
CLI/SDK probes belong here.

WHAT THIS GUARDS, and why it is not a duplicate of their tests: the SKILL.md an
agent actually reads is FETCHED FROM THE SERVER (onboarding/_skill.py:112 pulls
/skills/claude-code.md). So this repo can be perfectly correct while the file
installed on a user's machine tells them the wrong thing. Their guard asserts
the served source; this one asserts that OUR surface — the CLI — is consistent
with the rule that source states.

These are OFFLINE structural tests. Nothing here reaches the network: a guard
that silently no-ops when the bus is unreachable is a check that cannot go red,
which is the exact defect SPEC 186 exists to prevent.
"""

from __future__ import annotations

import pytest

from agentbus_client.cli._parser import build_parser


def _commands() -> set[str]:
    sub = next(a for a in build_parser()._actions if a.dest == "command")
    return set(sub.choices)


# ---------------------------------------------------------------- F3: the twin

def test_every_mcp_tool_has_a_cli_twin_or_a_recorded_exemption():
    """The matrix, from the CLI side.

    MCP tool -> the CLI command that reaches the same capability. A tool mapped
    to None is a RECORDED exemption with its reason, never a silent hole — SPEC
    186: "If a capability is deliberately absent from a surface, then that
    absence shall be a recorded decision, not an oversight."
    """
    twin = {
        "bus_register": "register",
        "bus_whoami": "whoami",
        "bus_phonebook": "phonebook",
        "bus_send": "send",
        "bus_reply": "reply",
        "bus_inbox": "inbox",
        "bus_read": "show",
        "bus_ack": "ack",
        "bus_thread": "thread",
        "bus_label": "labels",
        "bus_tag": "tag",
        "bus_attachment": "attachment",
        "bus_draft": "draft",
        "bus_verify_sender": "verify-sender",
        "bus_status": "status",
        "bus_busy": "busy",
        "bus_room_history": "history",
        "bus_room_schema": "schema",
        "bus_usage": "usage",
        "bus_request_approval": "approve",
        # F3 — the gap this ticket closed. It had no twin at all until e340567.
        "bus_approval_status": "approval",
        # EXEMPTION, confirmed by the backend agent on thread
        # 01M0GTSGPQNYHG2C7G0D39VJP8: bus_heartbeat is deliberately MCP-only.
        # The CLI's liveness story is `agentbus watch` + `agentbus health`,
        # which suits a supervised local process better than a manual poke.
        # There is no `agentbus heartbeat` and there should not be.
        "bus_heartbeat": None,
    }
    commands = _commands()
    missing = {
        tool: verb for tool, verb in twin.items() if verb is not None and verb not in commands
    }
    assert not missing, f"MCP tools whose CLI twin vanished: {missing}"


def test_the_heartbeat_exemption_is_real_not_a_forgotten_command():
    """Asserts the exemption in the direction that can actually fail.

    If someone adds `agentbus heartbeat`, the exemption above becomes a lie and
    this test says so — rather than the map quietly documenting a hole that no
    longer exists.
    """
    assert "heartbeat" not in _commands()


# ------------------------------------------------- F3: three outcomes, not two

def test_approval_verb_reaches_an_id_this_process_did_not_create():
    """The whole point of F3: `approve --wait` can only wait on its own id."""
    args = build_parser().parse_args(["approval", "01M0PEER", "--wait", "60"])
    assert args.approval_id == "01M0PEER"


def test_denial_and_no_answer_do_not_share_an_exit_code():
    """The distinction the old `0 if approved else 1` could not express.

    Duplicated deliberately from test_approval_status_verb.py: there it guards
    the implementation, here it guards the CONTRACT the skill documents. If the
    two ever disagree, the docs are wrong and an agent following them acts on a
    denial it read as "still pending".
    """
    from agentbus_client.cli._forward import _report_approval

    assert _report_approval({"id": "a", "status": "timed_out"}) == 1
    assert _report_approval({"id": "a", "status": "pending", "waited_out": True}) == 7


# ------------------------------------------- F1: the CLI is the sealing surface

@pytest.mark.parametrize("verb", ["send", "reply", "forward", "draft", "show", "inbox", "thread"])
def test_the_cli_carries_every_verb_mcp_cannot_serve_on_an_encrypted_workspace(verb):
    """F1, both directions.

    MCP holds no private key, so on an encrypted workspace it can neither seal
    nor unseal: the four write verbs REFUSE, and the read verbs SUCCEED while
    handing back `-----BEGIN AGE ENCRYPTED FILE-----`. Reproduced live on
    delivery 01M0GV4P5R8A1EFXR73P4JACC3.

    That makes the CLI the ONLY surface for message bodies there. Every verb
    below is therefore load-bearing, and losing one silently strands an
    encrypted workspace with no working path at all.
    """
    assert verb in _commands()


def test_the_client_can_unseal_which_is_the_capability_mcp_structurally_lacks():
    """The asymmetry in F1 is not a missing feature, it is a key location.

    If `unseal_message` ever leaves the client, the CLI stops being the answer
    to F1 and the guidance in every surface becomes wrong at once.
    """
    from agentbus_client.client import AgentBus

    assert callable(getattr(AgentBus, "unseal_message", None))


# ------------------------------------------------- local-only stays local-only

@pytest.mark.parametrize(
    "verb", ["setup", "signin", "keys", "watch", "service", "doctor", "identity", "join"]
)
def test_machine_local_verbs_are_cli_only_by_nature(verb):
    """These touch THIS machine — a key file, a supervisor, a config directory.

    A remote MCP server cannot serve them at all, so their absence from MCP is
    structural rather than drift. Recorded here so a future parity sweep does
    not "fix" it by inventing bus_signin.
    """
    assert verb in _commands()
