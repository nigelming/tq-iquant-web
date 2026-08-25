"""交易日历与交易总开关单元测试。

权威交易日历来自桥 xtdata.get_trading_dates（/calendar 端点）。设计原则：
- 周末（周六/周日）永远非交易日（A 股硬规则，不依赖数据）。
- 工作日是否交易日由桥返回的权威集合判定（节假日）。
- 桥离线/无数据时 fail-open：工作日默认交易日（绝不因日历过期误挡真实交易日），
  仅记录一次告警。资金安全靠 is_trading_allowed 的时段门 + 15:00 收盘守卫兜底。
"""
from datetime import date, datetime

import pytest

from core.engine.trading_calendar import TradingCalendar


# 2026 年部分交易日（真实日历，用于断言；桥 provider 一般返回整年）
_2026_TRADING = [
    date(2026, 1, 5),    # 周一
    date(2026, 1, 6),    # 周二
    date(2026, 10, 9),    # 国庆后
    date(2026, 12, 31),
]


def _provider(dates):
    """构造一个返回指定交易日列表的 provider。"""
    def _p():
        return list(dates)
    return _p


def test_weekend_is_never_trading_day():
    cal = TradingCalendar(_provider(_2026_TRADING))
    # 2026-08-22 周六、08-23 周日
    assert cal.is_trading_day(date(2026, 8, 22)) is False
    assert cal.is_trading_day(date(2026, 8, 23)) is False


def test_weekday_in_calendar_is_trading_day():
    cal = TradingCalendar(_provider([date(2026, 8, 24)]))  # 周一
    assert cal.is_trading_day(date(2026, 8, 24)) is True


def test_weekday_holiday_not_in_calendar_is_non_trading():
    # 工作日但不在交易日集合（如国庆假期内的调休工作日）→ 非交易日
    cal = TradingCalendar(_provider([date(2026, 10, 9)]))
    assert cal.is_trading_day(date(2026, 10, 1)) is False  # 国庆


def test_provider_failure_fail_open_on_weekday(caplog):
    """桥离线：工作日默认交易日（fail-open），并记录一次日志（现 INFO，非告警）。"""
    caplog.set_level("INFO", logger="core.engine.trading_calendar")
    def boom():
        raise RuntimeError("bridge offline")
    cal = TradingCalendar(boom)
    # 周一，无数据 → 放行（不误挡）
    assert cal.is_trading_day(date(2026, 8, 24)) is True
    # 周末仍硬判非交易日（不依赖 provider）
    assert cal.is_trading_day(date(2026, 8, 22)) is False
    assert any("calendar" in rec.message.lower() or "trading" in rec.message.lower()
               for rec in caplog.records)


def test_provider_returns_empty_fail_open_on_weekday():
    """provider 返回空（桥 404/老桥无 /calendar）：工作日 fail-open。"""
    cal = TradingCalendar(_provider([]))
    assert cal.is_trading_day(date(2026, 8, 24)) is True
    assert cal.is_trading_day(date(2026, 8, 22)) is False


def test_calendar_caches_per_year_single_provider_call():
    """同一自然年只拉一次日历（缓存）。"""
    calls = {"n": 0}
    def counting_provider():
        calls["n"] += 1
        return [date(2026, 8, 24), date(2026, 8, 25)]
    cal = TradingCalendar(counting_provider)
    cal.is_trading_day(date(2026, 8, 24))
    cal.is_trading_day(date(2026, 8, 25))
    cal.is_trading_day(date(2026, 8, 24))
    assert calls["n"] == 1


# ---------------- is_trading_allowed 总开关 ----------------

def test_trading_allowed_during_session():
    cal = TradingCalendar(_provider([date(2026, 8, 24)]))
    assert cal.is_trading_allowed(datetime(2026, 8, 24, 9, 30)) is True
    assert cal.is_trading_allowed(datetime(2026, 8, 24, 10, 0)) is True
    assert cal.is_trading_allowed(datetime(2026, 8, 24, 14, 59)) is True


def test_trading_blocked_before_open():
    cal = TradingCalendar(_provider([date(2026, 8, 24)]))
    assert cal.is_trading_allowed(datetime(2026, 8, 24, 9, 0)) is False
    assert cal.is_trading_allowed(datetime(2026, 8, 24, 9, 29)) is False


def test_trading_blocked_at_and_after_close():
    cal = TradingCalendar(_provider([date(2026, 8, 24)]))
    assert cal.is_trading_allowed(datetime(2026, 8, 24, 15, 0)) is False
    assert cal.is_trading_allowed(datetime(2026, 8, 24, 15, 30)) is False


def test_trading_blocked_on_weekend_even_at_session_time():
    # 周六 10:00 —— 时段对但非交易日，禁止
    cal = TradingCalendar(_provider([]))
    assert cal.is_trading_allowed(datetime(2026, 8, 22, 10, 0)) is False


def test_trading_blocked_on_holiday_at_session_time():
    cal = TradingCalendar(_provider([date(2026, 10, 9)]))
    assert cal.is_trading_allowed(datetime(2026, 10, 1, 10, 0)) is False  # 国庆


def test_trading_allowed_fail_open_weekday_when_provider_down():
    """桥离线时工作日交易时段内仍放行（fail-open）——不误挡真实交易日。"""
    def boom():
        raise RuntimeError("offline")
    cal = TradingCalendar(boom)
    assert cal.is_trading_allowed(datetime(2026, 8, 24, 10, 0)) is True
