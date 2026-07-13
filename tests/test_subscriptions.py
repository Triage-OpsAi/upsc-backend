from datetime import datetime, timedelta, timezone

from app.subscriptions import access_state_from_row


def test_trial_access_reports_rounded_up_days_and_founder_price():
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    state = access_state_from_row(
        {
            "trial_ends_at": now + timedelta(days=2, minutes=1),
            "subscription_status": "trial",
            "early_offer_number": 42,
        },
        now=now,
    )

    assert state.status == "trial"
    assert state.has_content_access is True
    assert state.trial_days_remaining == 3
    assert state.early_offer_eligible is True
    assert state.monthly_price_inr == 99


def test_expired_trial_has_no_content_access():
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    state = access_state_from_row(
        {
            "trial_ends_at": now - timedelta(seconds=1),
            "subscription_status": "trial",
            "early_offer_number": 501,
        },
        now=now,
    )

    assert state.status == "expired"
    assert state.has_content_access is False
    assert state.trial_days_remaining == 0
    assert state.early_offer_eligible is False
    assert state.monthly_price_inr == 299


def test_active_subscription_keeps_access_after_trial_end():
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    state = access_state_from_row(
        {
            "trial_ends_at": now - timedelta(days=30),
            "subscription_status": "active",
            "early_offer_number": 7,
        },
        now=now,
    )

    assert state.status == "active"
    assert state.has_content_access is True
