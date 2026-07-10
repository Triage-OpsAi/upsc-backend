"""
Thin async connection-pool wrapper around asyncpg, tuned to survive on
Supabase's free tier (60 max connections) while being called from many
short-lived Vercel serverless function invocations.

Key ideas:
- Pool is created lazily, once per warm serverless instance (module-level
  singleton). Cold starts pay the connection cost once; warm invocations
  reuse it.
- max_size is one by default because EVERY concurrent serverless
  instance gets its own pool - N instances must stay under Supabase's
  connection ceiling, hence we also require the *pooler* (pgbouncer)
  connection string, not a direct connection.
- max_inactive_connection_lifetime aggressively closes idle connections
  so a burst of traffic doesn't leave connections dangling.
- statement_cache_size=0 is REQUIRED when talking to Supabase's pgbouncer
  in "transaction" pooling mode (it doesn't support prepared statements).
"""

import asyncio
import logging
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import json
from contextlib import asynccontextmanager
from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None


class DatabaseUnavailable(RuntimeError):
    """Raised when the database pool cannot provide a connection after retries."""


def _transaction_pooler_dsn(dsn: str) -> str:
    """Use Supabase transaction pooling when a session-pooler URL was supplied."""
    parsed = urlsplit(dsn)
    hostname = parsed.hostname or ""
    if parsed.port != 5432 or not hostname.endswith(".pooler.supabase.com"):
        return dsn

    credentials = parsed.netloc.rsplit("@", 1)[0] if "@" in parsed.netloc else ""
    host = f"[{hostname}]" if ":" in hostname else hostname
    netloc = f"{credentials}@{host}:6543" if credentials else f"{host}:6543"
    logger.warning("Supabase session-pooler port 5432 detected; using transaction-pooler port 6543")
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


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
            dsn=_transaction_pooler_dsn(settings.DATABASE_URL),
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


async def _discard_pool() -> None:
    """Drop a poisoned/exhausted pool without holding up the next retry."""
    global _pool
    pool, _pool = _pool, None
    if pool is not None and not pool._closed:
        try:
            await asyncio.wait_for(pool.close(), timeout=2)
        except Exception:
            pool.terminate()


def _is_connection_capacity_error(error: Exception) -> bool:
    text = str(error).lower()
    return isinstance(error, (asyncpg.TooManyConnectionsError, asyncpg.CannotConnectNowError)) or any(
        marker in text
        for marker in ("emaxconnsession", "max clients reached", "too many connections", "connection was closed")
    )


@asynccontextmanager
async def acquire():
    """Usage: `async with acquire() as conn: await conn.fetch(...)`"""
    last_error: Exception | None = None
    pool: asyncpg.Pool | None = None
    conn: asyncpg.Connection | None = None
    for attempt in range(max(1, settings.DB_ACQUIRE_RETRIES)):
        try:
            pool = await get_pool()
            conn = await pool.acquire(timeout=settings.DB_COMMAND_TIMEOUT)
            break
        except Exception as error:
            if not _is_connection_capacity_error(error):
                raise
            last_error = error
            logger.warning(
                "Database connection unavailable (attempt %s/%s): %s",
                attempt + 1,
                settings.DB_ACQUIRE_RETRIES,
                error,
            )
            await _discard_pool()
            if attempt + 1 < settings.DB_ACQUIRE_RETRIES:
                await asyncio.sleep(0.2 * (attempt + 1))

    if conn is None or pool is None:
        raise DatabaseUnavailable("Database is temporarily at capacity") from last_error

    try:
        yield conn
    finally:
        try:
            await pool.release(conn)
        except Exception:
            await _discard_pool()
