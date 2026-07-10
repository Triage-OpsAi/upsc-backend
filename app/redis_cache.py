"""Small fail-open Upstash REST cache for authenticated session lookups."""

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.config import get_settings


settings = get_settings()
logger = logging.getLogger(__name__)


def redis_enabled() -> bool:
    return bool(settings.UPSTASH_REDIS_REST_URL and settings.UPSTASH_REDIS_REST_TOKEN)


def _session_key(session_id: str) -> str:
    return f"upsc:auth:session:{session_id}"


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _command(*parts: Any) -> Any:
    if not redis_enabled():
        return None
    try:
        async with httpx.AsyncClient(timeout=2.5) as client:
            response = await client.post(
                settings.UPSTASH_REDIS_REST_URL,
                headers={"Authorization": f"Bearer {settings.UPSTASH_REDIS_REST_TOKEN}"},
                json=list(parts),
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("error"):
                raise RuntimeError(str(payload["error"]))
            return payload.get("result")
    except Exception as error:
        # Redis is an optimization only. Authentication falls back to Postgres.
        logger.warning("Redis command failed: %s", error)
        return None


async def get_cached_session(session_id: str, token: str) -> dict[str, Any] | None:
    raw = await _command("GET", _session_key(session_id))
    if not raw:
        return None
    try:
        cached = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        await delete_session_cache(session_id)
        return None
    if cached.get("token_fingerprint") != _token_fingerprint(token):
        return None
    if int(cached.get("expires_at", 0)) <= int(datetime.now(timezone.utc).timestamp()):
        await delete_session_cache(session_id)
        return None
    return cached


async def set_cached_session(
    *,
    session_id: str,
    token: str,
    expires_at: int,
    student_id: str,
    email: str,
    device_id: str,
    name: str | None,
    target_exam: str,
) -> None:
    remaining = max(1, expires_at - int(datetime.now(timezone.utc).timestamp()))
    ttl = min(remaining, settings.REDIS_SESSION_CACHE_TTL_SECONDS)
    value = json.dumps(
        {
            "session_id": session_id,
            "student_id": student_id,
            "email": email,
            "device_id": device_id,
            "name": name,
            "target_exam": target_exam,
            "expires_at": expires_at,
            "token_fingerprint": _token_fingerprint(token),
        },
        separators=(",", ":"),
    )
    await _command("SET", _session_key(session_id), value, "EX", ttl)


async def delete_session_cache(session_id: str) -> None:
    await _command("DEL", _session_key(session_id))


async def delete_session_caches(session_ids: list[str]) -> None:
    if not session_ids:
        return
    await _command("DEL", *[_session_key(session_id) for session_id in session_ids])
