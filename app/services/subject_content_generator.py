"""Generation and persistence for permanent chapter-based subject content."""

import asyncio
import logging

import asyncpg

logger = logging.getLogger(__name__)
BREAKDOWN_CONCURRENCY = 3


async def store_subject_chapter_questions(
    conn: asyncpg.Connection,
    chapter_id,
    subject_key: str,
    chapter_name: str,
    count: int,
) -> int:
    """Generate and atomically store each question with its four-slide breakdown."""
    from app.openai_client import generate_subject_breakdown, generate_subject_questions

    requested = max(0, count)
    if requested == 0:
        return 0
    existing_rows = await conn.fetch(
        """
        select question_text
        from subject_questions
        where chapter_id=$1
        order by created_at, id
        """,
        chapter_id,
    )
    existing_texts = [row["question_text"] for row in existing_rows]
    generated = await generate_subject_questions(
        subject=subject_key,
        chapter=chapter_name,
        count=requested,
        existing_titles=existing_texts,
    )

    existing_keys = {" ".join(text.casefold().split()) for text in existing_texts}
    semaphore = asyncio.Semaphore(BREAKDOWN_CONCURRENCY)

    async def prepare(question: dict):
        try:
            key = " ".join(question["question_text"].casefold().split())
            if key in existing_keys:
                return None
            async with semaphore:
                slides = await generate_subject_breakdown(
                    question_text=question["question_text"],
                    correct_option=question["correct_option"],
                    explanation=question["explanation"],
                    subject=subject_key,
                    chapter=chapter_name,
                )
            return question, slides
        except Exception as exc:
            logger.warning(
                "Skipping one static question for %s / %s after breakdown failure: %s",
                subject_key,
                chapter_name,
                exc,
                exc_info=True,
            )
            return None

    preparation_tasks = [asyncio.create_task(prepare(question)) for question in generated]
    created = 0
    for completed in asyncio.as_completed(preparation_tasks):
        item = await completed
        if item is None:
            continue
        question, slides = item
        try:
            async with conn.transaction():
                question_id = await conn.fetchval(
                    """
                    insert into subject_questions
                      (chapter_id, question_text, options, correct_option, explanation,
                       difficulty, format)
                    values ($1,$2,$3,$4,$5,'very_hard',$6)
                    returning id
                    """,
                    chapter_id,
                    question["question_text"],
                    question["options"],
                    question["correct_option"],
                    question["explanation"],
                    question["format"],
                )
                for slide in slides:
                    await conn.execute(
                        """
                        insert into subject_breakdown_slides
                          (question_id, slide_order, slide_type, concept, content,
                           practice_question, practice_options, practice_correct_option,
                           practice_explanation)
                        values ($1,$2,$3,$4,$5,$6,$7,$8,$9)
                        """,
                        question_id,
                        slide["slide_order"],
                        slide["slide_type"],
                        slide["concept"],
                        slide.get("content"),
                        slide.get("practice_question"),
                        slide.get("practice_options"),
                        slide.get("practice_correct_option"),
                        slide.get("practice_explanation"),
                    )
            created += 1
        except Exception as exc:
            logger.warning(
                "Skipping one static question for %s / %s after store failure: %s",
                subject_key,
                chapter_name,
                exc,
                exc_info=True,
            )
    return created
