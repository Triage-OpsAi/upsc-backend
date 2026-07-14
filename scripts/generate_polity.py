"""Resumable generator for permanent Polity chapter questions."""

import argparse
import asyncio
import logging

from app.database import close_pool, get_pool
from app.services.subject_content_generator import store_subject_chapter_questions

DEFAULT_CHAPTER = "Constitutional Framework"
DEFAULT_TARGET = 200
BATCH_SIZE = 15

logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")


async def main(target: int, chapter: str, reset_unattempted: bool = False) -> None:
    target = max(0, target)
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            chapter_id = await conn.fetchval(
                """
                insert into subject_chapters (subject_key, name, chapter_order)
                values ('polity',$1,1)
                on conflict (subject_key, name)
                do update set name=excluded.name
                returning id
                """,
                chapter,
            )
            if reset_unattempted:
                attempts = await conn.fetchval(
                    """
                    select count(*)
                    from student_subject_attempts a
                    join subject_questions q on q.id = a.question_id
                    where q.chapter_id=$1
                    """,
                    chapter_id,
                )
                if attempts:
                    raise RuntimeError(
                        f"Refusing to reset {attempts} attempted subject-question records."
                    )
                deleted = await conn.fetchval(
                    """
                    with deleted as (
                      delete from subject_questions where chapter_id=$1 returning id
                    )
                    select count(*) from deleted
                    """,
                    chapter_id,
                )
                print(f"Reset {deleted} unattempted questions for {chapter}.", flush=True)
            stalled_batches = 0
            while True:
                current = await conn.fetchval(
                    "select count(*) from subject_questions where chapter_id=$1",
                    chapter_id,
                )
                print(f"{chapter}: {current}/{target} questions stored", flush=True)
                if current >= target:
                    break
                requested = min(BATCH_SIZE, target - current)
                created = await store_subject_chapter_questions(
                    conn,
                    chapter_id,
                    "polity",
                    chapter,
                    requested,
                )
                stalled_batches = stalled_batches + 1 if created == 0 else 0
                print(f"  -> {created} new questions stored", flush=True)
                if stalled_batches >= 3:
                    raise RuntimeError(
                        "Generation made no progress for three consecutive batches; "
                        "fix the reported validation/model errors and rerun safely."
                    )
            final_count = await conn.fetchval(
                "select count(*) from subject_questions where chapter_id=$1",
                chapter_id,
            )
            print(f"{chapter}: {final_count}/{target} questions stored", flush=True)
    finally:
        await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET)
    parser.add_argument("--chapter", default=DEFAULT_CHAPTER)
    parser.add_argument(
        "--reset-unattempted",
        action="store_true",
        help="delete this chapter only when none of its questions has student attempts",
    )
    args = parser.parse_args()
    asyncio.run(main(args.target, args.chapter, args.reset_unattempted))
