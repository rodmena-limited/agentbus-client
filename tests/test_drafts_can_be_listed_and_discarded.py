from __future__ import annotations

import io
import json
from contextlib import redirect_stdout

from agentbus_client.cli import _common, main
from agentbus_client.client import AgentBus, AsyncAgentBus

REAL_SHAPE = {
    "id": "01M2BY24DX5GVS6KTGH239RK76",
    "subject": "audit probe: draft shape",
    "recipients": ["agentbus-client-c70fbf"],
    "thread_id": None,
    "updated_at": "2026-09-12T23:08:24.894741Z",
}


class _Bus:
    def __init__(self, drafts):
        self._drafts = drafts
        self.deleted: list[str] = []
        self.listed = 0

    def drafts(self, agent=None):
        self.listed += 1
        return self._drafts

    def delete_draft(self, draft_id, agent=None):
        self.deleted.append(draft_id)


def _run(monkeypatch, bus, argv):
    monkeypatch.setattr(_common, "_bus", lambda args: bus)
    out = io.StringIO()
    with redirect_stdout(out):
        rc = main(argv)
    return rc, out.getvalue()


def test_drafts_are_listed_for_a_person_not_as_json(monkeypatch):
    rc, out = _run(monkeypatch, _Bus([REAL_SHAPE]), ["drafts"])
    assert rc == 0
    assert not out.lstrip().startswith("[")
    assert REAL_SHAPE["id"] in out
    assert "agentbus-client-c70fbf" in out
    assert "audit probe: draft shape" in out
    assert "agentbus drafts --delete" in out


def test_no_drafts_says_so(monkeypatch):
    rc, out = _run(monkeypatch, _Bus([]), ["drafts"])
    assert rc == 0
    assert out.strip() == "no drafts"


def test_json_listing_stays_the_server_list(monkeypatch):
    rc, out = _run(monkeypatch, _Bus([REAL_SHAPE]), ["drafts", "--json"])
    assert rc == 0
    assert json.loads(out) == [REAL_SHAPE]


def test_delete_discards_one_draft_without_listing(monkeypatch):
    bus = _Bus([REAL_SHAPE])
    rc, out = _run(monkeypatch, bus, ["drafts", "--delete", REAL_SHAPE["id"]])
    assert rc == 0
    assert bus.deleted == [REAL_SHAPE["id"]]
    assert bus.listed == 0
    assert f"discarded draft {REAL_SHAPE['id']}" in out


def test_both_sdks_can_delete_a_draft():
    assert callable(getattr(AgentBus, "delete_draft", None))
    assert callable(getattr(AsyncAgentBus, "delete_draft", None))
