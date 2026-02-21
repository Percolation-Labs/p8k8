"""AsyncPG connection pool + low-level query methods."""

from __future__ import annotations

import json

import asyncpg

from p8.settings import Settings


async def _init_connection(conn: asyncpg.Connection):
    """Set up JSON/JSONB codec so asyncpg returns dicts, not strings."""
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )
    await conn.set_type_codec(
        "json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


class PoolMixin:
    """Connection pool lifecycle and low-level fetch/execute methods."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.pool: asyncpg.Pool | None = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(
            self.settings.database_url,
            min_size=self.settings.db_pool_min,
            max_size=self.settings.db_pool_max,
            init=_init_connection,
        )

    async def close(self):
        if self.pool:
            await self.pool.close()

    async def fetch(self, query: str, *args):
        assert self.pool is not None, "Database not connected"
        return await self.pool.fetch(query, *args)

    async def fetchrow(self, query: str, *args):
        assert self.pool is not None, "Database not connected"
        return await self.pool.fetchrow(query, *args)

    async def fetchval(self, query: str, *args):
        assert self.pool is not None, "Database not connected"
        return await self.pool.fetchval(query, *args)

    async def execute(self, query: str, *args):
        assert self.pool is not None, "Database not connected"
        return await self.pool.execute(query, *args)
