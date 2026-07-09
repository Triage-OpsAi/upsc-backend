"""
Shared persistence logic used by BOTH the one-time bulk seed script
(Jan 2025 - Jul 2026) and the daily midnight IST cron (Jul 10 2026 onward).

Idempotent: `ca_topics` has a unique (year, month, title) constraint, so
re-running for a month that's already populated just skips duplicates.
"""

import asyncpg
import logging
import re
from datetime import date, datetime
from app.openai_client import generate_topics_and_questions, generate_breakdown

logger = logging.getLogger(__name__)
MAX_BULK_TOPICS_PER_MONTH = 30


async def seed_month(conn: asyncpg.Connection, month: int, year: int, count: int = MAX_BULK_TOPICS_PER_MONTH) -> int:
    """Generate + store `count` topics (with questions + breakdowns) for one month."""
    requested_count = min(max(0, count), MAX_BULK_TOPICS_PER_MONTH)
    topics = await generate_topics_and_questions(month, year, count=requested_count)
    created = 0
    for t in topics:
        try:
            created += await _store_topic(conn, month, year, t)
        except Exception as exc:
            logger.warning(
                "Skipping generated topic for %04d-%02d after store failure: %s",
                year,
                month,
                exc,
                exc_info=True,
            )
    return created


async def store_researched_topics(conn: asyncpg.Connection, month: int, year: int, topics: list[dict]) -> int:
    """Same as seed_month but topics were already generated (daily cron path)."""
    created = 0
    for t in topics:
        try:
            created += await _store_topic(conn, month, year, t)
        except Exception as exc:
            logger.warning(
                "Skipping researched topic for %04d-%02d after store failure: %s",
                year,
                month,
                exc,
                exc_info=True,
            )
    return created


async def _store_topic(conn: asyncpg.Connection, month: int, year: int, t: dict) -> int:
    q = _normalize_question(t.get("question"))
    if not q:
        logger.warning("Skipping generated topic without a valid question for %04d-%02d: %s", year, month, t.get("title"))
        return 0
    source_date = _parse_source_date(t.get("source_date"), month, year)
    async with conn.transaction():
        topic_row = await conn.fetchrow(
            """
            insert into ca_topics (month, year, title, summary, subject_tags, source_date, status)
            values ($1,$2,$3,$4,$5,$6,'published')
            on conflict (year, month, title) do nothing
            returning id
            """,
            month, year, t["title"], t.get("summary"), t.get("subject_tags", []), source_date,
        )
        if topic_row is None:
            return 0  # already exists
        topic_id = topic_row["id"]

        question_row = await conn.fetchrow(
            """
            insert into ca_questions (topic_id, question_text, options, correct_option, explanation)
            values ($1,$2,$3,$4,$5)
            returning id
            """,
            topic_id, q["question_text"], q["options"], q["correct_option"], q.get("explanation"),
        )
        question_id = question_row["id"]

        # Generate the 6-slide breakdown up front so every student who gets
        # it wrong sees the SAME pre-generated breakdown (no per-student cost).
        slides = await generate_breakdown(
            q["question_text"], q["correct_option"], q.get("explanation", ""), t.get("subject_tags", []),
        )
        for s in slides:
            await conn.execute(
                """
                insert into breakdown_slides
                  (question_id, slide_order, slide_type, subject, content,
                   practice_question, practice_options, practice_correct_option, practice_explanation)
                values ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                on conflict (question_id, slide_order) do nothing
                """,
                question_id, s["slide_order"], s["slide_type"], s["subject"], s.get("content"),
                s.get("practice_question"), s.get("practice_options"),
                s.get("practice_correct_option"), s.get("practice_explanation"),
            )
    return 1


def _normalize_question(question) -> dict | None:
    if not isinstance(question, dict):
        return None

    question_text = question.get("question_text") or question.get("question") or question.get("text")
    options = question.get("options")
    correct_option = (
        question.get("correct_option")
        or question.get("correctOption")
        or question.get("correct_answer")
        or question.get("correctAnswer")
        or question.get("answer_key")
        or question.get("answer")
    )

    if isinstance(correct_option, dict):
        correct_option = correct_option.get("key") or correct_option.get("option") or correct_option.get("answer")

    correct_option = _normalize_option_key(correct_option, options)
    if not question_text or not isinstance(options, list) or correct_option not in {"A", "B", "C", "D"}:
        return None

    return {
        "question_text": question_text,
        "options": options,
        "correct_option": correct_option,
        "explanation": question.get("explanation"),
    }


def _normalize_option_key(value, options) -> str | None:
    if value is None:
        return None

    value_text = str(value).strip()
    upper_value = value_text.upper()
    if upper_value in {"A", "B", "C", "D"}:
        return upper_value

    for option in options or []:
        if not isinstance(option, dict):
            continue
        key = str(option.get("key", "")).strip().upper()
        text = str(option.get("text", "")).strip()
        if key in {"A", "B", "C", "D"} and text and text.casefold() == value_text.casefold():
            return key

    match = re.search(r"\b([ABCD])\b", upper_value)
    if match:
        return match.group(1)
    return None


def _parse_source_date(value, month: int, year: int) -> date | None:
    if isinstance(value, datetime):
        parsed = value.date()
    elif value is None or isinstance(value, date):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None

    if parsed and (parsed.month != month or parsed.year != year):
        return None
    return parsed
