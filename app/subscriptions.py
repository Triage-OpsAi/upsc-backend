from dataclasses import dataclass
from datetime import datetime, timezone
from math import ceil

from fastapi import Depends, HTTPException

from app.database import acquire
from app.security import AuthContext, require_current_user

STANDARD_MONTHLY_PRICE_INR = 299
EARLY_MONTHLY_PRICE_INR = 99
EARLY_OFFER_LIMIT = 500


@dataclass(frozen=True)
class AccessState:
    status: str
    has_content_access: bool
    trial_ends_at: datetime
    trial_days_remaining: int
    early_offer_eligible: bool
    early_offer_number: int | None
    monthly_price_inr: int


def access_state_from_row(row, now: datetime | None = None) -> AccessState:
    current_time = now or datetime.now(timezone.utc)
    trial_ends_at = row["trial_ends_at"]
    stored_status = row["subscription_status"] or "trial"
    subscribed = stored_status == "active"
    trial_active = trial_ends_at > current_time
    has_content_access = subscribed or trial_active
    status = "active" if subscribed else ("trial" if trial_active else "expired")
    seconds_remaining = max(0.0, (trial_ends_at - current_time).total_seconds())
    days_remaining = int(ceil(seconds_remaining / 86400)) if seconds_remaining else 0
    offer_number = row["early_offer_number"]
    early_eligible = offer_number is not None and int(offer_number) <= EARLY_OFFER_LIMIT
    return AccessState(
        status=status,
        has_content_access=has_content_access,
        trial_ends_at=trial_ends_at,
        trial_days_remaining=days_remaining,
        early_offer_eligible=early_eligible,
        early_offer_number=int(offer_number) if offer_number is not None else None,
        monthly_price_inr=EARLY_MONTHLY_PRICE_INR if early_eligible else STANDARD_MONTHLY_PRICE_INR,
    )


async def fetch_access_state(conn, student_id: str) -> AccessState:
    row = await conn.fetchrow(
        """
        select trial_ends_at, subscription_status, early_offer_number
        from students
        where id=$1
        """,
        student_id,
    )
    if row is None:
        raise HTTPException(404, "Account not found")
    return access_state_from_row(row)


async def require_content_access(
    current: AuthContext = Depends(require_current_user),
) -> AuthContext:
    async with acquire() as conn:
        access = await fetch_access_state(conn, current.student_id)
    if not access.has_content_access:
        raise HTTPException(
            status_code=402,
            detail={
                "code": "trial_expired",
                "message": "Your 7-day free trial has ended. Choose a plan to continue accessing questions and breakdowns.",
                "trial_ends_at": access.trial_ends_at.isoformat(),
                "early_offer_eligible": access.early_offer_eligible,
                "monthly_price_inr": access.monthly_price_inr,
                "standard_monthly_price_inr": STANDARD_MONTHLY_PRICE_INR,
            },
        )
    return current
