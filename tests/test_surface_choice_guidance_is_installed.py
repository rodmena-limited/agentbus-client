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


# ------------------------------------------------------------------ surfaces
#
# The surfaces THIS repo ships. The backend's guard owns llms.txt, MCP and the
# two skills; the split is "the matrix lives where the artifact changes".

SURFACES = ("CLI", "SDK")


# ------------------------------------------------------------- capabilities
#
# One row per capability, one probe per surface — mirroring the shape of the
# backend's guard (tests/test_every_capability_reaches_every_surface.py in the
# agentbus repo) deliberately, so the two matrices read the same way instead of
# inventing a second dialect. The key IS the MCP tool name, so a reader can line
# the two files up without guessing.
#
# ON PROBE PRECISION, their rule and ours: a probe is a SPECIFIC token at a
# definition site — a registered subcommand, a method on the client class —
# never a bare word a docstring might use in a sentence. Probes are checked
# against a known-negative below, so one that matches everything fails this file
# rather than turning the matrix green.

CAPABILITIES: dict[str, dict[str, str]] = {
    "bus_register": {"CLI": "register", "SDK": "register"},
    "bus_whoami": {"CLI": "whoami", "SDK": "whoami"},
    "bus_phonebook": {"CLI": "phonebook", "SDK": "phonebook"},
    "bus_send": {"CLI": "send", "SDK": "send"},
    "bus_reply": {"CLI": "reply", "SDK": "reply"},
    "bus_inbox": {"CLI": "inbox", "SDK": "inbox"},
    "bus_read": {"CLI": "show", "SDK": "read"},
    "bus_ack": {"CLI": "ack", "SDK": "ack"},
    "bus_thread": {"CLI": "thread", "SDK": "thread"},
    "bus_label": {"CLI": "labels", "SDK": "label"},
    "bus_tag": {"CLI": "tag", "SDK": "tag"},
    "bus_attachment": {"CLI": "attachment", "SDK": "attachment"},
    "bus_draft": {"CLI": "draft", "SDK": "create_draft"},
    "bus_status": {"CLI": "status", "SDK": "status"},
    "bus_busy": {"CLI": "busy", "SDK": "busy"},
    "bus_request_approval": {"CLI": "approve", "SDK": "request_approval"},
    # F3 — the gap this ticket closed. No twin on either surface until e340567
    # surfaced the SDK method that had been there all along.
    "bus_approval_status": {"CLI": "approval", "SDK": "approval"},
    # A ROW, not merely an EXEMPTIONS entry. An exemption whose capability is
    # absent from the matrix is never parametrized, so its skip branch is dead
    # code and the "exemption" asserts nothing at all — a check that cannot go
    # red, hiding inside the file whose job is to catch those. The probes are
    # the names it WOULD have if it were ever un-exempted.
    "bus_heartbeat": {"CLI": "heartbeat", "SDK": "heartbeat"},
}


# --------------------------------------------------------------- exemptions
#
# A DELIBERATE absence, WITH the reason. Anything here is a decision someone
# defended; anything NOT here must be present. Keyed (capability, surface),
# matching the backend's dict[(capability, surface), reason].

EXEMPTIONS: dict[tuple[str, str], str] = {
    ("bus_heartbeat", "CLI"): (
        "Deliberately MCP-only, confirmed by the backend agent on thread "
        "01M0GTSGPQNYHG2C7G0D39VJP8. The CLI's liveness story is `agentbus "
        "watch` + `agentbus health` — a supervised local process, which fits "
        "the problem better than a manual poke."
    ),
    # NO SDK EXEMPTION. AgentBus.heartbeat() exists and always has — an
    # earlier draft of this file exempted it on both surfaces "for the same
    # reason", which was a guess, not a check. The known-negative in
    # test_the_matrix_mechanism_can_tell_present_from_absent caught it: the
    # assertion that bus_heartbeat is absent from the SDK failed, because it is
    # not absent. A false exemption is worse than a missing one — it records a
    # deliberate decision that nobody made, about a capability that shipped.
}

KNOWN_CAPABILITIES = set(CAPABILITIES)


def _sdk_has(name: str) -> bool:
    from agentbus_client.client import AgentBus

    return callable(getattr(AgentBus, name, None))


def _surface_has(capability: str, surface: str) -> bool:
    probe = CAPABILITIES[capability][surface]
    return probe in _commands() if surface == "CLI" else _sdk_has(probe)


@pytest.mark.parametrize("capability", sorted(CAPABILITIES))
@pytest.mark.parametrize("surface", SURFACES)
def test_every_capability_reaches_every_surface(capability, surface):
    """The matrix. A missing cell fails the build unless EXEMPTIONS explains it."""
    if (capability, surface) in EXEMPTIONS:
        pytest.skip(f"exempt: {EXEMPTIONS[(capability, surface)]}")
    assert _surface_has(capability, surface), (
        f"{capability} does not reach the {surface} surface and no EXEMPTIONS "
        f"entry explains why. Add it to the surface, or record the absence "
        f"with a reason."
    )


# ---------------------------------------------------- the honesty tests
#
# Mirrored from the backend's guard. These keep the matrix from passing
# vacuously, and they are the half most easily left out — a matrix with no
# honesty tests is a check that cannot go red.


def test_a_probe_that_appears_nowhere_matches_nothing():
    """A probe must not be able to pass by matching everything."""
    assert "zzz-no-such-subcommand" not in _commands()
    assert not _sdk_has("zzz_no_such_method")


def test_the_matrix_mechanism_can_tell_present_from_absent():
    """Known-positive AND known-negative for `_surface_has` itself.

    If the lookup were wired to something that always returned True, every row
    above would pass while asserting nothing. Both directions are checked, since
    only the negative can catch that.
    """
    assert _surface_has("bus_send", "CLI") and _surface_has("bus_send", "SDK")
    # bus_heartbeat is absent from the CLI (the exempt cell) and PRESENT on the
    # SDK, so this one capability exercises the mechanism in both directions.
    assert not _surface_has("bus_heartbeat", "CLI")
    assert _surface_has("bus_heartbeat", "SDK"), (
        "AgentBus.heartbeat() exists; if this fails the SDK lost a method and "
        "the CLI-only exemption below needs re-examining, not extending."
    )


def test_no_exemption_names_a_capability_that_no_longer_exists():
    """An exemption must not outlive its subject.

    Precisely how SPEC 186's guard rotted: the artifact moved, the record
    stayed, and the record went on asserting something true about nothing.
    """
    stale = {cap for cap, _s in EXEMPTIONS if cap not in KNOWN_CAPABILITIES}
    assert not stale, f"EXEMPTIONS names capabilities that no longer exist: {stale}"


def test_every_exemption_is_actually_REACHED_by_the_matrix():
    """An exemption must be EVALUATED, not merely well-formed.

    The stale-exemption test above asks whether an exemption names a live
    capability. This asks the harder question: does the matrix ever VISIT that
    cell? An exemption whose (capability, surface) pair is never parametrized is
    dead code — the skip branch never runs, the reason string is decoration, and
    the "exemption" asserts nothing at all.

    This file shipped with exactly that defect: bus_heartbeat sat in EXEMPTIONS
    but not in CAPABILITIES, so it passed the stale check (the capability was
    real) while never being evaluated. A check that cannot go red, hiding inside
    the file whose purpose is catching those. The backend agent took the same
    gap as a defect in their guard rather than a difference in shape
    (thread 01M0GTSGPQNYHG2C7G0D39VJP8).
    """
    evaluated = {(cap, surface) for cap in CAPABILITIES for surface in SURFACES}
    unreachable = set(EXEMPTIONS) - evaluated
    assert not unreachable, (
        f"EXEMPTIONS entries the matrix never evaluates, so their skip branch "
        f"is dead code and they assert nothing: {unreachable}"
    )


def test_no_exemption_is_reasonless():
    """'Exempt because it was easier' cannot pass as a reason."""
    empty = [key for key, reason in EXEMPTIONS.items() if not (reason or "").strip()]
    assert not empty, f"exemptions with no reason: {empty}"


def test_the_heartbeat_exemption_is_asserted_in_the_direction_that_can_fail():
    """If someone adds `agentbus heartbeat`, the exemption has become a lie.

    An exemption asserted only as "absent" is half a test: it stays green both
    when the absence is deliberate and when someone quietly ended it.
    """
    assert "heartbeat" not in _commands()


# ---------------------------------------------------------------- F3: the twin
# ---------------------------------------------------------------- F3: the twin


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
