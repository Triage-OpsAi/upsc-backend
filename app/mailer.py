import logging
import smtplib
from email.message import EmailMessage

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


def send_email(to_email: str, subject: str, body: str) -> None:
    if not settings.SMTP_HOST or not settings.SMTP_FROM_EMAIL:
        raise RuntimeError("SMTP settings are not configured")

    msg = EmailMessage()
    msg["From"] = settings.SMTP_FROM_EMAIL
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)

    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=20) as smtp:
        if settings.SMTP_USE_TLS:
            smtp.starttls()
        if settings.SMTP_USERNAME:
            smtp.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        smtp.send_message(msg)


def try_send_email(to_email: str, subject: str, body: str) -> None:
    try:
        send_email(to_email, subject, body)
    except Exception as exc:
        logger.warning("Email send failed for %s: %s", to_email, exc, exc_info=True)
