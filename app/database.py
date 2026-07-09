"""
Thin async connection-pool wrapper around asyncpg, tuned to survive on
Supabase's free tier (60 max connections) while being called from many
short-lived Vercel serverless function invocations.

Key ideas:
- Pool is created lazily, once per warm serverless instance (module-level
  singleton). Cold starts pay the connection cost once; warm invocations
  reuse it.
- max_size is small (default 5) because EVERY concurrent serverless
  instance gets its own pool - 5 * N instances must stay under Supabase's
  connection ceiling, hence we also require the *pooler* (pgbouncer)
  connection string, not a direct connection.
- max_inactive_connection_lifetime aggressively closes idle connections
  so a burst of traffic doesn't leave connections dangling.
- statement_cache_size=0 is REQUIRED when talking to Supabase's pgbouncer
  in "transaction" pooling mode (it doesn't support prepared statements).
"""

import asyncpg
import json
from contextlib import asynccontextmanager
from app.config import get_settings

settings = get_settings()

_pool: asyncpg.Pool | None = None


async def _init_connection(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec(
        "json",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )
    await conn.set_type_codec(
        "jsonb",
        encoder=json.dumps,
        decoder=json.loads,
        schema="pg_catalog",
    )


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None or _pool._closed:
        if not settings.DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL is not set. Put your Supabase pooler connection "
                "string in .env (see README)."
            )
        _pool = await asyncpg.create_pool(
            dsn=settings.DATABASE_URL,
            min_size=settings.DB_POOL_MIN_SIZE,
            max_size=settings.DB_POOL_MAX_SIZE,
            max_inactive_connection_lifetime=settings.DB_POOL_MAX_INACTIVE_CONN_LIFETIME,
            command_timeout=settings.DB_COMMAND_TIMEOUT,
            statement_cache_size=0,  # required for pgbouncer transaction mode
            init=_init_connection,
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None and not _pool._closed:
        await _pool.close()
        _pool = None


@asynccontextmanager
async def acquire():
    """Usage: `async with acquire() as conn: await conn.fetch(...)`"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn
