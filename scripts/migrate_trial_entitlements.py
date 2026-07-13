"""Apply the idempotent seven-day trial and founder-offer migration."""

import asyncio
from pathlib import Path

from app.database import close_pool, get_pool


async def main() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "db"
        / "migrations"
        / "20260714_add_trial_entitlements.sql"
    ).read_text(encoding="utf-8")
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(migration)
    finally:
        await close_pool()
    print("Trial entitlement migration applied.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
