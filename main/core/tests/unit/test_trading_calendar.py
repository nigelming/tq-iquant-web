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
from core.engine.live.calendar import BUILTIN_CLOSURES


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


def test_provider_datetime_items_coerced_to_date():
    """provider 返回 datetime（date 子类）时须归一成 date，否则集合查找永不命中。"""
    cal = TradingCalendar(_provider([datetime(2026, 8, 24, 0, 0), date(2026, 8, 26)]))
    assert cal.is_trading_day(date(2026, 8, 24)) is True
    # 08-25 在数据源覆盖范围内（08-24..08-26）却不在集合 → 非交易日
    assert cal.is_trading_day(date(2026, 8, 25)) is False


def test_weekday_holiday_not_in_calendar_is_non_trading():
    # 工作日但不在交易日集合（如国庆假期内的调休工作日）→ 非交易日
    cal = TradingCalendar(_provider([date(2026, 10, 9)]))
    assert cal.is_trading_day(date(2026, 10, 1)) is False  # 国庆


def test_provider_failure_fail_open_on_weekday(caplog):
    """桥离线：工作日默认交易日（fail-open/内置表），并记录一次告警（不静默）。"""
    caplog.set_level("WARNING", logger="core.engine.live.calendar")
    def boom():
        raise RuntimeError("bridge offline")
    cal = TradingCalendar(boom)
    # 周一，无数据 → 放行（不误挡）
    assert cal.is_trading_day(date(2026, 8, 24)) is True
    # 周末仍硬判非交易日（不依赖 provider）
    assert cal.is_trading_day(date(2026, 8, 22)) is False
    # 告警可见：异常原文 + 退化来源进日志
    assert any("bridge offline" in rec.message for rec in caplog.records)
    assert any("交易日历" in rec.message for rec in caplog.records)
    assert cal.status()["degraded"] is True


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


# ---------------- 2026-11-05 修复：日历格式 / 多源回退 / 退化可见 ----------------

def test_iso_dashed_date_strings_are_parsed():
    """掘金桥 /calendar 返回 'YYYY-MM-DD'：必须解析（原实现只认 'YYYYMMDD' → 全丢）。"""
    cal = TradingCalendar(_provider(["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]))
    assert cal.is_trading_day(date(2026, 11, 2)) is True
    assert cal.is_trading_day(date(2026, 10, 1)) is False  # 国庆（内置表拦下）
    assert cal.status()["degraded"] is False
    assert cal.status()["years"]["2026"]["source"] == "primary"
    assert cal.status()["years"]["2026"]["trading_days"] == 4


def test_other_date_formats_are_parsed():
    cal = TradingCalendar(_provider(["2026/01/05", "2026-01-06 00:00:00", "20260107"]))
    assert cal.is_trading_day(date(2026, 1, 5)) is True
    assert cal.is_trading_day(date(2026, 1, 6)) is True
    assert cal.is_trading_day(date(2026, 1, 7)) is True


def test_primary_empty_falls_back_to_secondary():
    cal = TradingCalendar(
        _provider([]),
        fallback_providers=[("goldminer_bridge", _provider(["2026-11-02"]))],
    )
    assert cal.is_trading_day(date(2026, 11, 2)) is True
    assert cal.status()["degraded"] is False
    assert cal.status()["years"]["2026"]["source"] == "goldminer_bridge"


def test_year_aware_fallback_provider_receives_year():
    seen = []

    def gm(year):
        seen.append(year)
        return ["2026-01-05"]

    cal = TradingCalendar(_provider([]), fallback_providers=[("goldminer_bridge", gm)])
    cal.is_trading_day(date(2026, 1, 5))
    assert seen == [2026]


def test_all_sources_empty_use_builtin_table_and_warn_once(caplog):
    """全部源为空 → 内置官方休市表 + 一条 WARNING + 一条 risk 事件（不刷屏）。"""
    caplog.set_level("WARNING", logger="core.engine.live.calendar")
    events = []
    cal = TradingCalendar(_provider([]), fallback_providers=[("goldminer_bridge", _provider([]))])
    cal.on_degraded = events.append

    assert cal.is_trading_day(date(2026, 10, 1)) is False  # 国庆（内置表休市）
    assert cal.is_trading_day(date(2026, 11, 6)) is True   # 普通工作日放行
    assert cal.status()["degraded"] is True
    assert cal.status()["years"]["2026"]["source"] == "builtin"
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1
    assert len(events) == 1
    assert events[0]["rule"] == "trading_calendar_degraded"

    # 同年重复判定不再新增告警/事件
    cal.is_trading_day(date(2026, 12, 1))
    assert len([r for r in caplog.records if r.levelname == "WARNING"]) == 1
    assert len(events) == 1


def test_uncovered_year_fail_open_with_warning(caplog):
    """内置表未收录年份（无权威数据）→ fail-open（工作日放行）+ WARNING。"""
    caplog.set_level("WARNING", logger="core.engine.live.calendar")
    cal = TradingCalendar(_provider([]))
    assert cal.is_trading_day(date(2030, 1, 7)) is True  # 周一
    assert cal.status()["years"]["2030"]["source"] == "fail_open"
    assert any("fail-open" in r.message for r in caplog.records)


def test_date_beyond_provider_coverage_not_misjudged_as_closed():
    """桥日历只给到"今天"：覆盖范围外的工作日不得因"不在集合"被判休市。"""
    cal = TradingCalendar(
        _provider(["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05"]))
    assert cal.is_trading_day(date(2026, 11, 6)) is True   # 覆盖外普通工作日
    assert cal.is_trading_day(date(2026, 12, 31)) is True
    assert cal.is_trading_day(date(2026, 10, 1)) is False  # 覆盖外但内置表是节假日


def test_empty_result_not_cached_forever():
    """退化结果不被永久缓存：退避窗后重试，源恢复即自动升级回权威集合。"""
    now = [1000.0]
    calls = {"n": 0}
    state = {"ok": False}

    def prov():
        calls["n"] += 1
        return ["2026-08-24"] if state["ok"] else []

    cal = TradingCalendar(prov, retry_after_seconds=60.0, clock=lambda: now[0])
    assert cal.is_trading_day(date(2026, 8, 24)) is True
    assert cal.status()["degraded"] is True

    cal.is_trading_day(date(2026, 8, 25))  # 退避窗内 → 不再打源
    assert calls["n"] == 1

    state["ok"] = True
    now[0] += 61.0
    assert cal.is_trading_day(date(2026, 8, 24)) is True
    assert calls["n"] == 2
    assert cal.status()["degraded"] is False
    assert cal.status()["years"]["2026"]["source"] == "primary"


def test_builtin_2026_matches_official_schedule():
    """内置表与上交所 2026 年休市安排一致（抽查节假日与复市日）。"""
    closed = BUILTIN_CLOSURES[2026]
    for d in (date(2026, 1, 1), date(2026, 2, 17), date(2026, 4, 6),
              date(2026, 5, 5), date(2026, 6, 19), date(2026, 9, 25),
              date(2026, 10, 7)):
        assert d in closed
    for d in (date(2026, 1, 5), date(2026, 2, 24), date(2026, 4, 7),
              date(2026, 5, 6), date(2026, 6, 22), date(2026, 9, 28),
              date(2026, 10, 8)):
        assert d not in closed
