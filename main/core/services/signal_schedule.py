"""实盘信号频率/时点规则（API 校验与 SignalScheduler 共用）。"""
from datetime import time
from typing import Optional

VALID_SIGNAL_FREQUENCIES = (
    "1m", "5m", "15m", "30m", "1h", "1d", "1w", "1mon",
)
_RANK = {
    "1m": 0, "5m": 1, "15m": 2, "30m": 3, "1h": 4,
    "1d": 5, "1w": 6, "1mon": 7,
}
_DEFAULT_AT = {"1d": "14:20", "1w": "09:00", "1mon": "09:00"}


def default_signal_at(frequency: str) -> str:
    return _DEFAULT_AT.get(frequency, "14:30")


def default_signal_data_end(frequency: str) -> str:
    return "completed" if frequency in ("1w", "1mon") else "current"


def effective_signal_frequency(period: str, frequency: Optional[str]) -> str:
    return frequency or period


def effective_signal_data_end(frequency: str, data_end: Optional[str]) -> str:
    return data_end or default_signal_data_end(frequency)


def parse_signal_at(value: Optional[str], frequency: str) -> time:
    raw = value or default_signal_at(frequency)
    try:
        hh, mm = raw.split(":")
        return time(int(hh), int(mm))
    except (TypeError, ValueError):
        return time(14, 20)


def validate_signal_config(
    period: str,
    frequency: Optional[str],
    signal_at: Optional[str],
    signal_weekday: Optional[int],
    data_end: Optional[str],
) -> Optional[str]:
    if period not in _RANK:
        return "period 非法：%s" % period
    freq = frequency or period
    if freq not in VALID_SIGNAL_FREQUENCIES:
        return "signal_frequency 必须为 %s，收到 %s" % (
            list(VALID_SIGNAL_FREQUENCIES), freq)
    if _RANK[freq] < _RANK[period]:
        return "signal_frequency 不能比 period 更细（period=%s, frequency=%s）" % (
            period, freq)
    at = signal_at or default_signal_at(freq)
    try:
        hh, mm = at.split(":")
        if len(hh) != 2 or len(mm) != 2:
            raise ValueError
        time(int(hh), int(mm))
    except (TypeError, ValueError):
        return "signal_at 必须为 HH:MM，收到 %s" % at
    if signal_weekday is not None and not (1 <= signal_weekday <= 5):
        return "signal_weekday 必须为 1-5，收到 %s" % signal_weekday
    if data_end is not None and data_end not in ("completed", "current"):
        return "signal_data_end 必须为 completed/current，收到 %s" % data_end
    if freq in ("1w", "1mon") and data_end == "current":
        return "%s 只允许 signal_data_end=completed，禁止未完成周/月 bar" % freq
    return None
