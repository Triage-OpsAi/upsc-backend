"""Read-only quality audit for generated static subject content."""

import argparse
import asyncio

from app.database import close_pool, get_pool


async def main(subject: str, chapter: str, sample_size: int) -> None:
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            chapter_row = await conn.fetchrow(
                "select id from subject_chapters where subject_key=$1 and name=$2",
                subject,
                chapter,
            )
            if chapter_row is None:
                print(f"No chapter found for {subject} / {chapter}")
                return
            chapter_id = chapter_row["id"]
            total = await conn.fetchval(
                "select count(*) from subject_questions where chapter_id=$1", chapter_id
            )
            formats = await conn.fetch(
                """
                select format, count(*)::int as count
                from subject_questions
                where chapter_id=$1
                group by format
                order by format
                """,
                chapter_id,
            )
            incomplete = await conn.fetchval(
                """
                select count(*)
                from subject_questions q
                where q.chapter_id=$1
                  and (select count(*) from subject_breakdown_slides s where s.question_id=q.id) <> 4
                """,
                chapter_id,
            )
            samples = await conn.fetch(
                """
                select q.format, q.question_text, q.options, q.correct_option, q.explanation
                from subject_questions q
                where q.chapter_id=$1
                order by random()
                limit $2
                """,
                chapter_id,
                max(0, sample_size),
            )
        print(f"{chapter}: {total} questions")
        print("Formats: " + ", ".join(f"{row['format']}={row['count']}" for row in formats))
        print(f"Questions without exactly 4 slides: {incomplete}")
        for index, row in enumerate(samples, 1):
            correct_text = next(
                (option["text"] for option in row["options"] if option.get("key") == row["correct_option"]),
                "<missing>",
            )
            print(f"\nSAMPLE {index} [{row['format']}] correct={row['correct_option']}: {correct_text}")
            print(row["question_text"])
            print("EXPLANATION:", row["explanation"])
    finally:
        await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--subject", default="polity")
    parser.add_argument("--chapter", default="Constitutional Framework")
    parser.add_argument("--sample", type=int, default=5)
    args = parser.parse_args()
    asyncio.run(main(args.subject, args.chapter, args.sample))
