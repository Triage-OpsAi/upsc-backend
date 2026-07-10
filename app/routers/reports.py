from fastapi import APIRouter, Depends, Request, HTTPException, Query
from datetime import date
from app.database import acquire
from app.schemas import DailyReportOut
from app.config import get_settings
from app.rate_limit import limiter
from app.security import AuthContext, require_current_user
from app.services.report_generator import build_report_for_student

router = APIRouter(prefix="/api/reports", tags=["reports"])
settings = get_settings()


@router.get("/me", response_model=DailyReportOut)
@limiter.limit(settings.RATE_LIMIT_DEFAULT)
async def get_my_report(
    request: Request,
    report_date: date | None = Query(None),
    current: AuthContext = Depends(require_current_user),
):
    return await _get_report_for_student(current.student_id, report_date)


@router.get("/{student_id}", response_model=DailyReportOut)
@limiter.limit(settings.RATE_LIMIT_DEFAULT)
async def get_report(
    request: Request,
    student_id: str,
    report_date: date | None = Query(None),
    current: AuthContext = Depends(require_current_user),
):
    if student_id != current.student_id:
        raise HTTPException(403, "Cannot read another account's report")
    return await _get_report_for_student(student_id, report_date)


async def _get_report_for_student(student_id: str, report_date: date | None):
    async with acquire() as conn:
        d = report_date
        if d is None:
            latest = await conn.fetchrow(
                """
                select
                  (select max(report_date) from daily_reports where student_id=$1) as report_date,
                  (select max((created_at at time zone 'Asia/Kolkata')::date)
                   from student_attempts
                   where student_id=$1 and attempt_number=1) as attempt_date
                """,
                student_id,
            )
            candidates = [
                value for value in (latest["report_date"], latest["attempt_date"])
                if value is not None
            ]
            if not candidates:
                raise HTTPException(404, "No practice attempts found for this account")
            d = max(candidates)

        row = await conn.fetchrow(
            """
            select report_date, total_attempted, total_correct, accuracy, percentile,
                   subject_breakdown, exam_wise_readiness, ai_feedback
            from daily_reports
            where student_id=$1 and report_date=$2
            """,
            student_id, d,
        )
        if row is None:
            live_report = await build_report_for_student(conn, student_id, d)
            if live_report is None:
                raise HTTPException(404, "No practice attempts found for this date")
            return DailyReportOut(**live_report)

    return DailyReportOut(
        report_date=row["report_date"], total_attempted=row["total_attempted"],
        total_correct=row["total_correct"], accuracy=float(row["accuracy"]),
        percentile=float(row["percentile"]), subject_breakdown=row["subject_breakdown"],
        exam_wise_readiness=row["exam_wise_readiness"], ai_feedback=row["ai_feedback"],
    )
