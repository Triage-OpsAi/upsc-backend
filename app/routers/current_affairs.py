from fastapi import APIRouter, Query, Request
from app.database import acquire
from app.schemas import TopicListOut, TopicOut, PageMeta, NextTopicOut
from app.config import get_settings
from app.rate_limit import limiter

router = APIRouter(prefix="/api/current-affairs", tags=["current-affairs"])
settings = get_settings()


@router.get("", response_model=TopicListOut)
@limiter.limit(settings.RATE_LIMIT_DEFAULT)
async def list_topics(
    request: Request,
    month: int | None = Query(None, ge=1, le=12),
    year: int | None = Query(None, ge=2025),
    page: int = Query(1, ge=1),
    page_size: int = Query(10, ge=1, le=settings.MAX_PAGE_SIZE),
):
    """Month-wise, paginated (max 10/page) list of published current-affairs topics."""
    where = ["status = 'published'"]
    params: list = []
    if month:
        params.append(month)
        where.append(f"month = ${len(params)}")
    if year:
        params.append(year)
        where.append(f"year = ${len(params)}")
    where_sql = " and ".join(where)

    offset = (page - 1) * page_size
    params_page = params + [page_size, offset]

    async with acquire() as conn:
        total = await conn.fetchval(f"select count(*) from ca_topics where {where_sql}", *params)
        rows = await conn.fetch(
            f"""
            select id, month, year, title, summary, subject_tags, source_date
            from ca_topics
            where {where_sql}
            order by year desc,
                     month desc,
                     source_date desc nulls last,
                     created_at desc,
                     id desc
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
        )
        for r in rows
    ]
    total_pages = max(1, (total + page_size - 1) // page_size)
    return TopicListOut(items=items, meta=PageMeta(page=page, page_size=page_size, total_items=total, total_pages=total_pages))


@router.get("/{topic_id}/next", response_model=NextTopicOut)
@limiter.limit(settings.RATE_LIMIT_DEFAULT)
async def next_topic(request: Request, topic_id: str):
    """Return the next published topic in the same order used by archive/dashboard."""
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
