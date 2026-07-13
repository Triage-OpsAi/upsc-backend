from fastapi import APIRouter, Request

from app.database import acquire
from app.rate_limit import limiter
from app.config import get_settings
from app.schemas import OfferSummaryOut
from app.subscriptions import (
    EARLY_MONTHLY_PRICE_INR,
    EARLY_OFFER_LIMIT,
    STANDARD_MONTHLY_PRICE_INR,
)

router = APIRouter(prefix="/api/subscription", tags=["subscription"])
settings = get_settings()


@router.get("/offer", response_model=OfferSummaryOut)
@limiter.limit(settings.RATE_LIMIT_DEFAULT)
async def offer_summary(request: Request):
    async with acquire() as conn:
        claimed_numbers = int(await conn.fetchval(
            """
            select count(*)
            from students
            where early_offer_number between 1 and $1
            """,
            EARLY_OFFER_LIMIT,
        ) or 0)
    return OfferSummaryOut(
        trial_days=7,
        standard_monthly_price_inr=STANDARD_MONTHLY_PRICE_INR,
        early_monthly_price_inr=EARLY_MONTHLY_PRICE_INR,
        early_offer_limit=EARLY_OFFER_LIMIT,
        spots_remaining=max(0, EARLY_OFFER_LIMIT - claimed_numbers),
    )
