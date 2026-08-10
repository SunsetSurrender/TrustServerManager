"""Invio delle notifiche: prova manuale adesso, scheduler in un commit separato.

Riferimento: BACKEND-PLAN.md §8.38.
"""
from app.notifications.ratelimit import (
    NotificationLimiterUnavailable,
    NotificationTestRateLimited,
    reserve_slot,
)
from app.notifications.smtp import (
    MAX_TEST_RECIPIENTS,
    NoRecipients,
    SendOutcome,
    SmtpNotConfigured,
    SmtpSendFailed,
    build_test_message,
    choose_test_recipients,
    send_test_message,
)

__all__ = [
    "MAX_TEST_RECIPIENTS", "NoRecipients", "NotificationLimiterUnavailable",
    "NotificationTestRateLimited", "SendOutcome", "SmtpNotConfigured",
    "SmtpSendFailed", "build_test_message", "choose_test_recipients",
    "reserve_slot", "send_test_message",
]
