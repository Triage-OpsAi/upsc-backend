"""Read-only audit of trial and founder-offer entitlement data."""

import asyncio

from app.database import close_pool, get_pool


async def main() -> None:
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                select count(*)::int as accounts,
                       count(*) filter (where trial_ends_at > now())::int as trials_with_access,
                       count(*) filter (where subscription_status='active')::int as active_subscriptions,
                       count(*) filter (where early_offer_number between 1 and 500)::int as founder_accounts,
                       min(trial_ends_at) as earliest_trial_end,
                       max(trial_ends_at) as latest_trial_end
                from students
                """
            )
    finally:
        await close_pool()
    print(dict(row), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
