"""Database package — backward-compatible re-export of Database class.

``from p8.services.database import Database`` continues to work.
"""

from __future__ import annotations

from uuid import UUID

from p8.services.database.pool import PoolMixin
from p8.services.database.query_engine import RemQuery, RemQueryEngine, RemQueryParser
from p8.services.database.rem import RemMixin

from p8.settings import Settings


class Database(PoolMixin, RemMixin):
    """AsyncPG connection pool + REM function wrappers + query engine.

    Combines pool lifecycle (PoolMixin) and REM query methods (RemMixin)
    into a single class. Also exposes ``rem_query()`` for text-based
    REM dialect queries.
    """

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self._query_engine: RemQueryEngine | None = None

    @property
    def query_engine(self) -> RemQueryEngine:
        if self._query_engine is None:
            self._query_engine = RemQueryEngine(self, self.settings)
        return self._query_engine

    async def rem_query(
        self,
        query_string: str,
        *,
        tenant_id: str | None = None,
        user_id: UUID | None = None,
    ) -> list[dict]:
        """Parse and execute a REM dialect query string.

        Parameters
        ----------
        query_string : str
            REM dialect query, e.g. ``LOOKUP "sarah-chen"`` or
            ``SEARCH "topic" FROM schemas LIMIT 5``.
        tenant_id : str, optional
            Tenant isolation filter.
        user_id : str, optional
            User isolation filter.
        """
        return await self.query_engine.execute(
            query_string, tenant_id=tenant_id, user_id=user_id
        )


__all__ = [
    "Database",
    "RemQuery",
    "RemQueryEngine",
    "RemQueryParser",
]
