from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.responses import JSONResponse
from contextlib import asynccontextmanager

from app.config import get_settings
from app.rate_limit import limiter
from app.database import DatabaseUnavailable, get_pool, close_pool
from app.routers import auth, current_affairs, dashboard, practice, reports, cron, subscriptions

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # warm the pool on cold start; close it cleanly on shutdown so idle
    # connections don't linger against Supabase's connection limit
    await get_pool()
    yield
    await close_pool()


app = FastAPI(title="UPSC Current Affairs Practice API", lifespan=lifespan)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)


@app.exception_handler(DatabaseUnavailable)
async def database_unavailable_handler(request, exc):
    return JSONResponse(
        status_code=503,
        content={"detail": "The database is busy. Please retry in a moment."},
        headers={"Retry-After": "1"},
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(current_affairs.router)
app.include_router(auth.router)
app.include_router(subscriptions.router)
app.include_router(dashboard.router)
app.include_router(practice.router)
app.include_router(reports.router)
app.include_router(cron.router)


@app.get("/health")
@app.get("/api/health")
async def health():
    return {"status": "ok"}
