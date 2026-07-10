"""
These endpoints are meant to be hit ONLY by Vercel Cron (see vercel.json),
never by the frontend. Protected by a shared-secret header so nobody else
can trigger (and pay for) OpenAI generation.
"""

from fastapi import APIRouter, Request, Header, HTTPException
from datetime import datetime, timedelta
import logging
from app.database import acquire
from app.config import get_settings
from app.rate_limit import limiter
from app.services.content_generator import store_researched_topics
from app.services.report_generator import generate_reports_for_date
from app.time_utils import ist_today

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
    today = ist_today()
    live_start = datetime.strptime(settings.LIVE_CRON_START_DATE, "%Y-%m-%d").date()
    logger.info("daily-content date check: today=%s live_start=%s should_run=%s", today, live_start, today >= live_start)
    if today < live_start:
        return {"skipped": True, "reason": f"live cron starts {live_start}"}

    async with acquire() as conn:
        previous_status = await conn.fetchval(
            "select status from generation_log where run_type='daily_content_cron' and run_date=$1", today
        )
        if previous_status == "success":
            return {"skipped": True, "reason": "already ran today"}
        await conn.execute(
            """
            insert into generation_log (run_type, run_date, status)
            values ('daily_content_cron',$1,'started')
            on conflict (run_type, run_date) do update
            set status='started', details='{}'::jsonb
            """,
            today,
        )
        try:
            from app.openai_client import research_todays_current_affairs

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
    """Runs just after midnight IST and reports on the day that just ended."""
    _check_secret(authorization, x_cron_secret)
    report_date = ist_today() - timedelta(days=1)
    async with acquire() as conn:
        previous_status = await conn.fetchval(
            "select status from generation_log where run_type='daily_report_cron' and run_date=$1", report_date
        )
        if previous_status == "success":
            return {"skipped": True, "reason": "report already generated", "report_date": report_date}
        await conn.execute(
            """
            insert into generation_log (run_type, run_date, status)
            values ('daily_report_cron',$1,'started')
            on conflict (run_type, run_date) do update
            set status='started', details='{}'::jsonb
            """,
            report_date,
        )
        try:
            count = await generate_reports_for_date(conn, report_date)
            await conn.execute(
                "update generation_log set status='success', details=$2 where run_type='daily_report_cron' and run_date=$1",
                report_date, {"reports_generated": count},
            )
            return {"reports_generated": count, "report_date": report_date}
        except Exception as e:
            await conn.execute(
                "update generation_log set status='failed', details=$2 where run_type='daily_report_cron' and run_date=$1",
                report_date, {"error": str(e)},
            )
            raise
