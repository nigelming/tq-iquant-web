from datetime import time

from core.services.signal_schedule import (
    effective_signal_data_end, effective_signal_frequency, parse_signal_at,
    validate_signal_config,
)


def test_effective_and_defaults():
    assert effective_signal_frequency("1d", None) == "1d"
    assert effective_signal_frequency("1d", "1w") == "1w"
    assert effective_signal_data_end("1w", None) == "completed"
    assert effective_signal_data_end("1d", None) == "current"
    assert parse_signal_at(None, "1w") == time(9, 0)
    assert parse_signal_at(None, "1d") == time(14, 30)


def test_frequency_cannot_be_finer_than_period():
    assert validate_signal_config("1d", "1m", None, None, None)
    assert validate_signal_config("1d", "1w", "09:00", 1, "completed") is None
    assert validate_signal_config("5m", "1m", None, None, None)


def test_weekly_and_monthly_require_completed():
    assert validate_signal_config("1d", "1w", "09:00", 1, "current")
    assert validate_signal_config("1d", "1mon", "09:00", None, None) is None
    assert validate_signal_config("1d", "1w", "9:00", 1, None)
    assert validate_signal_config("1d", "1w", "09:00", 6, None)
