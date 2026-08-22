"""A sealing key belongs to ONE agent, even on a shared machine.

WHAT WAS WRONG. The key lived at a single `~/.config/agentbus/keys/sealing.key`
and `key_path()` was documented as "this machine's private key". Two agents
onboarded on one box registered the SAME public key:

    agentbus-81bb81     fingerprint 6f360d6fa1182cbb
    agentbus-ui-12ff19  fingerprint 6f360d6fa1182cbb   ← identical

Demonstrated live rather than argued: the ciphertext of a message sealed to
agentbus-81bb81 was opened with the machine key that agentbus-ui also uses.
Isolation rested entirely on the API refusing to hand over the bytes — so a DB
dump, a backup, or an operator-scope fetch defeated it completely.

It also made a published claim false. "Even agents joining after you won't see
this" was untrue for any agent registered later on the same machine.

`load_private_keys()` was the second half of the hole: it globbed `*.superseded`
across the whole directory, so every agent inherited every other agent's retired
keys. Splitting the filename without fixing the glob would have changed nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from agentbus_client import sealing
from agentbus_client.sealing import CannotDecrypt


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(sealing, "config_dir", lambda: tmp_path)
    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)
    (tmp_path / "keys").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.mark.usefixtures("store")
def test_two_agents_on_one_machine_get_different_keys():
    """THE REGRESSION. Before, both calls returned the same keypair."""
    _priv_a, pub_a = sealing.ensure_keypair("agent-a")
    _priv_b, pub_b = sealing.ensure_keypair("agent-b")
    assert pub_a != pub_b, "two agents on one machine still share one public key"
    assert sealing.key_path("agent-a") != sealing.key_path("agent-b")
    assert sealing.key_path("agent-a").exists()
    assert sealing.key_path("agent-b").exists()


@pytest.mark.usefixtures("store")
def test_one_agents_key_does_not_open_anothers_mail():
    """The property the whole change exists for, asserted on real ciphertext
    rather than on filenames."""
    _priv_a, pub_a = sealing.ensure_keypair("agent-a")
    sealing.ensure_keypair("agent-b")

    sealed = sealing.seal_for("a secret for agent-a only", [pub_a])

    assert sealing.unseal_with_any(sealed, "agent-a") == "a secret for agent-a only"
    with pytest.raises(CannotDecrypt):
        sealing.unseal_with_any(sealed, "agent-b")


@pytest.mark.usefixtures("store")
def test_an_agent_loads_only_its_own_keys():
    """THE SECOND HALF OF THE HOLE. `load_private_keys` used to glob the whole
    directory, so per-agent files alone would have fixed nothing."""
    priv_a, _pub_a = sealing.ensure_keypair("agent-a")
    priv_b, _pub_b = sealing.ensure_keypair("agent-b")

    a_keys = sealing.load_private_keys("agent-a")
    b_keys = sealing.load_private_keys("agent-b")

    assert priv_a in a_keys and priv_b not in a_keys
    assert priv_b in b_keys and priv_a not in b_keys


def test_a_retired_key_is_not_shared_across_agents(store):
    """A rotation must not become a cross-agent disclosure: agent-b must not
    inherit agent-a's superseded key just by living in the same directory."""
    priv_a, _ = sealing.ensure_keypair("agent-a")
    sealing.ensure_keypair("agent-b")
    retired = store / "keys" / "sealing-agent-a-deadbeef.key.superseded"
    retired.write_text(priv_a)

    assert priv_a in sealing.load_private_keys("agent-a")
    assert priv_a not in sealing.load_private_keys("agent-b")


@pytest.mark.usefixtures("store")
def test_a_key_with_no_owner_is_refused():
    """A sealing key with no agent IS the shared key this design removed, so it
    must be impossible to ask for one rather than silently defaulting."""
    with pytest.raises(ValueError) as exc:
        sealing.key_path()
    assert "one agent" in str(exc.value).lower()


@pytest.mark.usefixtures("store")
def test_unbound_agent_loaders_return_none_not_value_error():
    """THE AUDIT FIX. key_path() still REFUSES a no-owner key (above), but the
    READ-side loaders must treat 'no acting agent' as 'this machine
    holds no key for it' — returning None/[] exactly like load_signing_key —
    instead of raising an unhandled ValueError that crashes a send or read."""
    assert sealing.load_private_key() is None
    assert sealing.load_private_keys() == []
    assert sealing.load_signing_key() is None


def test_apply_seal_uses_the_explicit_agent(monkeypatch):
    """THE AUDIT FIX (second half): _apply_seal must resolve the sealing key
    for the agent passed to send(..., agent=...), not the client's own bound
    agent. Before, an explicit agent= was ignored and the send resealed to the
    wrong identity's key."""
    from agentbus_client.client.base import _Base

    real_priv, real_pub = sealing.generate_keypair()  # valid age key, no I/O

    seen: dict[str, str | None] = {}

    def _fake_ensure(agent=None):
        seen["agent"] = agent
        return real_priv, real_pub

    monkeypatch.setattr(sealing, "ensure_keypair", _fake_ensure)

    bus = _Base(api_key="ab_sk_x", base_url="https://x", agent="bound-agent")
    resolved = {
        "encrypted": True,
        "external": False,
        "missing_keys": [],
        "keys": {"recipient": [{"public_key": real_pub}]},
    }
    out = bus._apply_seal({"text": "hello"}, resolved, agent="explicit-agent")
    assert seen["agent"] == "explicit-agent"
    assert out["sealed"] is True


def test_apply_seal_requires_acting_agent_when_unbound(monkeypatch) -> None:
    """B1: an unbound client (no agent=, no AGENTBUS_AGENT) sealing to an
    ENCRYPTED workspace must raise a TYPED AgentBusError, not a raw ValueError
    from ensure_keypair — so an SDK caller catching AgentBusError can handle it."""
    from agentbus_client.client import AgentBusError
    from agentbus_client.client.base import _Base

    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)
    bus = _Base(api_key="ab_sk_x", base_url="https://x")
    resolved = {
        "encrypted": True,
        "external": False,
        "missing_keys": [],
        "keys": {"r": [{"public_key": "k"}]},
    }
    with pytest.raises(AgentBusError) as exc:
        bus._apply_seal({"text": "hi"}, resolved, agent=None)
    assert "no acting agent" in str(exc.value)


def test_seal_to_self_requires_acting_agent_when_unbound(monkeypatch) -> None:
    """B2: same unbound-client guard on the draft path — _seal_to_self seals
    to the acting agent's OWN key, so with no acting agent it is a typed error,
    not a raw ValueError."""
    from agentbus_client.client import AgentBus, AgentBusError

    monkeypatch.delenv("AGENTBUS_AGENT", raising=False)
    bus = AgentBus(api_key="ab_sk_x", base_url="https://x")
    monkeypatch.setattr(bus, "_request", lambda *a, **k: {"encrypted": True, "keys": {}})
    with pytest.raises(AgentBusError) as exc:
        bus._seal_to_self({"text": "hi"}, None)
    assert "no acting agent" in str(exc.value)


def test_the_agent_name_is_sanitised_into_the_filename(store):
    """An agent name reaches a path. Anything that could traverse or collide
    must be neutralised, or a crafted name reads another agent's key."""
    p = sealing.key_path("../../etc/evil")
    # The invariant that matters: the file cannot leave the keys directory.
    assert p.resolve().parent == (store / "keys").resolve()
    assert "/" not in p.name and ".." not in p.name


@pytest.mark.usefixtures("store")
def test_the_environment_supplies_the_agent_when_the_caller_does_not(monkeypatch):
    """Every CLI path should not have to thread it, but the fallback must be an
    explicit agent identity — never 'whatever key is lying around'."""
    monkeypatch.setenv("AGENTBUS_AGENT", "agent-env")
    assert sealing.key_path().name == "sealing-agent-env.key"
