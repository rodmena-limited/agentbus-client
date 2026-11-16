"""#98: reminder paging with cursors (server #409, #410, #421).

The fake server follows the contract agentbus-8dc08d sent on 2026-11-16 and
the route in agentbus routes_reminders.py at 800f2fb: state, limit 1..200 and
cursor are the only parameters, anything else is 422 validation_error, rows are
newest first, `total` counts the rows under the same state filter, and
`next_cursor` is null on the last page.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import httpx
import pytest

from agentbus_client import cli
from agentbus_client.client import AgentBus, AsyncAgentBus, IncompleteListing

BASE = "https://bus.test"
STATES = {"scheduled", "fired", "cancelled", "expired", "failed", "all"}


def _rows(live: int, finished: int = 0) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [
        {"id": f"rem_{i:04d}", "state": "scheduled", "due_at": f"2026-12-01T00:{i % 60:02d}:00Z"}
        for i in range(live)
    ]
    rows += [{"id": f"rem_f{i:03d}", "state": "fired"} for i in range(finished)]
    return rows


class FakeServer:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.requests: list[dict[str, str]] = []

    def _error(self, detail: str) -> httpx.Response:
        body = {"code": "validation_error", "status": 422, "detail": detail}
        return httpx.Response(422, json=body)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append(params)
        if request.url.path != "/v1/reminders" or request.method != "GET":
            return httpx.Response(404, json={"code": "not_found", "detail": "no route"})
        extra = set(params) - {"state", "limit", "cursor"}
        if extra:
            return self._error(f"unknown query parameter: {sorted(extra)}")
        state = params.get("state", "scheduled")
        if state not in STATES:
            return self._error(f"unknown state {state!r}")
        limit = int(params.get("limit", "50"))
        if not 1 <= limit <= 200:
            return self._error("limit: must be between 1 and 200")
        matching = [r for r in self.rows if state == "all" or r["state"] == state]
        start = 0
        if "cursor" in params:
            try:
                last_id = base64.urlsafe_b64decode(params["cursor"].encode()).decode()
                start = next(i for i, r in enumerate(matching) if r["id"] == last_id) + 1
            except (ValueError, StopIteration, UnicodeDecodeError):
                return self._error("cursor: malformed")
        page = matching[start : start + limit]
        more = start + limit < len(matching)
        nxt = base64.urlsafe_b64encode(page[-1]["id"].encode()).decode() if more else None
        return httpx.Response(
            200,
            json={
                "reminders": page,
                "count": len(page),
                "total": len(matching),
                "has_more": more,
                "limit": limit,
                "next_cursor": nxt,
            },
        )


def _bus(server: Any) -> AgentBus:
    bus = AgentBus(api_key="ab_sk_x_y", base_url=BASE, agent="me-1")
    bus._client = httpx.Client(transport=httpx.MockTransport(server), base_url=BASE)
    return bus


def _cli(monkeypatch: pytest.MonkeyPatch, server: Any, argv: list[str]) -> int:
    bus = _bus(server)
    monkeypatch.setattr(cli._common, "_bus", lambda _args: bus)
    return cli.main(argv)


def test_one_page_carries_the_cursor_both_ways() -> None:
    server = FakeServer(_rows(239))
    bus = _bus(server)
    first = bus.reminds_page(limit=50)
    assert first["count"] == 50
    assert first["has_more"] is True
    assert isinstance(first["next_cursor"], str)
    second = bus.reminds_page(limit=50, cursor=first["next_cursor"])
    assert second["reminders"][0]["id"] == "rem_0050"
    assert server.requests[1] == {
        "state": "scheduled",
        "limit": "50",
        "cursor": first["next_cursor"],
    }


def test_state_overrides_all() -> None:
    server = FakeServer(_rows(3, finished=2))
    page = _bus(server).reminds_page(state="fired", all=True)
    assert [r["id"] for r in page["reminders"]] == ["rem_f000", "rem_f001"]
    assert server.requests[0]["state"] == "fired"


def test_reminds_returns_every_row_not_the_first_200() -> None:
    server = FakeServer(_rows(239))
    rows = _bus(server).reminds()
    assert len(rows) == 239
    assert len({r["id"] for r in rows}) == 239
    assert [r["id"] for r in rows] == [f"rem_{i:04d}" for i in range(239)]
    assert len(server.requests) == 2


def test_every_page_merges_and_says_it_is_complete() -> None:
    server = FakeServer(_rows(239))
    env = _bus(server).reminds_every_page(limit=50)
    assert env["count"] == 239
    assert env["total"] == 239
    assert env["has_more"] is False
    assert env["next_cursor"] is None
    assert env["pages"] == 5
    assert len(server.requests) == 5


def test_an_empty_listing_is_one_request_and_complete() -> None:
    server = FakeServer([])
    env = _bus(server).reminds_every_page()
    assert env["reminders"] == []
    assert env["pages"] == 1
    assert env["has_more"] is False


def _scripted(pages: list[dict[str, Any]]) -> Any:
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.url.params))
        return httpx.Response(200, json=pages[min(len(calls) - 1, len(pages) - 1)])

    handler.calls = calls  # type: ignore[attr-defined]
    return handler


def test_a_repeated_cursor_stops_with_incomplete_listing() -> None:
    page = {"reminders": [{"id": "a"}], "count": 1, "total": 9, "has_more": True, "limit": 1}
    handler = _scripted([{**page, "next_cursor": "c1"}, {**page, "next_cursor": "c1"}])
    with pytest.raises(IncompleteListing) as caught:
        _bus(handler).reminds_every_page()
    assert caught.value.code == "incomplete_listing"
    assert caught.value.pages == 2
    assert "'c1' a second time" in caught.value.detail
    assert len(handler.calls) == 2


def test_has_more_without_a_cursor_is_incomplete_not_complete() -> None:
    page = {"reminders": [{"id": "a"}], "count": 1, "total": 239, "has_more": True, "limit": 200}
    with pytest.raises(IncompleteListing) as caught:
        _bus(_scripted([page])).reminds()
    assert "has_more but sent no next_cursor" in caught.value.detail


def test_an_endless_cursor_stops_at_the_page_ceiling() -> None:
    count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        count["n"] += 1
        return httpx.Response(
            200,
            json={
                "reminders": [{"id": f"r{count['n']}"}],
                "count": 1,
                "total": 10**6,
                "has_more": True,
                "limit": 1,
                "next_cursor": f"c{count['n']}",
            },
        )

    with pytest.raises(IncompleteListing) as caught:
        _bus(handler).reminds_every_page(limit=1)
    assert count["n"] == 1000
    assert caught.value.pages == 1000
    assert caught.value.rows == 1000


@pytest.mark.anyio
async def test_the_async_twin_follows_every_page() -> None:
    server = FakeServer(_rows(239))
    bus = AsyncAgentBus(api_key="ab_sk_x_y", base_url=BASE, agent="me-1")
    bus._client = httpx.AsyncClient(transport=httpx.MockTransport(server), base_url=BASE)
    rows = await bus.reminds()
    assert len(rows) == 239
    env = await bus.reminds_every_page(limit=100)
    assert env["pages"] == 3
    page = await bus.reminds_page(limit=10)
    assert page["next_cursor"] is not None
    second = await bus.reminds_page(limit=10, cursor=page["next_cursor"])
    assert second["reminders"][0]["id"] == "rem_0010"
    await bus.aclose()


def test_cli_json_stays_a_list(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(monkeypatch, FakeServer(_rows(239)), ["--json", "reminds"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert isinstance(out, list)
    assert len(out) == 200


def test_cli_all_pages_json_lists_every_row(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(monkeypatch, FakeServer(_rows(239)), ["--json", "reminds", "--all-pages"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out) == 239


def test_cli_envelope_keeps_the_paging_fields(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(monkeypatch, FakeServer(_rows(239)), ["reminds", "--envelope"]) == 0
    env = json.loads(capsys.readouterr().out)
    assert env["count"] == 200
    assert env["total"] == 239
    assert env["has_more"] is True
    assert isinstance(env["next_cursor"], str)
    assert env["limit"] == 200


def test_cli_envelope_all_pages_is_complete(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    server = FakeServer(_rows(239))
    argv = ["--json", "reminds", "--all-pages", "--envelope", "--limit", "100"]
    assert _cli(monkeypatch, server, argv) == 0
    env = json.loads(capsys.readouterr().out)
    assert env["count"] == 239
    assert env["has_more"] is False
    assert env["next_cursor"] is None
    assert [p["limit"] for p in server.requests] == ["100", "100", "100"]


@pytest.mark.parametrize("bad", ["0", "201", "-5"])
def test_cli_refuses_a_limit_outside_the_api_range(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], bad: str
) -> None:
    server = FakeServer(_rows(3))
    assert _cli(monkeypatch, server, ["reminds", "--limit", bad]) == 2
    assert server.requests == []
    assert f"--limit takes 1 to 200; got {bad}" in capsys.readouterr().err


def test_cli_table_names_a_next_page_command_that_works(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    server = FakeServer(_rows(120))
    assert _cli(monkeypatch, server, ["reminds", "--limit", "50"]) == 0
    out = capsys.readouterr().out
    assert "SHOWING 50 OF 120" in out
    line = next(ln for ln in out.splitlines() if "next page:" in ln)
    command = line.split("next page:", 1)[1].split()
    assert command[:4] == ["agentbus", "reminds", "--limit", "50"]
    assert command[4] == "--cursor"
    assert _cli(monkeypatch, server, command[1:]) == 0
    second = capsys.readouterr().out
    assert "rem_0050" in second
    assert "rem_0049" not in second


def test_cli_last_page_has_no_next_page_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(monkeypatch, FakeServer(_rows(5)), ["reminds"]) == 0
    out = capsys.readouterr().out
    assert "next page" not in out
    assert "SHOWING" not in out


def test_cli_a_malformed_cursor_prints_the_server_detail(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(monkeypatch, FakeServer(_rows(5)), ["reminds", "--cursor", "!!!"]) == 2
    assert "could not list reminders: cursor: malformed" in capsys.readouterr().err


def test_cli_incomplete_listing_exits_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    page = {"reminders": [{"id": "a"}], "count": 1, "total": 9, "has_more": True, "limit": 1}
    handler = _scripted([page])
    assert _cli(monkeypatch, handler, ["reminds", "--all-pages"]) == 1
    assert "could not list every reminder" in capsys.readouterr().err


def test_cli_counts_finished_rows_beyond_the_first_page(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(monkeypatch, FakeServer(_rows(239, finished=10)), ["reminds"]) == 0
    out = capsys.readouterr().out
    assert "(10 finished reminder(s) hidden" in out
    assert "SHOWING 200 OF 239" in out


LIMIT_REACHED = (
    '{"type":"about:blank","title":"Structural limit reached","status":422,'
    '"code":"limit_reached","detail":"this agent holds 1000 live reminders and its '
    "plan allows 1000; cancel reminders it no longer needs (agentbus reminds, then "
    'agentbus remind --cancel <id>) or let one-shots fire, then arm again","limit":1000}'
)


def test_remind_prints_the_limit_reached_detail_and_limit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/recipients/resolve":
            return httpx.Response(200, json={"encrypted": False, "keys": {}})
        return httpx.Response(
            422, content=LIMIT_REACHED, headers={"content-type": "application/problem+json"}
        )

    code = _cli(monkeypatch, handler, ["remind", "-m", "x", "--delay", "1h"])
    err = capsys.readouterr().err
    assert code == 3
    assert "could not schedule: this agent holds 1000 live reminders" in err
    assert "this agent's plan allows 1000 live reminders" in err
    assert [(r.method, r.url.path) for r in seen] == [
        ("POST", "/v1/recipients/resolve"),
        ("POST", "/v1/reminders"),
    ]
