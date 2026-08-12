"""Invio delle notifiche: prova manuale, digest delle scadenze, worker.

Riferimento: BACKEND-PLAN.md §8.38 (prova manuale) e §8.41 (worker).
"""
from app.notifications.digest import build_digest, sanitise_field
from app.notifications.expiry import (
    EXPIRY_KINDS,
    DueItem,
    applicable_thresholds,
    due_items,
    local_today,
    parse_expiry,
)
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
    deliver,
    send_test_message,
)

__all__ = [
    "EXPIRY_KINDS", "MAX_TEST_RECIPIENTS", "DueItem", "NoRecipients",
    "NotificationLimiterUnavailable", "NotificationTestRateLimited",
    "SendOutcome", "SmtpNotConfigured", "SmtpSendFailed",
    "applicable_thresholds", "build_digest", "build_test_message",
    "choose_test_recipients", "deliver", "due_items", "local_today",
    "parse_expiry", "reserve_slot", "sanitise_field", "send_test_message",
]
