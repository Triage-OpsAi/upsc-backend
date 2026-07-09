from datetime import date, timedelta

from fastapi import APIRouter, Depends, Request

from app.config import get_settings
from app.database import acquire
from app.rate_limit import limiter
from app.schemas import DashboardStatsOut
from app.security import AuthContext, require_current_user

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])
settings = get_settings()


@router.get("", response_model=DashboardStatsOut)
@limiter.limit(settings.RATE_LIMIT_DEFAULT)
async def get_dashboard(request: Request, current: AuthContext = Depends(require_current_user)):
    today = date.today()
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            select count(*)::int as attempted,
                   count(*) filter (where is_correct)::int as correct
            from student_attempts
            where student_id=$1 and created_at::date=$2 and attempt_number=1
            """,
            current.student_id,
            today,
        )
        rank_row = await conn.fetchrow(
            """
            with daily as (
              select student_id,
                     count(*)::int as attempted,
                     count(*) filter (where is_correct)::int as correct
              from student_attempts
              where created_at::date=$1 and attempt_number=1
              group by student_id
            ),
            ranked as (
              select student_id,
                     dense_rank() over (order by correct desc, attempted desc) as rank_today,
                     count(*) over ()::int as active_aspirants_today
              from daily
            )
            select rank_today::int, active_aspirants_today
            from ranked
            where student_id=$2
            """,
            today,
            current.student_id,
        )
        attempt_days = await conn.fetch(
            """
            select distinct created_at::date as attempt_date
            from student_attempts
            where student_id=$1 and attempt_number=1
            order by attempt_date desc
            limit 90
            """,
            current.student_id,
        )

    attempted = row["attempted"] if row else 0
    correct = row["correct"] if row else 0
    accuracy = round((correct * 100.0 / attempted), 2) if attempted else 0.0
    streak = _streak_from_days([r["attempt_date"] for r in attempt_days], today)

    return DashboardStatsOut(
        run_date=today,
        questions_attempted_today=attempted,
        correct_today=correct,
        accuracy_today=accuracy,
        current_streak_days=streak,
        rank_today=rank_row["rank_today"] if rank_row else None,
        active_aspirants_today=rank_row["active_aspirants_today"] if rank_row else 0,
    )


def _streak_from_days(days: list[date], today: date) -> int:
    day_set = set(days)
    cursor = today
    if cursor not in day_set:
        cursor = today - timedelta(days=1)
        if cursor not in day_set:
            return 0

    streak = 0
    while cursor in day_set:
        streak += 1
        cursor -= timedelta(days=1)
    return streak
