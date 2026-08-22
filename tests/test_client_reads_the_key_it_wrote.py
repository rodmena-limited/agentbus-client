"""The client must read the credential its own installer just wrote.

THE ONBOARDING FAILURE THIS PINS.

A new customer ran the advertised one-liner on an ENCRYPTED workspace. `signin`
verified the key and stored it at ~/.config/agentbus/operator.env. Seconds
later, in the same command, `setup` reported:

    sealing key  NOT REGISTERED (no API key. ... save it to
    ~/.config/agentbus/keys/../operator.env or export AGENTBUS_API_KEY)

The key existed, was valid, and was in exactly the file the message named. The
cause was that `AgentBus.__init__` resolved credentials from an explicit
argument or $AGENTBUS_API_KEY and NOWHERE ELSE — it never read a file — while
the sealing step at onboarding.py builds `AgentBus(base_url=..., agent=name)`
with neither.

Why it mattered rather than merely annoyed: registration succeeded, the bound
key was minted, MCP was wired, and setup ended on a green "Next" panel. The one
line that failed is the one that makes an encrypted workspace encrypted — no
published sealing key means peers cannot seal to this agent at all, and the
customer had no reason to look.
"""

from __future__ import annotations

import os
import pathlib

import pytest

from agentbus_client import client as client_module


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An isolated HOME, so the developer's real credentials cannot mask a bug."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("AGENTBUS_API_KEY", raising=False)
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)
    # NO importlib.reload here. Reloading swapped the module object out from
    # under tests that already held references to it, breaking three unrelated
    # tests only when the suite ran as a whole. `_key_from_disk` calls
    # expanduser("~") on every call, which reads $HOME live, so monkeypatching
    # the environment is sufficient and leaves the module identity alone.
    cfg = tmp_path / ".config" / "agentbus"
    (cfg / "keys").mkdir(parents=True)
    return cfg


def _write(path: pathlib.Path, key: str) -> None:
    """The exact shape onboarding writes: `export KEY=value` lines."""
    path.write_text(f"export AGENTBUS_API_KEY={key}\nexport AGENTBUS_AGENT=a1\n")


def test_the_sealing_step_can_construct_a_client(home):
    """THE REGRESSION, as the failing call. RED before the fix: AuthError."""
    _write(home / "operator.env", "ab_sk_OPERATOR")
    _write(home / "keys" / "agentbus-abc123.env", "ab_sk_BOUND")

    bus = client_module.AgentBus(base_url="https://x", agent="agentbus-abc123")

    assert bus.api_key == "ab_sk_BOUND"


@pytest.mark.usefixtures("home")
def test_a_keyless_machine_still_raises():
    """THE CONTROL. Without this the test above passes on any change at all —
    a resolver that returned a constant would look identical."""
    with pytest.raises(client_module.AuthError):
        client_module.AgentBus(base_url="https://x", agent="nobody")


def test_the_bound_key_is_preferred_over_the_operator_key(home):
    """Least privilege. The operations this fallback serves are `self`
    operations, so reaching for the unbound operator credential by default
    would hand routine calls the most powerful key on the box."""
    _write(home / "operator.env", "ab_sk_OPERATOR")
    _write(home / "keys" / "a1.env", "ab_sk_BOUND")
    assert client_module._key_from_disk("a1") == "ab_sk_BOUND"


def test_a_named_agent_never_borrows_the_operator_key(home):
    """SEV-1-B (#234): the pre-fix behaviour let AgentBus(agent="peer") silently
    fall through to operator.env when keys/peer.env was missing, so any Python
    script or notebook that constructed AgentBus with an arbitrary agent name
    acted as that peer WITH OPERATOR AUTHORITY, and the server saw a signed,
    attested send from that peer. The CLI's resolve_credentials refused this;
    the SDK constructor bypassed it. Now the SDK enforces the same rule: a
    named agent gets its own bound key OR NOTHING — never the operator's.
    """
    _write(home / "operator.env", "ab_sk_OPERATOR")
    # Named agent with no bound file: EMPTY. The subsequent constructor call
    # will raise AuthError naming this, rather than silently escalating.
    assert client_module._key_from_disk("not-registered-yet") == ""
    # Un-named callers still get operator.env — the legitimate acting-mode
    # (a NEW-agent registration builds AgentBus with no agent).
    assert client_module._key_from_disk(None) == "ab_sk_OPERATOR"


def test_a_named_agent_raises_a_clear_error_when_its_bound_key_is_absent(home):
    """SEV-1-B follow-through: the error must NAME what happened, not send an
    operator hunting for a key they already had in operator.env."""
    _write(home / "operator.env", "ab_sk_OPERATOR")
    with pytest.raises(client_module.AuthError) as exc:
        client_module.AgentBus(base_url="https://x", agent="peer-nobody")
    text = str(exc.value)
    assert "peer-nobody" in text
    assert "REFUSES to fall back" in text
    assert "operator.env" in text  # named as what we refused, not as advice


def test_an_explicit_key_wins_over_disk_and_env(home):
    """The explicit `api_key=...` constructor arg is the strongest signal
    and must beat every other source. Unchanged."""
    _write(home / "keys" / "a1.env", "ab_sk_BOUND")
    assert client_module.AgentBus(base_url="https://x", api_key="ab_sk_ARG").api_key == "ab_sk_ARG"


def test_disk_wins_over_env_when_agent_is_named_dotenv_poisoning_defense(home):
    """When the caller names an agent AND a bound key exists on disk for
    that agent, the disk key wins over $AGENTBUS_API_KEY.

    Changed from the older env-wins-when-named contract (2026-08-17). Root
    cause traced by backend agentbus-8dc08d (thread
    01M08QS3M10M49WKT8WVX3P2P7): `resilient_circuit/storage.py` calls
    `load_dotenv()` at IMPORT time, and find_dotenv() walks UP from that
    module's file inside `.venv/lib/python3.13/site-packages/`. Any parent
    directory's `.env` containing `AGENTBUS_API_KEY=<other-key>` therefore
    stomps os.environ silently — and if that stomped key is bound to a
    deleted workspace, every downstream call sees `WorkspaceDeleted`
    while the correct freshly-minted bound key sits ignored on disk.

    Trade-off: an operator who WANTS to override a bound-agent's disk key
    via $env has to pass --api-key / api_key=... explicitly now. That's a
    small ergonomic cost. Silent .env poisoning was catastrophic.
    """
    _write(home / "keys" / "a1.env", "ab_sk_BOUND")
    os.environ["AGENTBUS_API_KEY"] = "ab_sk_ENV_POISONED"
    try:
        assert client_module.AgentBus(base_url="https://x", agent="a1").api_key == "ab_sk_BOUND"
    finally:
        del os.environ["AGENTBUS_API_KEY"]


def test_env_still_wins_in_the_unnamed_agent_operator_cli_path(home):
    """When NO agent is named (the `agentbus signin`, `agentbus register`
    operator paths), env keeps winning. The tightened rule is
    'disk-when-named wins over env', not 'disk always beats env'."""
    _write(home / "operator.env", "ab_sk_OPERATOR_ON_DISK")
    os.environ["AGENTBUS_API_KEY"] = "ab_sk_ENV_WINS_HERE"
    try:
        # no agent named
        assert client_module.AgentBus(base_url="https://x").api_key == "ab_sk_ENV_WINS_HERE"
    finally:
        del os.environ["AGENTBUS_API_KEY"]


def test_comments_and_blank_lines_are_not_mistaken_for_a_key(home):
    """A commented-out key must not be resurrected — that would silently
    authenticate as a credential the operator believed was disabled."""
    (home / "operator.env").write_text(
        "\n# export AGENTBUS_API_KEY=ab_sk_DISABLED\n\nexport AGENTBUS_API_KEY=ab_sk_LIVE\n"
    )
    assert client_module._key_from_disk(None) == "ab_sk_LIVE"


def test_an_empty_value_is_not_a_key(home):
    """`export AGENTBUS_API_KEY=` is a half-written file, not a credential.
    Returning "" from it would produce the confusing 401 this fix removes."""
    (home / "operator.env").write_text("export AGENTBUS_API_KEY=\n")
    assert client_module._key_from_disk(None) == ""


def test_quoted_values_are_unwrapped(home):
    """Operators quote things. A key returned with its quotes attached fails
    authentication with a message that blames the key, not the parser."""
    (home / "operator.env").write_text('export AGENTBUS_API_KEY="ab_sk_QUOTED"\n')
    assert client_module._key_from_disk(None) == "ab_sk_QUOTED"


def test_an_unreadable_file_is_skipped_not_fatal(home):
    """A root-owned or 0000 credential file must not crash every CLI call."""
    bad = home / "operator.env"
    bad.write_text("export AGENTBUS_API_KEY=ab_sk_X\n")
    bad.chmod(0o000)
    try:
        assert client_module._key_from_disk(None) == ""
    finally:
        bad.chmod(0o600)


@pytest.mark.usefixtures("home")
def test_the_error_names_every_place_it_looked():
    """The old message named a file the code could not read, which sent an
    operator hunting for a key he already had. The message must describe the
    search that actually happened.

    SEV-1-B (#234): when an agent IS named, the search set is smaller (bound
    file only, never operator.env) — the message names precisely that, so a
    caller sees WHY operator.env was not consulted.
    """
    with pytest.raises(client_module.AuthError) as exc:
        client_module.AgentBus(base_url="https://x", agent="a1")
    text = str(exc.value)
    assert "AGENTBUS_API_KEY" in text
    assert "keys/a1.env" in text
    assert "operator.env" in text  # named as REFUSED, so the caller knows why
    assert "agent 'a1'" in text  # names the identity the caller asserted


@pytest.mark.usefixtures("home")
def test_the_error_when_no_agent_is_named_still_advises_signin():
    """The un-named path is the pre-signin case: send the operator to signin."""
    with pytest.raises(client_module.AuthError) as exc:
        client_module.AgentBus(base_url="https://x")
    text = str(exc.value)
    assert "AGENTBUS_API_KEY" in text
    assert "operator.env" in text
    assert "agentbus signin" in text
