from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.config import get_settings
from app.database import acquire
from app.rate_limit import limiter
from app.schemas import (
    AttemptCreate,
    AttemptResult,
    BreakdownAnswerCreate,
    BreakdownAnswerResult,
    ChapterOut,
    PageMeta,
    SubjectBreakdownSlideOut,
    SubjectOut,
    SubjectQuestionListOut,
    SubjectQuestionOut,
)
from app.security import AuthContext
from app.subscriptions import require_content_access

router = APIRouter(prefix="/api/subjects", tags=["subjects"])
settings = get_settings()


def _require_visible(subject_key: str) -> None:
    if not settings.is_subject_visible(subject_key):
        raise HTTPException(404, "Subject not yet available")


async def _question_subject(conn, question_id: str):
    return await conn.fetchrow(
        """
        select q.id, q.correct_option, q.explanation, c.subject_key
        from subject_questions q
        join subject_chapters c on c.id = q.chapter_id
        where q.id=$1
        """,
        question_id,
    )


async def _slide_subject(conn, slide_id: str):
    return await conn.fetchrow(
        """
        select s.practice_correct_option, s.practice_explanation, c.subject_key
        from subject_breakdown_slides s
        join subject_questions q on q.id = s.question_id
        join subject_chapters c on c.id = q.chapter_id
        where s.id=$1
        """,
        slide_id,
    )


def _question_out(row) -> SubjectQuestionOut:
    return SubjectQuestionOut(
        id=str(row["id"]),
        chapter_id=str(row["chapter_id"]),
        question_text=row["question_text"],
        options=row["options"],
        difficulty=row["difficulty"],
        format=row["format"],
    )


@router.get("", response_model=list[SubjectOut])
@limiter.limit(settings.RATE_LIMIT_DEFAULT)
async def list_subjects(request: Request):
    return [
        SubjectOut(key=key, name=meta["name"], visible=settings.is_subject_visible(key))
        for key, meta in settings.SUBJECTS.items()
    ]


@router.get("/{subject_key}/chapters", response_model=list[ChapterOut])
@limiter.limit(settings.RATE_LIMIT_DEFAULT)
async def list_chapters(
    request: Request,
    subject_key: str,
    current: AuthContext = Depends(require_content_access),
):
    _require_visible(subject_key)
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            select c.id, c.subject_key, c.name, c.chapter_order,
                   count(q.id)::int as question_count
            from subject_chapters c
            left join subject_questions q on q.chapter_id = c.id
            where c.subject_key=$1
            group by c.id
            order by c.chapter_order, c.created_at, c.id
            """,
            subject_key,
        )
    return [
        ChapterOut(
            id=str(row["id"]), subject_key=row["subject_key"], name=row["name"],
            chapter_order=row["chapter_order"], question_count=row["question_count"],
        )
        for row in rows
    ]


@router.get("/{subject_key}/chapters/{chapter_id}/questions", response_model=SubjectQuestionListOut)
@limiter.limit(settings.RATE_LIMIT_DEFAULT)
async def list_questions(
    request: Request,
    subject_key: str,
    chapter_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=settings.MAX_PAGE_SIZE),
    current: AuthContext = Depends(require_content_access),
):
    _require_visible(subject_key)
    offset = (page - 1) * page_size
    async with acquire() as conn:
        chapter = await conn.fetchrow(
            "select id from subject_chapters where id=$1 and subject_key=$2",
            chapter_id,
            subject_key,
        )
        if chapter is None:
            raise HTTPException(404, "Chapter not found")
        total = await conn.fetchval(
            "select count(*) from subject_questions where chapter_id=$1", chapter_id
        )
        rows = await conn.fetch(
            """
            select id, chapter_id, question_text, options, difficulty, format
            from subject_questions
            where chapter_id=$1
            order by created_at, id
            limit $2 offset $3
            """,
            chapter_id,
            page_size,
            offset,
        )
    total_pages = (total + page_size - 1) // page_size if total else 0
    return SubjectQuestionListOut(
        items=[_question_out(row) for row in rows],
        meta=PageMeta(
            page=page, page_size=page_size, total_items=total, total_pages=total_pages
        ),
    )


@router.get(
    "/{subject_key}/chapters/{chapter_id}/questions/{question_id}",
    response_model=SubjectQuestionOut,
)
@limiter.limit(settings.RATE_LIMIT_DEFAULT)
async def get_question(
    request: Request,
    subject_key: str,
    chapter_id: str,
    question_id: str,
    current: AuthContext = Depends(require_content_access),
):
    _require_visible(subject_key)
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            select q.id, q.chapter_id, q.question_text, q.options, q.difficulty, q.format
            from subject_questions q
            join subject_chapters c on c.id = q.chapter_id
            where q.id=$1 and q.chapter_id=$2 and c.subject_key=$3
            """,
            question_id,
            chapter_id,
            subject_key,
        )
    if row is None:
        raise HTTPException(404, "Question not found")
    return _question_out(row)


@router.post("/attempts", response_model=AttemptResult)
@limiter.limit(settings.RATE_LIMIT_SUBMIT)
async def submit_attempt(
    request: Request,
    body: AttemptCreate,
    current: AuthContext = Depends(require_content_access),
):
    if body.student_id != current.student_id:
        raise HTTPException(403, "Cannot submit attempts for another account")
    async with acquire() as conn:
        question = await _question_subject(conn, body.question_id)
        if question is None:
            raise HTTPException(404, "Question not found")
        _require_visible(question["subject_key"])
        is_correct = body.selected_option.strip().upper() == question["correct_option"].strip().upper()
        await conn.execute(
            """
            insert into student_subject_attempts
              (student_id, question_id, selected_option, is_correct, attempt_number,
               went_through_breakdown)
            values ($1,$2,$3,$4,$5,$6)
            """,
            current.student_id,
            body.question_id,
            body.selected_option,
            is_correct,
            body.attempt_number,
            body.went_through_breakdown,
        )
        breakdown_available = False
        if not is_correct:
            breakdown_available = bool(
                await conn.fetchval(
                    "select exists(select 1 from subject_breakdown_slides where question_id=$1)",
                    body.question_id,
                )
            )
    reveal = is_correct or body.attempt_number >= 2
    return AttemptResult(
        is_correct=is_correct,
        correct_option=question["correct_option"] if reveal else None,
        explanation=question["explanation"] if reveal else None,
        breakdown_available=breakdown_available,
    )


@router.get("/breakdown/{question_id}", response_model=list[SubjectBreakdownSlideOut])
@limiter.limit(settings.RATE_LIMIT_DEFAULT)
async def get_breakdown(
    request: Request,
    question_id: str,
    current: AuthContext = Depends(require_content_access),
):
    async with acquire() as conn:
        question = await _question_subject(conn, question_id)
        if question is None:
            raise HTTPException(404, "Question not found")
        _require_visible(question["subject_key"])
        rows = await conn.fetch(
            """
            select id, slide_order, slide_type, concept, content,
                   practice_question, practice_options
            from subject_breakdown_slides
            where question_id=$1
            order by slide_order
            """,
            question_id,
        )
    if not rows:
        raise HTTPException(404, "No breakdown available for this question yet")
    return [
        SubjectBreakdownSlideOut(
            id=str(row["id"]), slide_order=row["slide_order"],
            slide_type=row["slide_type"], concept=row["concept"], content=row["content"],
            practice_question=row["practice_question"], practice_options=row["practice_options"],
        )
        for row in rows
    ]


@router.post("/breakdown-answers", response_model=BreakdownAnswerResult)
@limiter.limit(settings.RATE_LIMIT_SUBMIT)
async def submit_breakdown_answer(
    request: Request,
    body: BreakdownAnswerCreate,
    current: AuthContext = Depends(require_content_access),
):
    if body.student_id != current.student_id:
        raise HTTPException(403, "Cannot submit breakdown answers for another account")
    async with acquire() as conn:
        slide = await _slide_subject(conn, body.slide_id)
        if slide is None or slide["practice_correct_option"] is None:
            raise HTTPException(404, "Practice slide not found")
        _require_visible(slide["subject_key"])
        is_correct = body.selected_option.strip().upper() == slide["practice_correct_option"].strip().upper()
        await conn.execute(
            """
            insert into student_subject_breakdown_answers
              (student_id, slide_id, selected_option, is_correct)
            values ($1,$2,$3,$4)
            """,
            current.student_id,
            body.slide_id,
            body.selected_option,
            is_correct,
        )
    return BreakdownAnswerResult(
        is_correct=is_correct,
        correct_option=slide["practice_correct_option"],
        explanation=slide["practice_explanation"],
    )
