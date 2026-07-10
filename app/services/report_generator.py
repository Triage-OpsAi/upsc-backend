"""
Runs once a night (triggered by the /api/cron/daily-report endpoint, which
Vercel Cron hits at 1am IST). For every student who was active that day:
  1. Compute today's accuracy + per-subject breakdown via SQL.
  2. Compute percentile vs all other active students that day.
  3. Ask the cheap model for a short personalised feedback paragraph.
  4. Upsert into daily_reports.
"""

import asyncpg
from datetime import date


EXAM_SUBJECT_WEIGHTS = {
    # crude illustrative weighting per exam - tune freely
    "UPSC": {"polity": 1.2, "economy": 1.2, "history": 1.1, "geography": 1.0, "environment": 1.1, "science_tech": 1.0, "ethics": 1.3, "international_relations": 1.1, "schemes": 1.0},
    "SSC": {"polity": 1.0, "economy": 1.0, "history": 1.0, "geography": 1.0, "environment": 0.9, "science_tech": 1.1, "ethics": 0.7, "international_relations": 0.8, "schemes": 1.0},
    "STATE_PSC": {"polity": 1.1, "economy": 1.0, "history": 1.2, "geography": 1.2, "environment": 1.0, "science_tech": 0.9, "ethics": 1.0, "international_relations": 0.8, "schemes": 1.1},
}


async def generate_reports_for_date(conn: asyncpg.Connection, report_date: date) -> int:
    students = await conn.fetch(
        """
        select distinct s.id, s.target_exam
        from students s
        join student_attempts a on a.student_id = s.id
        where a.created_at::date = $1
        """,
        report_date,
    )

    count = 0
    for s in students:
        stats = await _compute_stats(conn, s["id"], report_date)
        if stats["total_attempted"] == 0:
            continue
        percentile = await _compute_percentile(conn, report_date, stats["accuracy"])
        readiness = _exam_readiness(stats["subject_breakdown"])
        from app.openai_client import generate_report_feedback

        feedback = await generate_report_feedback({
            "target_exam": s["target_exam"],
            **stats,
            "percentile": percentile,
        })
        await conn.execute(
            """
            insert into daily_reports
              (student_id, report_date, total_attempted, total_correct, accuracy,
               percentile, subject_breakdown, exam_wise_readiness, ai_feedback)
            values ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            on conflict (student_id, report_date) do update set
              total_attempted = excluded.total_attempted,
              total_correct = excluded.total_correct,
              accuracy = excluded.accuracy,
              percentile = excluded.percentile,
              subject_breakdown = excluded.subject_breakdown,
              exam_wise_readiness = excluded.exam_wise_readiness,
              ai_feedback = excluded.ai_feedback
            """,
            s["id"], report_date, stats["total_attempted"], stats["total_correct"],
            stats["accuracy"], percentile, stats["subject_breakdown"], readiness, feedback,
        )
        count += 1
    return count


async def _compute_stats(conn: asyncpg.Connection, student_id, report_date: date) -> dict:
    row = await conn.fetchrow(
        """
        select count(*) as total, count(*) filter (where is_correct) as correct
        from student_attempts
        where student_id=$1 and created_at::date=$2 and attempt_number=1
        """,
        student_id, report_date,
    )
    total, correct = row["total"], row["correct"]
    accuracy = round(100.0 * correct / total, 2) if total else 0.0

    subject_rows = await conn.fetch(
        """
        select unnest(t.subject_tags) as subject,
               count(*) as total,
               count(*) filter (where a.is_correct) as correct
        from student_attempts a
        join ca_questions q on q.id = a.question_id
        join ca_topics t on t.id = q.topic_id
        where a.student_id=$1 and a.created_at::date=$2 and a.attempt_number=1
        group by subject
        """,
        student_id, report_date,
    )
    subject_breakdown = {r["subject"]: {"total": r["total"], "correct": r["correct"]} for r in subject_rows}

    return {
        "total_attempted": total,
        "total_correct": correct,
        "accuracy": accuracy,
        "subject_breakdown": subject_breakdown,
    }


async def _compute_percentile(conn: asyncpg.Connection, report_date: date, my_accuracy: float) -> float:
    row = await conn.fetchrow(
        """
        with daily as (
          select student_id,
                 100.0 * count(*) filter (where is_correct) / nullif(count(*),0) as acc
          from student_attempts
          where created_at::date=$1 and attempt_number=1
          group by student_id
        )
        select count(*) filter (where acc <= $2)::float / nullif(count(*),0)::float * 100 as pct
        from daily
        """,
        report_date, my_accuracy,
    )
    return round(float(row["pct"] or 0), 1)


def _exam_readiness(subject_breakdown: dict) -> dict:
    result = {}
    for exam, weights in EXAM_SUBJECT_WEIGHTS.items():
        total_w = 0.0
        score_w = 0.0
        for subject, perf in subject_breakdown.items():
            w = weights.get(subject, 1.0)
            if perf["total"] == 0:
                continue
            total_w += w
            score_w += w * (perf["correct"] / perf["total"])
        result[exam] = round(100 * score_w / total_w, 1) if total_w else 0.0
    return result
