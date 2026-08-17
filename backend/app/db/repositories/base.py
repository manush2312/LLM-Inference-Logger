"""Repository base.

Repositories are the only place that writes SQL. Routers, services and the
ingestion worker all go through them, which means a query can be optimised,
instrumented or rewritten in exactly one place.

They deliberately do *not* commit. Transaction boundaries belong to the caller
(`Database.session()`), so several repository calls can compose into one atomic
unit of work instead of each committing independently.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, Executable
from sqlalchemy.ext.asyncio import AsyncSession


class Repository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    async def _affected_rows(self, statement: Executable) -> int:
        """Execute DML and report how many rows it actually touched.

        `AsyncSession.execute` is typed as returning `Result`, which has no
        `rowcount` -- only the `CursorResult` it returns for DML does. The cast
        is confined here rather than repeated at every call site.
        """
        result = await self._session.execute(statement)
        return cast(CursorResult[Any], result).rowcount
