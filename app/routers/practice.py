from fastapi import APIRouter, Depends, Request, HTTPException
from app.database import acquire
from app.schemas import (
    QuestionOut, AttemptCreate, AttemptResult, BreakdownSlideOut,
    BreakdownAnswerCreate, BreakdownAnswerResult, StudentCreate, StudentOut,
)
from app.config import get_settings
from app.rate_limit import limiter
from app.security import AuthContext, require_current_user

router = APIRouter(prefix="/api", tags=["practice"])
settings = get_settings()


# ---------------------------------------------------------------------------
@router.post("/students", response_model=StudentOut)
@limiter.limit(settings.RATE_LIMIT_DEFAULT)
async def get_or_create_student(request: Request, body: StudentCreate):
    async with acquire() as conn:
        row = await conn.fetchrow("select id, device_id, name, target_exam from students where device_id=$1", body.device_id)
        if row is None:
            row = await conn.fetchrow(
                """
                insert into students (device_id, name, email, target_exam)
                values ($1,$2,$3,$4)
                returning id, device_id, name, target_exam
                """,
                body.device_id, body.name, body.email, body.target_exam or "UPSC",
            )
        else:
            await conn.execute("update students set last_active_at=now() where id=$1", row["id"])
    return StudentOut(id=str(row["id"]), device_id=row["device_id"], name=row["name"], target_exam=row["target_exam"])


# ---------------------------------------------------------------------------
@router.get("/questions/topic/{topic_id}", response_model=QuestionOut)
@limiter.limit(settings.RATE_LIMIT_DEFAULT)
async def get_question_for_topic(request: Request, topic_id: str):
    async with acquire() as conn:
        row = await conn.fetchrow(
            "select id, topic_id, question_text, options, difficulty from ca_questions where topic_id=$1 limit 1",
            topic_id,
        )
    if row is None:
        raise HTTPException(404, "No question found for this topic")
    return QuestionOut(
        id=str(row["id"]), topic_id=str(row["topic_id"]), question_text=row["question_text"],
        options=row["options"], difficulty=row["difficulty"],
    )


# ---------------------------------------------------------------------------
@router.post("/attempts", response_model=AttemptResult)
@limiter.limit(settings.RATE_LIMIT_SUBMIT)
async def submit_attempt_authenticated(
    request: Request,
    body: AttemptCreate,
    current: AuthContext = Depends(require_current_user),
):
    if body.student_id != current.student_id:
        raise HTTPException(403, "Cannot submit attempts for another account")
    async with acquire() as conn:
        q = await conn.fetchrow(
            """
            select q.correct_option, q.explanation, q.question_text,
                   t.title as topic_title, t.subject_tags
            from ca_questions q
            join ca_topics t on t.id = q.topic_id
            where q.id=$1
            """,
            body.question_id,
        )
        if q is None:
            raise HTTPException(404, "Question not found")
        is_correct = body.selected_option.strip().upper() == q["correct_option"].strip().upper()

        await conn.execute(
            """
            insert into student_attempts
              (student_id, question_id, selected_option, is_correct, attempt_number,
               went_through_breakdown, question_text_snapshot, topic_title_snapshot,
               subject_tags_snapshot)
            values ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            """,
            current.student_id, body.question_id, body.selected_option, is_correct,
            body.attempt_number, body.went_through_breakdown, q["question_text"],
            q["topic_title"], q["subject_tags"],
        )

        breakdown_available = False
        if not is_correct:
            count = await conn.fetchval("select count(*) from breakdown_slides where question_id=$1", body.question_id)
            breakdown_available = count > 0

    return AttemptResult(
        is_correct=is_correct,
        correct_option=q["correct_option"] if (is_correct or body.attempt_number >= 2) else None,
        explanation=q["explanation"] if (is_correct or body.attempt_number >= 2) else None,
        breakdown_available=breakdown_available,
    )


# ---------------------------------------------------------------------------
@router.get("/breakdown/{question_id}", response_model=list[BreakdownSlideOut])
@limiter.limit(settings.RATE_LIMIT_DEFAULT)
async def get_breakdown(request: Request, question_id: str):
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            select id, slide_order, slide_type, subject, content,
                   practice_question, practice_options
            from breakdown_slides
            where question_id=$1
            order by slide_order asc
            """,
            question_id,
        )
    if not rows:
        raise HTTPException(404, "No breakdown available for this question yet")
    return [
        BreakdownSlideOut(
            id=str(r["id"]), slide_order=r["slide_order"], slide_type=r["slide_type"],
            subject=r["subject"], content=r["content"],
            practice_question=r["practice_question"], practice_options=r["practice_options"],
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
@router.post("/breakdown-answers", response_model=BreakdownAnswerResult)
@limiter.limit(settings.RATE_LIMIT_SUBMIT)
async def submit_breakdown_answer_authenticated(
    request: Request,
    body: BreakdownAnswerCreate,
    current: AuthContext = Depends(require_current_user),
):
    if body.student_id != current.student_id:
        raise HTTPException(403, "Cannot submit breakdown answers for another account")
    async with acquire() as conn:
        slide = await conn.fetchrow(
            """
            select practice_correct_option, practice_explanation,
                   practice_question, subject
            from breakdown_slides where id=$1
            """,
            body.slide_id,
        )
        if slide is None or slide["practice_correct_option"] is None:
            raise HTTPException(404, "Practice slide not found")
        is_correct = body.selected_option.strip().upper() == slide["practice_correct_option"].strip().upper()
        await conn.execute(
            """
            insert into student_breakdown_answers
              (student_id, slide_id, selected_option, is_correct,
               practice_question_snapshot, subject_snapshot)
            values ($1,$2,$3,$4,$5,$6)
            """,
            current.student_id, body.slide_id, body.selected_option, is_correct,
            slide["practice_question"], slide["subject"],
        )
    return BreakdownAnswerResult(
        is_correct=is_correct,
        correct_option=slide["practice_correct_option"],
        explanation=slide["practice_explanation"],
    )
