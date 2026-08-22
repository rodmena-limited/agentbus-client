"""#43: `keys list` must show SIGNING keys, not sealing keys only.

It listed sealing keys while `keys --help` promised "every published key". The
omission was silent, and it cost something real: tracker-manager-0e2462 audited
three identities with this view, concluded none had a published signing key, and
was wrong about all three — agentbus-8dc08d's key was demonstrably signing its
mail at the time.

A WRONG NEGATIVE FROM A VIEW THAT STRUCTURALLY CANNOT PRODUCE A POSITIVE. The
reader had no way to tell "this agent has no signing key" from "this command
never asks about signing keys", and both render as no line at all.

So the absence is now printed explicitly. A listing that silently omits a
category caused this; one that shows an empty section says which it is.
"""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stdout

from agentbus_client.cli import _keys
from agentbus_client.client.errors import AgentBusError


class _Bus:
    def __init__(self, sealing, signing):
        self._sealing, self._signing = sealing, signing
        self.asked: list[str | None] = []

    def _request(self, method, path, params=None, **kw):
        algo = (params or {}).get("algorithm")
        self.asked.append(algo)
        if algo == "ed25519":
            if self._signing is None:
                raise AgentBusError("nope", code="not_found", status=404)
            return {"keys": self._signing}
        return {"keys": self._sealing}


def _run(bus, as_json=False):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _keys._keys_list(bus, argparse.Namespace(json=as_json), "a", None)
    assert rc == 0
    return buf.getvalue()


SEAL = [{"fingerprint": "seal1", "label": "s"}]
SIGN = [{"fingerprint": "sign1", "label": "g"}]


def test_a_published_signing_key_appears():
    out = _run(_Bus(SEAL, SIGN))
    assert "sign1" in out


def test_the_ed25519_surface_is_actually_queried():
    """Asserting on output alone would pass if the key were printed from the
    sealing response by accident. Assert the request that went out."""
    bus = _Bus(SEAL, SIGN)
    _run(bus)
    assert "ed25519" in bus.asked


def test_no_signing_key_says_so_rather_than_printing_nothing():
    """THE AUDIT FAILURE. Silence read as 'none' when it meant 'never asked'."""
    out = _run(_Bus(SEAL, None))
    assert "none published" in out
    assert "keys sign" in out


def test_the_absence_notice_is_absent_when_a_key_exists():
    """Known-negative: a notice that always prints tells the reader nothing."""
    assert "none published" not in _run(_Bus(SEAL, SIGN))


def test_sealing_keys_are_still_listed():
    """Known-positive control: the half that already worked must keep working."""
    assert "seal1" in _run(_Bus(SEAL, SIGN))


def test_json_carries_both_key_types():
    d = json.loads(_run(_Bus(SEAL, SIGN), as_json=True))
    assert d["keys"][0]["fingerprint"] == "seal1"
    assert d["signing_keys"][0]["fingerprint"] == "sign1"


def test_a_failed_signing_lookup_does_not_kill_the_whole_listing():
    """A listing that dies because one of two lookups failed tells the reader
    less than one showing the half it has."""

    class _Broken(_Bus):
        def _request(self, method, path, params=None, **kw):
            if (params or {}).get("algorithm") == "ed25519":
                raise AgentBusError("boom", code="internal_error", status=500)
            return {"keys": SEAL}

    out = _run(_Broken(SEAL, None))
    assert "seal1" in out
