"""
IP-based rate limiting via slowapi (in-memory token bucket).

Note: on Vercel each serverless instance has its OWN memory, so this is
"per-instance" rate limiting, not perfectly global. For a free-tier
100-200 concurrent-user app this is a reasonable, zero-extra-cost
tradeoff. If you outgrow it, swap the storage_uri below for a Redis URL
(Upstash has a free tier that works well on Vercel) - the rest of the
code does not change.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address
from app.config import get_settings

settings = get_settings()

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[settings.RATE_LIMIT_DEFAULT],
    storage_uri="memory://",
)
