import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from fastapi import Header, HTTPException

from app.config import get_settings
from app.database import acquire

settings = get_settings()


@dataclass(frozen=True)
class AuthContext:
    student_id: str
    session_id: str
    email: str
    device_id: str
    name: str | None
    target_exam: str


def normalize_email(email: str) -> str:
    value = " ".join(email.strip().lower().split())
    if "@" not in value or "." not in value.rsplit("@", 1)[-1]:
        raise HTTPException(422, "Enter a valid email address")
    return value


def generate_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def hash_otp(email: str, otp: str) -> str:
    return _hmac_hex(f"otp:{normalize_email(email)}:{otp.strip()}")


def verify_otp(email: str, otp: str, otp_hash: str) -> bool:
    return hmac.compare_digest(hash_otp(email, otp), otp_hash)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_access_token(
    *,
    student_id: str,
    session_id: str,
    email: str,
    device_id: str,
    expires_at: datetime,
) -> str:
    if not settings.JWT_SECRET:
        raise RuntimeError("JWT_SECRET or CRON_SECRET must be set before auth can issue tokens")

    exp = int(expires_at.timestamp())
    now = int(datetime.now(timezone.utc).timestamp())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "iss": settings.JWT_ISSUER,
        "sub": student_id,
        "sid": session_id,
        "email": email,
        "device_id": device_id,
        "iat": now,
        "exp": exp,
    }
    signing_input = f"{_b64json(header)}.{_b64json(payload)}"
    signature = _b64url(hmac.new(settings.JWT_SECRET.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256).digest())
    return f"{signing_input}.{signature}"


def decode_access_token(token: str) -> dict[str, Any]:
    if not settings.JWT_SECRET:
        raise HTTPException(500, "JWT secret is not configured")

    parts = token.split(".")
    if len(parts) != 3:
        raise HTTPException(401, "Invalid token")
    signing_input = f"{parts[0]}.{parts[1]}"
    expected = _b64url(hmac.new(settings.JWT_SECRET.encode("utf-8"), signing_input.encode("utf-8"), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, parts[2]):
        raise HTTPException(401, "Invalid token")

    try:
        payload = json.loads(_b64url_decode(parts[1]))
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(401, "Invalid token")

    if payload.get("iss") != settings.JWT_ISSUER:
        raise HTTPException(401, "Invalid token issuer")
    if int(payload.get("exp", 0)) <= int(datetime.now(timezone.utc).timestamp()):
        raise HTTPException(401, "Session expired")
    if not payload.get("sub") or not payload.get("sid") or not payload.get("device_id"):
        raise HTTPException(401, "Invalid token")
    return payload


async def require_current_user(
    authorization: str | None = Header(None),
    x_device_id: str | None = Header(None),
) -> AuthContext:
    token = _bearer_token(authorization)
    payload = decode_access_token(token)
    token_device_id = str(payload["device_id"])
    if not x_device_id or x_device_id != token_device_id:
        raise HTTPException(401, "This session is valid only on the device that created it")

    async with acquire() as conn:
        row = await conn.fetchrow(
            """
            select sess.id as session_id, sess.device_id, sess.expires_at, sess.revoked_at,
                   s.id as student_id, s.email, s.name, s.target_exam, s.suspended_until
            from auth_sessions sess
            join students s on s.id = sess.student_id
            where sess.id=$1 and sess.student_id=$2
            """,
            payload["sid"],
            payload["sub"],
        )
        if row is None or row["revoked_at"] is not None:
            raise HTTPException(401, "Session is no longer active")
        if row["expires_at"] <= datetime.now(timezone.utc):
            raise HTTPException(401, "Session expired")
        if row["device_id"] != x_device_id:
            raise HTTPException(401, "This account is active on another device")
        if row["suspended_until"] and row["suspended_until"] > datetime.now(timezone.utc):
            raise HTTPException(403, f"Account suspended until {row['suspended_until'].isoformat()}")

        await conn.execute("update auth_sessions set last_seen_at=now() where id=$1", row["session_id"])
        await conn.execute("update students set last_active_at=now() where id=$1", row["student_id"])

    return AuthContext(
        student_id=str(row["student_id"]),
        session_id=str(row["session_id"]),
        email=row["email"],
        device_id=row["device_id"],
        name=row["name"],
        target_exam=row["target_exam"],
    )


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Missing bearer token")
    return authorization[7:].strip()


def _hmac_hex(value: str) -> str:
    if not settings.JWT_SECRET:
        raise RuntimeError("JWT_SECRET or CRON_SECRET must be set before auth can hash OTPs")
    return hmac.new(settings.JWT_SECRET.encode("utf-8"), value.encode("utf-8"), hashlib.sha256).hexdigest()


def _b64json(value: dict[str, Any]) -> str:
    return _b64url(json.dumps(value, separators=(",", ":")).encode("utf-8"))


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> str:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
