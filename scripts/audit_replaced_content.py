import argparse
import asyncio

from app.database import close_pool, get_pool
from app.services.report_generator import build_report_for_student


async def main(start_month: int, end_month: int) -> None:
    pool = await get_pool()
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                select t.month,
                       count(distinct t.id) as topics,
                       count(distinct q.id) as questions,
                       count(distinct s.id) as slides,
                       count(distinct q.id) filter (
                         where q.question_text ilike '%Assertion (A)%'
                           and q.question_text ilike '%Reason (R)%'
                       ) as assertion_reason,
                       count(distinct q.id) filter (
                         where q.question_text not ilike '%Assertion (A)%'
                           and q.question_text ilike '%NOT%'
                       ) as negative,
                       count(distinct q.id) filter (
                         where q.question_text ilike '%How many%'
                           and q.question_text not ilike '%Assertion (A)%'
                           and q.question_text not ilike '%NOT%'
                       ) as matching,
                       count(distinct q.id) filter (
                         where q.question_text not ilike '%Assertion (A)%'
                           and q.question_text not ilike '%NOT%'
                           and q.question_text not ilike '%How many%'
                       ) as statement
                from ca_topics t
                join ca_questions q on q.topic_id = t.id
                join breakdown_slides s on s.question_id = q.id
                where t.year = 2026 and t.month between $1 and $2
                group by t.month
                order by t.month
                """,
                start_month,
                end_month,
            )
            invalid_breakdowns = await conn.fetchval(
                """
                with slide_counts as (
                  select q.id,
                         count(s.id) as slide_count,
                         bool_or(s.slide_order = 1 and s.content ilike '%Precision hinge:%') as has_hinge
                  from ca_topics t
                  join ca_questions q on q.topic_id = t.id
                  left join breakdown_slides s on s.question_id = q.id
                  where t.year = 2026 and t.month between $1 and $2
                  group by q.id
                )
                select count(*) from slide_counts
                where slide_count <> 6 or not has_hinge
                """,
                start_month,
                end_month,
            )
            history = await conn.fetchrow(
                """
                select count(*) as attempts,
                       count(*) filter (where content_changed) as changed_attempts,
                       count(*) filter (where question_id is null) as detached_attempts
                from student_attempts
                """
            )
            breakdown_history = await conn.fetchrow(
                """
                select count(*) as answers,
                       count(*) filter (where content_changed) as changed_answers,
                       count(*) filter (where slide_id is null) as detached_answers
                from student_breakdown_answers
                """
            )
            changed_report_key = await conn.fetchrow(
                """
                select student_id, (created_at at time zone 'Asia/Kolkata')::date as report_date
                from student_attempts
                where content_changed and attempt_number = 1
                order by created_at
                limit 1
                """
            )
            for row in rows:
                print(dict(row))
            print({"invalid_breakdowns": invalid_breakdowns})
            print(dict(history))
            print(dict(breakdown_history))
            if changed_report_key:
                changed_report = await build_report_for_student(
                    conn,
                    changed_report_key["student_id"],
                    changed_report_key["report_date"],
                )
                print({
                    "changed_report_date": changed_report_key["report_date"],
                    "content_changed": changed_report["content_changed"],
                    "notice": changed_report["content_change_notice"],
                    "attempted": changed_report["total_attempted"],
                    "correct": changed_report["total_correct"],
                })
    finally:
        await close_pool()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-month", type=int, default=1)
    parser.add_argument("--to-month", type=int, default=7)
    args = parser.parse_args()
    asyncio.run(main(args.from_month, args.to_month))
