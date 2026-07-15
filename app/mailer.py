import logging
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EmailDelivery:
    message_id: str
    provider: str
    attempts: int


@dataclass(frozen=True)
class _SmtpTransport:
    name: str
    host: str
    port: int
    username: str
    password: str
    from_email: str
    use_tls: bool
    use_ssl: bool


def _transports() -> list[_SmtpTransport]:
    transports = [
        _SmtpTransport(
            name="primary",
            host=settings.SMTP_HOST,
            port=settings.SMTP_PORT,
            username=settings.SMTP_USERNAME,
            password=settings.SMTP_PASSWORD,
            from_email=settings.SMTP_FROM_EMAIL,
            use_tls=settings.SMTP_USE_TLS,
            use_ssl=settings.SMTP_USE_SSL,
        )
    ]
    if settings.SMTP_FALLBACK_HOST:
        transports.append(
            _SmtpTransport(
                name="fallback",
                host=settings.SMTP_FALLBACK_HOST,
                port=settings.SMTP_FALLBACK_PORT,
                username=settings.SMTP_FALLBACK_USERNAME,
                password=settings.SMTP_FALLBACK_PASSWORD,
                from_email=settings.SMTP_FALLBACK_FROM_EMAIL,
                use_tls=settings.SMTP_FALLBACK_USE_TLS,
                use_ssl=settings.SMTP_FALLBACK_USE_SSL,
            )
        )
    return transports


def _is_retryable(error: Exception) -> bool:
    code = getattr(error, "smtp_code", None)
    if isinstance(code, int):
        return 400 <= code < 500
    return isinstance(error, (TimeoutError, OSError, smtplib.SMTPServerDisconnected))


def _send_once(transport: _SmtpTransport, message: EmailMessage, to_email: str) -> None:
    if not transport.host or not transport.from_email:
        raise RuntimeError(f"{transport.name} SMTP settings are not configured")
    if transport.use_ssl and transport.use_tls:
        raise RuntimeError(f"{transport.name} SMTP cannot enable SSL and STARTTLS together")

    smtp_class = smtplib.SMTP_SSL if transport.use_ssl else smtplib.SMTP
    kwargs = {
        "host": transport.host,
        "port": transport.port,
        "timeout": settings.SMTP_TIMEOUT_SECONDS,
    }
    if transport.use_ssl:
        kwargs["context"] = ssl.create_default_context()

    with smtp_class(**kwargs) as smtp:
        smtp.ehlo()
        if transport.use_tls:
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
        if transport.username:
            smtp.login(transport.username, transport.password)
        refused = smtp.send_message(
            message,
            from_addr=transport.from_email,
            to_addrs=[to_email],
        )
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)


def send_email(to_email: str, subject: str, body: str) -> EmailDelivery:
    transports = _transports()
    if not transports[0].host or not transports[0].from_email:
        raise RuntimeError("SMTP settings are not configured")

    msg = EmailMessage()
    sender_domain = transports[0].from_email.rsplit("@", 1)[-1]
    message_id = make_msgid(domain=sender_domain)
    msg["From"] = formataddr((settings.SMTP_FROM_NAME, transports[0].from_email))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = message_id
    msg["Auto-Submitted"] = "auto-generated"
    msg.set_content(body)

    last_error: Exception | None = None
    total_attempts = 0
    for transport in transports:
        # The visible From header must match the active provider's verified sender.
        msg.replace_header("From", formataddr((settings.SMTP_FROM_NAME, transport.from_email)))
        for attempt in range(1, settings.SMTP_RETRY_ATTEMPTS + 1):
            total_attempts += 1
            try:
                _send_once(transport, msg, to_email)
                logger.info(
                    "Email accepted by %s SMTP for %s; message_id=%s attempt=%s",
                    transport.name,
                    to_email,
                    message_id,
                    attempt,
                )
                return EmailDelivery(message_id, transport.name, total_attempts)
            except Exception as error:
                last_error = error
                retry = _is_retryable(error) and attempt < settings.SMTP_RETRY_ATTEMPTS
                logger.warning(
                    "Email attempt failed via %s for %s; message_id=%s attempt=%s retry=%s error=%s",
                    transport.name,
                    to_email,
                    message_id,
                    attempt,
                    retry,
                    error,
                )
                if not retry:
                    break
                time.sleep(settings.SMTP_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))

    assert last_error is not None
    raise last_error


def try_send_email(to_email: str, subject: str, body: str) -> None:
    try:
        send_email(to_email, subject, body)
    except Exception as exc:
        logger.warning("Email send failed for %s: %s", to_email, exc, exc_info=True)
