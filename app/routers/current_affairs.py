from fastapi import APIRouter, Depends, Query, Request
from app.database import acquire
from app.schemas import ArchiveMonthOut, TopicListOut, TopicOut, PageMeta, NextTopicOut
from app.config import get_settings
from app.rate_limit import limiter
from app.security import AuthContext
from app.subscriptions import require_content_access

router = APIRouter(prefix="/api/current-affairs", tags=["current-affairs"])
settings = get_settings()


def _require_current_affairs_visible() -> None:
    if not settings.is_subject_visible("current_affairs"):
        from fastapi import HTTPException
        raise HTTPException(404, "Subject not yet available")


@router.get("", response_model=TopicListOut)
@limiter.limit(settings.RATE_LIMIT_DEFAULT)
async def list_topics(
    request: Request,
    month: int | None = Query(None, ge=1, le=12),
    year: int | None = Query(None, ge=2025),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=settings.MAX_PAGE_SIZE),
    current: AuthContext = Depends(require_content_access),
):
    """Month-wise, paginated (max 10/page) list of published current-affairs topics."""
    _require_current_affairs_visible()
    where = [
        "t.status = 'published'",
        "exists (select 1 from ca_questions available_q where available_q.topic_id = t.id)",
    ]
    params: list = []
    if month:
        params.append(month)
        where.append(f"t.month = ${len(params)}")
    if year:
        params.append(year)
        where.append(f"t.year = ${len(params)}")
    where_sql = " and ".join(where)

    offset = (page - 1) * page_size
    params_page = params + [page_size, offset]

    async with acquire() as conn:
        total = await conn.fetchval(f"select count(*) from ca_topics t where {where_sql}", *params)
        rows = await conn.fetch(
            f"""
            select t.id, t.month, t.year, t.title, t.summary, t.subject_tags, t.source_date,
                   (select q.question_text
                    from ca_questions q
                    where q.topic_id = t.id
                    order by q.created_at asc, q.id asc
                    limit 1) as question_text
            from ca_topics t
            where {where_sql}
            order by t.year desc,
                     t.month desc,
                     t.source_date desc nulls last,
                     t.created_at desc,
                     t.id desc
            limit ${len(params_page) - 1} offset ${len(params_page)}
            """,
            *params_page,
        )

    items = [
        TopicOut(
            id=str(r["id"]),
            month=r["month"],
            year=r["year"],
            title=r["title"],
            summary=r["summary"],
            subject_tags=r["subject_tags"] or [],
            source_date=r["source_date"],
            question_text=r["question_text"],
        )
        for r in rows
    ]
    total_pages = (total + page_size - 1) // page_size if total else 0
    return TopicListOut(items=items, meta=PageMeta(page=page, page_size=page_size, total_items=total, total_pages=total_pages))


@router.get("/months", response_model=list[ArchiveMonthOut])
@limiter.limit(settings.RATE_LIMIT_DEFAULT)
async def available_months(
    request: Request,
    current: AuthContext = Depends(require_content_access),
):
    """Return one archive filter option per month that has practice questions."""
    _require_current_affairs_visible()
    async with acquire() as conn:
        rows = await conn.fetch(
            """
            select t.year, t.month, count(*)::int as question_count
            from ca_topics t
            where t.status = 'published'
              and exists (select 1 from ca_questions q where q.topic_id = t.id)
            group by t.year, t.month
            order by t.year desc, t.month desc
            """
        )
    return [
        ArchiveMonthOut(
            year=row["year"],
            month=row["month"],
            question_count=row["question_count"],
        )
        for row in rows
    ]


@router.get("/practice/latest", response_model=NextTopicOut)
@limiter.limit(settings.RATE_LIMIT_DEFAULT)
async def latest_practice_topic(
    request: Request,
    current: AuthContext = Depends(require_content_access),
):
    """Return the newest topic only when it has an actual practice question."""
    _require_current_affairs_visible()
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            select t.id, t.month, t.year, t.title, t.summary, t.subject_tags, t.source_date,
                   (select q.question_text
                    from ca_questions q
                    where q.topic_id = t.id
                    order by q.created_at asc, q.id asc
                    limit 1) as question_text
            from ca_topics t
            where t.status = 'published'
              and exists (select 1 from ca_questions q where q.topic_id = t.id)
            order by t.year desc, t.month desc, t.source_date desc nulls last,
                     t.created_at desc, t.id desc
            limit 1
            """
        )
    if row is None:
        return NextTopicOut(topic=None)
    return NextTopicOut(
        topic=TopicOut(
            id=str(row["id"]),
            month=row["month"],
            year=row["year"],
            title=row["title"],
            summary=row["summary"],
            subject_tags=row["subject_tags"] or [],
            source_date=row["source_date"],
            question_text=row["question_text"],
        )
    )


@router.get("/{topic_id}/next", response_model=NextTopicOut)
@limiter.limit(settings.RATE_LIMIT_DEFAULT)
async def next_topic(
    request: Request,
    topic_id: str,
    current: AuthContext = Depends(require_content_access),
):
    """Return the next published topic in the same order used by archive/dashboard."""
    _require_current_affairs_visible()
    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            with ordered as (
              select t.id,
                     t.month,
                     t.year,
                     t.title,
                     t.summary,
                     t.subject_tags,
                     t.source_date,
                     row_number() over (
                       order by t.year desc,
                                t.month desc,
                                t.source_date desc nulls last,
                                t.created_at desc,
                                t.id desc
                     ) as rn
              from ca_topics t
              where t.status = 'published'
                and exists (
                  select 1
                  from ca_questions q
                  where q.topic_id = t.id
                )
            ),
            current_topic as (
              select rn
              from ordered
              where id = $1
            )
            select id, month, year, title, summary, subject_tags, source_date
            from ordered
            where rn = (select rn + 1 from current_topic)
            """,
            topic_id,
        )
    if row is None:
        return NextTopicOut(topic=None)
    return NextTopicOut(
        topic=TopicOut(
            id=str(row["id"]),
            month=row["month"],
            year=row["year"],
            title=row["title"],
            summary=row["summary"],
            subject_tags=row["subject_tags"] or [],
            source_date=row["source_date"],
        )
    )
