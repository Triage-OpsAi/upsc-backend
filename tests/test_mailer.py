import smtplib
from unittest.mock import patch

import pytest

from app import mailer


SETTINGS = {
    "SMTP_HOST": "smtp.primary.test",
    "SMTP_PORT": 587,
    "SMTP_USERNAME": "sender@example.com",
    "SMTP_PASSWORD": "secret",
    "SMTP_FROM_EMAIL": "sender@example.com",
    "SMTP_FROM_NAME": "AspirantOS",
    "SMTP_USE_TLS": True,
    "SMTP_USE_SSL": False,
    "SMTP_TIMEOUT_SECONDS": 20,
    "SMTP_RETRY_ATTEMPTS": 3,
    "SMTP_RETRY_BASE_SECONDS": 0,
    "SMTP_FALLBACK_HOST": "",
    "SMTP_FALLBACK_PORT": 587,
    "SMTP_FALLBACK_USERNAME": "",
    "SMTP_FALLBACK_PASSWORD": "",
    "SMTP_FALLBACK_FROM_EMAIL": "sender@example.com",
    "SMTP_FALLBACK_USE_TLS": True,
    "SMTP_FALLBACK_USE_SSL": False,
}


def test_transient_smtp_failure_is_retried():
    transient = smtplib.SMTPConnectError(421, b"temporarily unavailable")
    with (
        patch.multiple(mailer.settings, **SETTINGS),
        patch.object(mailer, "_send_once", side_effect=[transient, None]) as send_once,
        patch.object(mailer.time, "sleep") as sleep,
    ):
        delivery = mailer.send_email("student@example.com", "OTP", "123456")

    assert delivery.provider == "primary"
    assert delivery.attempts == 2
    assert send_once.call_count == 2
    sleep.assert_called_once_with(0)


def test_permanent_authentication_failure_is_not_retried():
    permanent = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    with (
        patch.multiple(mailer.settings, **SETTINGS),
        patch.object(mailer, "_send_once", side_effect=permanent) as send_once,
        patch.object(mailer.time, "sleep") as sleep,
        pytest.raises(smtplib.SMTPAuthenticationError),
    ):
        mailer.send_email("student@example.com", "OTP", "123456")

    send_once.assert_called_once()
    sleep.assert_not_called()


def test_fallback_provider_is_used_after_primary_fails():
    settings = {
        **SETTINGS,
        "SMTP_FALLBACK_HOST": "smtp.fallback.test",
        "SMTP_FALLBACK_USERNAME": "fallback@example.com",
        "SMTP_FALLBACK_PASSWORD": "fallback-secret",
        "SMTP_FALLBACK_FROM_EMAIL": "fallback@example.com",
    }
    permanent = smtplib.SMTPAuthenticationError(535, b"bad credentials")
    with (
        patch.multiple(mailer.settings, **settings),
        patch.object(mailer, "_send_once", side_effect=[permanent, None]) as send_once,
    ):
        delivery = mailer.send_email("student@example.com", "OTP", "123456")

    assert delivery.provider == "fallback"
    assert delivery.attempts == 2
    assert [item.args[0].name for item in send_once.call_args_list] == ["primary", "fallback"]


def test_retry_classification_distinguishes_4xx_and_5xx():
    assert mailer._is_retryable(smtplib.SMTPConnectError(421, b"try later"))
    assert not mailer._is_retryable(smtplib.SMTPAuthenticationError(535, b"denied"))
