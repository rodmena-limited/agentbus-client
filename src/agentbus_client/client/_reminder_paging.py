from __future__ import annotations

from typing import Any

from .errors import IncompleteListing

API_MAX_LIMIT = 200
MAX_PAGES = 1000


def list_params(
    *,
    every_state: bool,
    state: str | None,
    limit: int,
    cursor: str | None,
) -> dict[str, str]:
    params = {"state": state or ("all" if every_state else "scheduled"), "limit": str(limit)}
    if cursor is not None:
        params["cursor"] = cursor
    return params


class ReminderPager:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.pages = 0
        self._ids: set[object] = set()
        self._cursors: set[str] = set()
        self._last: dict[str, Any] = {}

    def _incomplete(self, why: str) -> IncompleteListing:
        return IncompleteListing(
            f"the reminder listing is incomplete after {self.pages} page(s) and "
            f"{len(self.rows)} row(s): {why}",
            pages=self.pages,
            rows=len(self.rows),
        )

    def take(self, page: dict[str, Any]) -> str | None:
        self.pages += 1
        self._last = page
        for row in page.get("reminders") or []:
            key = row.get("id")
            if key is not None:
                if key in self._ids:
                    continue
                self._ids.add(key)
            self.rows.append(row)
        nxt = page.get("next_cursor")
        why = _unfollowable(page, nxt, self._cursors, self.pages)
        if why:
            raise self._incomplete(why)
        if nxt is None:
            return None
        self._cursors.add(nxt)
        return str(nxt)

    def envelope(self) -> dict[str, Any]:
        return {
            "reminders": self.rows,
            "count": len(self.rows),
            "total": self._last.get("total"),
            "has_more": False,
            "limit": self._last.get("limit"),
            "next_cursor": None,
            "pages": self.pages,
        }


def _unfollowable(page: dict[str, Any], nxt: object, seen: set[str], pages: int) -> str | None:
    if nxt is None:
        return (
            "the server reported has_more but sent no next_cursor" if page.get("has_more") else None
        )
    if not isinstance(nxt, str) or not nxt:
        return f"the server sent an unusable next_cursor {nxt!r}"
    if nxt in seen:
        return f"the server sent cursor {nxt!r} a second time"
    if pages >= MAX_PAGES:
        return f"stopped at the {MAX_PAGES}-page ceiling"
    return None
