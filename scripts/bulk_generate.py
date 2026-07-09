"""
Run ONCE to populate all current affairs from Jan 2025 through Jul 2026.
From Jul 10 2026 onward, the /api/cron/daily-content endpoint (Vercel Cron,
midnight IST daily) takes over automatically.

Usage:
    cd backend
    python -m scripts.bulk_generate                 # all months, capped at 30 topics/month
    python -m scripts.bulk_generate --count 12       # 12 topics/month
    python -m scripts.bulk_generate --only 2025-03   # just one month (re-run/backfill)

This is idempotent: ca_topics has a unique (year, month, title) constraint,
so re-running skips anything already generated.
"""

import asyncio
import argparse
import logging
import sys
from app.database import get_pool, close_pool
from app.services.content_generator import seed_month
from app.config import get_settings

settings = get_settings()
logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")


def month_range():
    y, m = settings.BULK_SEED_START_YEAR, settings.BULK_SEED_START_MONTH
    end_y, end_m = settings.BULK_SEED_END_YEAR, settings.BULK_SEED_END_MONTH
    while (y, m) <= (end_y, end_m):
        yield y, m
        m += 1
        if m > 12:
            m = 1
            y += 1


async def main(count: int, only: str | None):
    pool = await get_pool()
    summary: list[tuple[int, int, int]] = []
    try:
        months = list(month_range())
        if only:
            oy, om = map(int, only.split("-"))
            months = [(oy, om)]

        async with pool.acquire() as conn:
            for y, m in months:
                print(f"Generating {y}-{m:02d} ...", flush=True)
                try:
                    created = await seed_month(conn, m, y, count=count)
                    print(f"  -> {created} new topics stored", flush=True)
                except Exception as exc:
                    created = 0
                    print(f"MONTH FAILED: {y}-{m:02d}: {exc}", file=sys.stderr, flush=True)
                summary.append((y, m, created))
    finally:
        await close_pool()

    print(flush=True)
    print("month | topics_created", flush=True)
    print("------|---------------", flush=True)
    for y, m, created in summary:
        print(f"{y}-{m:02d} | {created}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=500, help="topics per month, capped at 30")
    parser.add_argument("--only", type=str, default=None, help="YYYY-MM to backfill a single month")
    args = parser.parse_args()
    asyncio.run(main(args.count, args.only))
