import os
from pathlib import Path
from functools import lru_cache
from dotenv import load_dotenv

# backend/.env only — this app never reads a frontend or root env file.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _bool_env(name: str, default: str = "false") -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


class Settings:
    # --- required secrets (from .env) -----------------------------------
    DATABASE_URL: str = os.environ.get("DATABASE_URL", "")
    OPENAI_API_KEY: str = os.environ.get("OPENAI_API_KEY", "")

    # --- model routing (cheapest model does the heavy-volume work) ------
    # Main question generation (quality matters more, runs rarely/in bulk)
    MODEL_MAIN: str = os.environ.get("MODEL_MAIN", "gpt-4.1-mini")
    # Breakdown slides + daily personalised reports (runs a LOT -> cheapest)
    MODEL_CHEAP: str = os.environ.get("MODEL_CHEAP", "gpt-4o-mini")
    # Model used only by the daily 1am cron to research "what happened today"
    # Needs browsing/tool support - falls back to MODEL_MAIN if unset.
    MODEL_SEARCH: str = os.environ.get("MODEL_SEARCH", "gpt-4.1-mini")

    # --- connection pooling (tuned for Supabase free tier: 60 max conns) -
    # Use the Supabase *pooler* connection string (port 6543, pgbouncer,
    # "Transaction" mode) as DATABASE_URL, not the direct :5432 one, because
    # Vercel spins up many short-lived serverless instances. Each instance
    # keeps only a tiny pool and closes idle connections quickly.
    DB_POOL_MIN_SIZE: int = int(os.environ.get("DB_POOL_MIN_SIZE", "0"))
    DB_POOL_MAX_SIZE: int = int(os.environ.get("DB_POOL_MAX_SIZE", "5"))
    DB_POOL_MAX_INACTIVE_CONN_LIFETIME: float = float(
        os.environ.get("DB_POOL_MAX_INACTIVE_CONN_LIFETIME", "30")
    )  # seconds an idle connection is kept before being closed
    DB_COMMAND_TIMEOUT: float = float(os.environ.get("DB_COMMAND_TIMEOUT", "10"))

    # --- rate limiting -----------------------------------------------------
    RATE_LIMIT_DEFAULT: str = os.environ.get("RATE_LIMIT_DEFAULT", "60/minute")
    RATE_LIMIT_SUBMIT: str = os.environ.get("RATE_LIMIT_SUBMIT", "20/minute")
    RATE_LIMIT_CRON: str = os.environ.get("RATE_LIMIT_CRON", "5/minute")

    # --- pagination ----------------------------------------------------
    MAX_PAGE_SIZE: int = 10

    # --- cron auth -------------------------------------------------------
    CRON_SECRET: str = os.environ.get("CRON_SECRET", "")

    # --- email OTP auth --------------------------------------------------
    JWT_SECRET: str = os.environ.get("JWT_SECRET") or os.environ.get("CRON_SECRET", "")
    JWT_ISSUER: str = os.environ.get("JWT_ISSUER", "upsc-current-affairs")
    SESSION_TTL_HOURS: int = int(os.environ.get("SESSION_TTL_HOURS", "2"))
    OTP_TTL_MINUTES: int = int(os.environ.get("OTP_TTL_MINUTES", "10"))
    OTP_MAX_ATTEMPTS: int = int(os.environ.get("OTP_MAX_ATTEMPTS", "5"))
    DEVICE_SWITCH_WINDOW_DAYS: int = int(os.environ.get("DEVICE_SWITCH_WINDOW_DAYS", "30"))
    DEVICE_LIMIT_BEFORE_SUSPENSION: int = int(os.environ.get("DEVICE_LIMIT_BEFORE_SUSPENSION", "2"))
    ACCOUNT_SUSPENSION_DAYS: int = int(os.environ.get("ACCOUNT_SUSPENSION_DAYS", "3"))
    DEV_EXPOSE_LOGGED_OTP: bool = _bool_env("DEV_EXPOSE_LOGGED_OTP", "false")

    SMTP_HOST: str = os.environ.get("SMTP_HOST", "")
    SMTP_PORT: int = int(os.environ.get("SMTP_PORT", "587"))
    SMTP_USERNAME: str = os.environ.get("SMTP_USERNAME", "")
    SMTP_PASSWORD: str = os.environ.get("SMTP_PASSWORD", "")
    SMTP_FROM_EMAIL: str = os.environ.get("SMTP_FROM_EMAIL", SMTP_USERNAME)
    SMTP_USE_TLS: bool = _bool_env("SMTP_USE_TLS", "true")

    # --- content boundary date -----------------------------------------
    BULK_SEED_START_YEAR: int = 2025
    BULK_SEED_START_MONTH: int = 1
    BULK_SEED_END_YEAR: int = 2026
    BULK_SEED_END_MONTH: int = 7
    LIVE_CRON_START_DATE: str = "2026-07-10"  # daily cron takes over from here

    # --- allowed origins for CORS ---------------------------------------
    ALLOWED_ORIGINS: list = os.environ.get("ALLOWED_ORIGINS", "*").split(",")


@lru_cache
def get_settings() -> Settings:
    return Settings()
