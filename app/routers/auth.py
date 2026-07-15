import asyncio
import logging
import math
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from app.config import get_settings
from app.database import acquire
from app.mailer import send_email, try_send_email
from app.rate_limit import limiter
from app.redis_cache import delete_session_cache, delete_session_caches
from app.schemas import AuthTokenOut, OtpRequest, OtpRequestOut, OtpVerify, ProfileUpdate, StudentOut
from app.security import (
    AuthContext,
    create_access_token,
    generate_otp,
    hash_otp,
    hash_token,
    normalize_email,
    require_current_user,
    verify_otp,
)
from app.subscriptions import STANDARD_MONTHLY_PRICE_INR, access_state_from_row

router = APIRouter(prefix="/api", tags=["auth"])
settings = get_settings()
logger = logging.getLogger(__name__)


@router.post("/auth/request-otp", response_model=OtpRequestOut)
@limiter.limit(settings.RATE_LIMIT_SUBMIT)
async def request_otp(request: Request, body: OtpRequest):
    email = normalize_email(body.email)
    otp = generate_otp()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.OTP_TTL_MINUTES)

    async with acquire() as conn:
        last_requested_at = await conn.fetchval(
            """
            select created_at
            from auth_otp_codes
            where email=$1 and purpose=$2
            order by created_at desc
            limit 1
            """,
            email,
            body.purpose or "login",
        )
        if last_requested_at is not None:
            elapsed = (datetime.now(timezone.utc) - last_requested_at).total_seconds()
            remaining = math.ceil(settings.OTP_RESEND_COOLDOWN_SECONDS - elapsed)
            if remaining > 0:
                raise HTTPException(
                    429,
                    detail={
                        "code": "otp_resend_cooldown",
                        "message": f"Please wait {remaining} seconds before requesting another OTP.",
                        "retry_after_seconds": remaining,
                    },
                    headers={"Retry-After": str(remaining)},
                )
        account_exists = bool(
            await conn.fetchval("select exists(select 1 from students where email=$1)", email)
        )
        otp_id = await conn.fetchval(
            """
            insert into auth_otp_codes (email, otp_hash, purpose, expires_at, request_ip, user_agent)
            values ($1,$2,$3,$4,$5,$6)
            returning id
            """,
            email,
            hash_otp(email, otp),
            body.purpose or "login",
            expires_at,
            request.client.host if request.client else None,
            request.headers.get("user-agent"),
        )

    if settings.DEV_EXPOSE_LOGGED_OTP:
        logger.warning("DEV OTP for %s is %s", email, otp)

    try:
        delivery = await asyncio.to_thread(
            send_email,
            email,
            "Your AspirantOS sign-in code",
            (
                "Use this one-time password to sign in to AspirantOS:\n\n"
                f"{otp}\n\n"
                f"It expires in {settings.OTP_TTL_MINUTES} minutes. If you did not request this, ignore this email."
            ),
        )
        logger.info(
            "OTP email accepted for %s; message_id=%s provider=%s attempts=%s",
            email,
            delivery.message_id,
            delivery.provider,
            delivery.attempts,
        )
    except Exception as error:
        async with acquire() as conn:
            await conn.execute("delete from auth_otp_codes where id=$1", otp_id)
        logger.exception("OTP email delivery failed for %s", email)
        raise HTTPException(503, "Could not send the OTP email. Please try again.") from error

    async with acquire() as conn:
        await conn.execute(
            """
            update auth_otp_codes
            set consumed_at=now()
            where email=$1 and purpose=$2 and consumed_at is null and id<>$3
            """,
            email,
            body.purpose or "login",
            otp_id,
        )
    return OtpRequestOut(
        ok=True,
        expires_in_minutes=settings.OTP_TTL_MINUTES,
        account_exists=account_exists,
        resend_after_seconds=settings.OTP_RESEND_COOLDOWN_SECONDS,
        dev_otp=otp if settings.DEV_EXPOSE_LOGGED_OTP else None,
    )


@router.post("/auth/verify-otp", response_model=AuthTokenOut)
@limiter.limit(settings.RATE_LIMIT_SUBMIT)
async def verify_otp_login(request: Request, body: OtpVerify):
    email = normalize_email(body.email)
    now = datetime.now(timezone.utc)
    device_id = body.device_id.strip()
    login_ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")
    new_account = False
    switched_device = False
    suspended_until = None
    suspension_reason = None
    distinct_devices = 0
    revoked_session_ids: list[str] = []

    async with acquire() as conn:
        otp_row = await conn.fetchrow(
            """
            select id, otp_hash, attempts, expires_at
            from auth_otp_codes
            where email=$1 and purpose='login' and consumed_at is null
            order by created_at desc
            limit 1
            """,
            email,
        )
        if otp_row is None:
            raise HTTPException(400, "OTP expired or not found")
        if otp_row["expires_at"] <= now:
            raise HTTPException(400, "OTP expired")
        if otp_row["attempts"] >= settings.OTP_MAX_ATTEMPTS:
            raise HTTPException(429, "Too many OTP attempts")
        if not verify_otp(email, body.otp, otp_row["otp_hash"]):
            await conn.execute("update auth_otp_codes set attempts=attempts+1 where id=$1", otp_row["id"])
            raise HTTPException(400, "Invalid OTP")

        device_lock = await conn.fetchrow(
            """
            select email
            from auth_device_email_locks
            where device_id=$1
            """,
            device_id,
        )
        if device_lock is None:
            legacy_device_owner = await conn.fetchrow(
                """
                select id, email
                from students
                where device_id=$1 and email is not null
                """,
                device_id,
            )
            if legacy_device_owner is not None:
                await conn.execute(
                    """
                    insert into auth_device_email_locks (device_id, email, student_id)
                    values ($1,$2,$3)
                    on conflict (device_id) do nothing
                    """,
                    device_id,
                    normalize_email(legacy_device_owner["email"]),
                    legacy_device_owner["id"],
                )
                device_lock = {"email": normalize_email(legacy_device_owner["email"])}

        if device_lock is not None and normalize_email(device_lock["email"]) != email:
            await conn.execute(
                """
                update auth_device_email_locks
                set blocked_attempts=blocked_attempts+1,
                    last_attempted_email=$2,
                    last_blocked_at=now()
                where device_id=$1
                """,
                device_id,
                email,
            )
            await conn.execute("update auth_otp_codes set consumed_at=now() where id=$1", otp_row["id"])
            await asyncio.to_thread(
                try_send_email,
                email,
                "Sign-in blocked on this device",
                (
                    "This sign-in was blocked because this device is already linked "
                    "to a different Current Affairs Gazette account.\n\n"
                    "Use the original account on this device, or use a different device."
                ),
            )
            raise HTTPException(403, "This device is already linked to another email account")

        async with conn.transaction():
            student = await conn.fetchrow(
                """
                select id, device_id, name, email, target_exam, avatar_url, bio, city,
                       suspended_until, active_device_id, trial_ends_at,
                       subscription_status, early_offer_number
                from students
                where email=$1
                """,
                email,
            )
            if student is None:
                student = await conn.fetchrow(
                    """
                    select id, device_id, name, email, target_exam, avatar_url, bio, city,
                           suspended_until, active_device_id, trial_ends_at,
                           subscription_status, early_offer_number
                    from students
                    where device_id=$1 and email is null
                    """,
                    device_id,
                )
                if student is None:
                    student = await conn.fetchrow(
                        """
                        insert into students
                          (device_id, active_device_id, name, email, target_exam, email_verified_at, last_login_at)
                        values ($1,$1,$2,$3,$4,now(),now())
                        returning id, device_id, name, email, target_exam, avatar_url, bio, city,
                                  suspended_until, active_device_id, trial_ends_at,
                                  subscription_status, early_offer_number
                        """,
                        device_id,
                        body.name,
                        email,
                        body.target_exam or "UPSC",
                    )
                else:
                    student = await conn.fetchrow(
                        """
                        update students
                        set email=$2, name=coalesce($3, name), target_exam=coalesce($4, target_exam),
                            active_device_id=$1, email_verified_at=now(), last_login_at=now()
                        where id=$5
                        returning id, device_id, name, email, target_exam, avatar_url, bio, city,
                                  suspended_until, active_device_id, trial_ends_at,
                                  subscription_status, early_offer_number
                        """,
                        device_id,
                        email,
                        body.name,
                        body.target_exam,
                        student["id"],
                    )
                new_account = True
            else:
                switched_device = bool(student["active_device_id"] and student["active_device_id"] != device_id)
                if student["suspended_until"] and student["suspended_until"] > now:
                    await conn.execute("update auth_otp_codes set consumed_at=now() where id=$1", otp_row["id"])
                    suspended_until = student["suspended_until"]
                    suspension_reason = "Account is currently suspended"
                else:
                    student = await conn.fetchrow(
                        """
                        update students
                        set name=coalesce($2, name), target_exam=coalesce($3, target_exam),
                            email_verified_at=coalesce(email_verified_at, now()), last_login_at=now()
                        where id=$1
                        returning id, device_id, name, email, target_exam, avatar_url, bio, city,
                                  suspended_until, active_device_id, trial_ends_at,
                                  subscription_status, early_offer_number
                        """,
                        student["id"],
                        body.name,
                        body.target_exam,
                    )

            if suspended_until is None:
                device_lock_row = await conn.fetchrow(
                    """
                    insert into auth_device_email_locks (device_id, email, student_id, last_seen_at)
                    values ($1,$2,$3,now())
                    on conflict (device_id) do update
                    set student_id=excluded.student_id,
                        last_seen_at=now()
                    where auth_device_email_locks.email=excluded.email
                    returning device_id
                    """,
                    device_id,
                    email,
                    student["id"],
                )
                if device_lock_row is None:
                    await conn.execute(
                        """
                        update auth_device_email_locks
                        set blocked_attempts=blocked_attempts+1,
                            last_attempted_email=$2,
                            last_blocked_at=now()
                        where device_id=$1
                        """,
                        device_id,
                        email,
                    )
                    await conn.execute("update auth_otp_codes set consumed_at=now() where id=$1", otp_row["id"])
                    raise HTTPException(403, "This device is already linked to another email account")

                await conn.execute(
                    """
                    insert into auth_device_events (student_id, email, device_id, event_type, request_ip, user_agent)
                    values ($1,$2,$3,'login_verified',$4,$5)
                    """,
                    student["id"],
                    email,
                    device_id,
                    login_ip,
                    user_agent,
                )
                distinct_devices = int(await conn.fetchval(
                    """
                    select count(distinct device_id)
                    from auth_device_events
                    where student_id=$1
                      and event_type='login_verified'
                      and created_at >= now() - make_interval(days => $2::int)
                    """,
                    student["id"],
                    settings.DEVICE_SWITCH_WINDOW_DAYS,
                ) or 0)
                revoked_session_ids = [
                    str(row["id"])
                    for row in await conn.fetch(
                        "select id from auth_sessions where student_id=$1 and revoked_at is null",
                        student["id"],
                    )
                ]
                if distinct_devices > settings.DEVICE_LIMIT_BEFORE_SUSPENSION:
                    suspended_until = now + timedelta(days=settings.ACCOUNT_SUSPENSION_DAYS)
                    suspension_reason = (
                        f"More than {settings.DEVICE_LIMIT_BEFORE_SUSPENSION} devices used for this account"
                    )
                    await conn.execute(
                        """
                        update students
                        set suspended_until=$2, suspension_reason=$3, active_device_id=null
                        where id=$1
                        """,
                        student["id"],
                        suspended_until,
                        suspension_reason,
                    )
                    await conn.execute(
                        "update auth_sessions set revoked_at=now(), revoked_reason='account_suspended' where student_id=$1 and revoked_at is null",
                        student["id"],
                    )
                    await conn.execute("update auth_otp_codes set consumed_at=now() where id=$1", otp_row["id"])
                else:
                    await conn.execute(
                        """
                        update auth_sessions
                        set revoked_at=now(), revoked_reason='new_login'
                        where student_id=$1 and revoked_at is null
                        """,
                        student["id"],
                    )
                    session = await conn.fetchrow(
                        """
                        insert into auth_sessions (student_id, device_id, expires_at, request_ip, user_agent)
                        values ($1,$2,now() + make_interval(hours => $3::int),$4,$5)
                        returning id, expires_at
                        """,
                        student["id"],
                        device_id,
                        settings.SESSION_TTL_HOURS,
                        login_ip,
                        user_agent,
                    )
                    await conn.execute(
                        """
                        update students
                        set active_device_id=$2, last_login_at=now(), last_active_at=now()
                        where id=$1
                        """,
                        student["id"],
                        device_id,
                    )
                    await conn.execute("update auth_otp_codes set consumed_at=now() where id=$1", otp_row["id"])

                    token = create_access_token(
                        student_id=str(student["id"]),
                        session_id=str(session["id"]),
                        email=email,
                        device_id=device_id,
                        expires_at=session["expires_at"],
                    )
                    await conn.execute("update auth_sessions set token_hash=$2 where id=$1", session["id"], hash_token(token))

    await delete_session_caches(revoked_session_ids)

    if suspended_until is not None:
        await asyncio.to_thread(
            try_send_email,
            email,
            "Your Current Affairs Gazette account is suspended",
            (
                "Your account has been suspended for security reasons.\n\n"
                f"Reason: {suspension_reason}\n"
                f"Suspended until: {suspended_until.isoformat()}\n\n"
                "This protects your account because it was used across too many devices."
            ),
        )
        raise HTTPException(
            403,
            detail={
                "code": "device_limit_suspension",
                "message": (
                    f"Security warning: this account was used on more than "
                    f"{settings.DEVICE_LIMIT_BEFORE_SUSPENSION} devices within "
                    f"{settings.DEVICE_SWITCH_WINDOW_DAYS} days and is suspended until "
                    f"{suspended_until.isoformat()}."
                ),
                "suspended_until": suspended_until.isoformat(),
                "device_limit": settings.DEVICE_LIMIT_BEFORE_SUSPENSION,
            },
        )

    if new_account:
        await asyncio.to_thread(
            try_send_email,
            email,
            "Welcome to The Current Affairs Gazette",
            (
                "Your account has been created with a 7-day free trial. "
                "Your login session is valid for up to 30 days on this device."
            ),
        )
    elif switched_device:
        await asyncio.to_thread(
            try_send_email,
            email,
            "New device login to The Current Affairs Gazette",
            "Your account just signed in on a new device. Your previous device session was ended automatically.",
        )

    return AuthTokenOut(
        access_token=token,
        expires_at=session["expires_at"].isoformat(),
        student=_student_out(
            student,
            active_device_id=device_id,
            recent_device_count=distinct_devices,
        ),
    )


@router.post("/auth/logout")
async def logout(current: AuthContext = Depends(require_current_user)):
    async with acquire() as conn:
        await conn.execute(
            "update auth_sessions set revoked_at=now(), revoked_reason='logout' where id=$1",
            current.session_id,
        )
    await delete_session_cache(current.session_id)
    return {"ok": True}


@router.get("/auth/me", response_model=StudentOut)
async def me(current: AuthContext = Depends(require_current_user)):
    async with acquire() as conn:
        student = await conn.fetchrow(
            """
            select id, device_id, name, email, target_exam, avatar_url, bio, city,
                   suspended_until, active_device_id, trial_ends_at,
                   subscription_status, early_offer_number
            from students
            where id=$1
            """,
            current.student_id,
        )
        recent_device_count = await _recent_device_count(conn, current.student_id)
    if student is None:
        raise HTTPException(404, "Account not found")
    return _student_out(student, recent_device_count=recent_device_count)


@router.patch("/profile", response_model=StudentOut)
async def update_profile(body: ProfileUpdate, current: AuthContext = Depends(require_current_user)):
    async with acquire() as conn:
        student = await conn.fetchrow(
            """
            update students
            set name=coalesce($2, name),
                target_exam=coalesce($3, target_exam),
                avatar_url=coalesce($4, avatar_url),
                bio=coalesce($5, bio),
                city=coalesce($6, city),
                profile_completed_at=case
                    when coalesce($2, name) is not null then coalesce(profile_completed_at, now())
                    else profile_completed_at
                end,
                last_active_at=now()
            where id=$1
            returning id, device_id, name, email, target_exam, avatar_url, bio, city,
                      suspended_until, active_device_id, trial_ends_at,
                      subscription_status, early_offer_number
            """,
            current.student_id,
            body.name,
            body.target_exam,
            body.avatar_url,
            body.bio,
            body.city,
        )
        recent_device_count = await _recent_device_count(conn, current.student_id)
    await delete_session_cache(current.session_id)
    return _student_out(student, recent_device_count=recent_device_count)


async def _recent_device_count(conn, student_id: str) -> int:
    return int(await conn.fetchval(
        """
        select count(distinct device_id)
        from auth_device_events
        where student_id=$1
          and event_type='login_verified'
          and created_at >= now() - make_interval(days => $2::int)
        """,
        student_id,
        settings.DEVICE_SWITCH_WINDOW_DAYS,
    ) or 0)


def _device_warning(recent_device_count: int) -> str | None:
    if recent_device_count < settings.DEVICE_LIMIT_BEFORE_SUSPENSION:
        return None
    return (
        f"Security warning: this account has been used on {recent_device_count} devices "
        f"within {settings.DEVICE_SWITCH_WINDOW_DAYS} days. Signing in on another new "
        f"device will suspend the account for {settings.ACCOUNT_SUSPENSION_DAYS} days."
    )


def _student_out(
    row,
    active_device_id: str | None = None,
    recent_device_count: int = 0,
) -> StudentOut:
    access = access_state_from_row(row)
    return StudentOut(
        id=str(row["id"]),
        device_id=active_device_id or row["active_device_id"] or row["device_id"],
        name=row["name"],
        email=row["email"],
        target_exam=row["target_exam"] or "UPSC",
        avatar_url=row["avatar_url"],
        bio=row["bio"],
        city=row["city"],
        suspended_until=row["suspended_until"],
        recent_device_count=recent_device_count,
        device_limit=settings.DEVICE_LIMIT_BEFORE_SUSPENSION,
        device_warning=_device_warning(recent_device_count),
        subscription_status=access.status,
        has_content_access=access.has_content_access,
        trial_ends_at=access.trial_ends_at,
        trial_days_remaining=access.trial_days_remaining,
        early_offer_eligible=access.early_offer_eligible,
        early_offer_number=access.early_offer_number,
        monthly_price_inr=access.monthly_price_inr,
        standard_monthly_price_inr=STANDARD_MONTHLY_PRICE_INR,
    )
