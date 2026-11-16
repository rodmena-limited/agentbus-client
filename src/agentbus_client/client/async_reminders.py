from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from ._reminder_paging import API_MAX_LIMIT, ReminderPager, list_params

if TYPE_CHECKING:
    from ._mixin_base import AsyncClientBase as _MixinBase
else:
    _MixinBase = object


class AsyncRemindersMixin(_MixinBase):
    async def reminds(self, *, agent: str | None = None, all: bool = False) -> list[dict[str, Any]]:
        """Every scheduled reminder, following `next_cursor` to the last page.

        Mine by default; every state with all=True. Raises IncompleteListing
        when the pages cannot be followed to the end. Not `reminders()`, which
        lists unacked sent messages.
        """
        page = await self.reminds_every_page(agent=agent, all=all)
        return cast("list[dict[str, Any]]", page["reminders"])

    async def reminds_page(
        self,
        *,
        agent: str | None = None,
        all: bool = False,
        state: str | None = None,
        limit: int = API_MAX_LIMIT,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """One page: {reminders, count, total, has_more, limit, next_cursor}.

        `state` overrides `all`. Pass `next_cursor` back as `cursor` until it
        is null. `total` counts rows under the same state filter.
        """
        params = list_params(every_state=all, state=state, limit=limit, cursor=cursor)
        result: dict[str, Any] = await self._request(
            "GET", "/v1/reminders", params=params, agent=agent
        )
        return result

    async def reminds_every_page(
        self,
        *,
        agent: str | None = None,
        all: bool = False,
        state: str | None = None,
        limit: int = API_MAX_LIMIT,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Follow `next_cursor` from `cursor` to the last page and merge the rows.

        Returns the envelope shape with has_more false, next_cursor null and
        `pages` set. Raises IncompleteListing if a cursor repeats, the server
        reports has_more without a cursor, or 1,000 pages are reached.
        """
        pager = ReminderPager()
        nxt = cursor
        while True:
            page = await self.reminds_page(
                agent=agent, all=all, state=state, limit=limit, cursor=nxt
            )
            nxt = pager.take(page)
            if nxt is None:
                return pager.envelope()
