"""
These endpoints are meant to be hit ONLY by Vercel Cron (see vercel.json),
never by the frontend. Protected by a shared-secret header so nobody else
can trigger (and pay for) OpenAI generation.
"""

from fastapi import APIRouter, Request, Header, HTTPException
from datetime import date, datetime
import logging
from app.database import acquire
from app.config import get_settings
from app.rate_limit import limiter
from app.services.content_generator import store_researched_topics
from app.services.report_generator import generate_reports_for_date
from app.openai_client import research_todays_current_affairs

router = APIRouter(prefix="/api/cron", tags=["cron"])
settings = get_settings()
logger = logging.getLogger(__name__)


def _provided_secret(authorization: str | None, x_cron_secret: str | None) -> str | None:
    if x_cron_secret:
        return x_cron_secret
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return authorization


def _check_secret(authorization: str | None, x_cron_secret: str | None):
    if not settings.CRON_SECRET or _provided_secret(authorization, x_cron_secret) != settings.CRON_SECRET:
        raise HTTPException(401, "Invalid or missing cron secret")


@router.api_route("/daily-content", methods=["GET", "POST"])
@limiter.limit(settings.RATE_LIMIT_CRON)
async def daily_content(
    request: Request,
    authorization: str | None = Header(None),
    x_cron_secret: str | None = Header(None),
):
    """Runs at midnight IST. From LIVE_CRON_START_DATE onward, researches + stores
    today's current affairs (topics + questions + breakdowns)."""
    _check_secret(authorization, x_cron_secret)
    today = date.today()
    live_start = datetime.strptime(settings.LIVE_CRON_START_DATE, "%Y-%m-%d").date()
    logger.info("daily-content date check: today=%s live_start=%s should_run=%s", today, live_start, today >= live_start)
    if today < live_start:
        return {"skipped": True, "reason": f"live cron starts {live_start}"}

    async with acquire() as conn:
        already = await conn.fetchval(
            "select 1 from generation_log where run_type='daily_content_cron' and run_date=$1", today
        )
        if already:
            return {"skipped": True, "reason": "already ran today"}
        await conn.execute(
            "insert into generation_log (run_type, run_date, status) values ('daily_content_cron',$1,'started')",
            today,
        )
        try:
            topics = await research_todays_current_affairs(today.isoformat(), count=10)
            created = await store_researched_topics(conn, today.month, today.year, topics)
            await conn.execute(
                "update generation_log set status='success', details=$2 where run_type='daily_content_cron' and run_date=$1",
                today, {"created": created},
            )
            return {"created": created}
        except Exception as e:
            await conn.execute(
                "update generation_log set status='failed', details=$2 where run_type='daily_content_cron' and run_date=$1",
                today, {"error": str(e)},
            )
            raise


@router.get("/daily-report")
@limiter.limit(settings.RATE_LIMIT_CRON)
async def daily_report(
    request: Request,
    authorization: str | None = Header(None),
    x_cron_secret: str | None = Header(None),
):
    """Runs at midnight IST (right after daily-content). Generates every active
    student's personalised report for 'today' (i.e. the day that just ended)."""
    _check_secret(authorization, x_cron_secret)
    today = date.today()
    async with acquire() as conn:
        already = await conn.fetchval(
            "select 1 from generation_log where run_type='daily_report_cron' and run_date=$1", today
        )
        if already:
            return {"skipped": True, "reason": "already ran today"}
        await conn.execute(
            "insert into generation_log (run_type, run_date, status) values ('daily_report_cron',$1,'started')",
            today,
        )
        try:
            count = await generate_reports_for_date(conn, today)
            await conn.execute(
                "update generation_log set status='success', details=$2 where run_type='daily_report_cron' and run_date=$1",
                today, {"reports_generated": count},
            )
            return {"reports_generated": count}
        except Exception as e:
            await conn.execute(
                "update generation_log set status='failed', details=$2 where run_type='daily_report_cron' and run_date=$1",
                today, {"error": str(e)},
            )
            raise
