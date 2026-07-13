"""Safely regenerate a contiguous month range with fully prepared content."""

import argparse
import asyncio

from app.database import close_pool, get_pool
from app.services.content_generator import prepare_month_replacement, replace_month


def _parse_month(value: str) -> tuple[int, int]:
    year, month = map(int, value.split("-"))
    if not 1 <= month <= 12:
        raise argparse.ArgumentTypeError("month must be YYYY-MM")
    return year, month


def _month_range(start: tuple[int, int], end: tuple[int, int]):
    year, month = start
    while (year, month) <= end:
        yield year, month
        month += 1
        if month == 13:
            year += 1
            month = 1


async def main(start: tuple[int, int], end: tuple[int, int], count: int) -> None:
    if start > end:
        raise ValueError("--from must not be after --to")
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            for year, month in _month_range(start, end):
                label = f"{year:04d}-{month:02d}"
                print(f"Preparing {label} ({count} questions and breakdowns) ...", flush=True)
                prepared = await prepare_month_replacement(month, year, count=count)
                print(f"Replacing {label} atomically ...", flush=True)
                replaced = await replace_month(
                    conn,
                    month,
                    year,
                    prepared,
                )
                print(f"  -> {replaced} topics replaced", flush=True)
    finally:
        await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--from", dest="start", type=_parse_month, required=True, help="first month, YYYY-MM")
    parser.add_argument("--to", dest="end", type=_parse_month, required=True, help="last month, YYYY-MM")
    parser.add_argument("--count", type=int, default=30, help="questions per month, capped at 30")
    args = parser.parse_args()
    asyncio.run(main(args.start, args.end, args.count))
