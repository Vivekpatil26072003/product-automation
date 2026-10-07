"""Company settings defaults (stored overrides live in tenant.settings)."""

from typing import Any

# Engineering defaults from spec §13 that require business approval (decision D7/D10).
DEFAULTS: dict[str, Any] = {
    "retention_days": {"source": 180, "business": 365, "logs": 30},
    "daily_spend_limit": None,  # decimal string in company currency; None = not configured
    "submission_cutoff_local_time": "18:00",  # A2 expected daily submission cutoff
    "working_days": [1, 2, 3, 4, 5, 6],  # ISO weekdays; A3 reminders honour these
    "feature_flags": {"scheduling": False, "auto_send": False, "erp_integration": False},
    # Email domains treated as internal; recipients elsewhere get an external-domain warning (D9).
    "internal_email_domains": [],
    # A1 exception thresholds: achievement outside [low, high] % or stop minutes at/above the limit, recent days.
    "exception_rules": {
        "low_achievement_pct": 50,
        "high_achievement_pct": 150,
        "stop_minutes": 240,
        "lookback_days": 7,
    },
    # A3 reminders for missing daily submissions, in minutes after the submission cutoff. Off until enabled.
    "reminders": {
        "enabled": False,
        "first_after_minutes": 0,
        "second_after_minutes": 60,
        "escalate_after_minutes": 120,
        "email": False,
    },
}
