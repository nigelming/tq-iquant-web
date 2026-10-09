from datetime import date, timedelta
from decimal import Decimal

from core.engine.risk_manager import PortfolioRiskManager


def _pm(max_drawdown=Decimal("0.2"), daily_loss_limit=Decimal("0.05")):
    return PortfolioRiskManager(max_drawdown=max_drawdown, daily_loss_limit=daily_loss_limit)


# ===========================================================================
# update(total_value, current_date, initial_capital) — 每根 bar 日终调用
# 时序：Day N 收盘检测 → 触发后暂停 Day N+1 整日 → Day N+1 收盘恢复 → Day N+2 复盘。
# 即「次日暂停，第三日恢复」= 暂停窗口恰 1 个交易日。
# ===========================================================================
def test_max_drawdown_triggers_breaker_next_day_recovery():
    """peak=100000，value=79000（回撤 21% ≥ 20%）→ Day2 update 熔断；
    暂停次日（Day3 开盘 on_bar 剥 BUY）；Day3 收盘 value 回升不再回撤 → 恢复。
    注：回撤持续存在时恢复后会立即再次触发——故恢复测试需 value 回到峰值附近。"""
    pm = _pm()
    initial = Decimal("100000")

    pm.update(Decimal("100000"), date(2026, 7, 1), initial)  # 建峰
    pm.update(Decimal("79000"), date(2026, 7, 2), initial)   # 回撤 21% → 熔断
    assert pm.circuit_breaker_active is True
    assert pm.breaker_trigger_date == date(2026, 7, 2)
    assert pm.consecutive_drawdown_triggers == 1

    # Day3 收盘：value 回升到 99000（仍破阈但接近峰值，回撤 1% < 20%）→ 恢复不重触发
    pm.update(Decimal("99000"), date(2026, 7, 3), initial)
    assert pm.circuit_breaker_active is False


def test_max_drawdown_three_triggers_manual_recovery():
    """累计触发 3 次 → manual_recovery=True，后续 update 不再自动恢复。"""
    pm = _pm()
    initial = Decimal("100000")
    # 反复触发：每次从 100000 回撤到 79000（21%）
    trigger_days = [date(2026, 7, 2), date(2026, 7, 4), date(2026, 7, 6)]
    for td in trigger_days:
        pm.update(Decimal("100000"), td - timedelta(days=1), initial)  # 建峰
        pm.update(Decimal("79000"), td, initial)                       # 触发
    # 第 3 次触发后转手动
    assert pm.consecutive_drawdown_triggers == 3
    assert pm.manual_recovery is True
    assert pm.circuit_breaker_active is True
    # 后续即便 date > trigger 也不自动恢复
    pm.update(Decimal("100000"), date(2026, 7, 10), initial)
    assert pm.circuit_breaker_active is True


def test_daily_loss_triggers_pause_next_day_recovery():
    """日内亏损 ≥ daily_loss_limit → daily_pause_active=True，次日恢复。
    initial=100000, limit=5% → 日内亏 6000(6%) 触发。
    新语义（#8）：update 跨日补算上一日 daily_loss，最后一日由 finalize_last_day 收尾。"""
    pm = _pm(daily_loss_limit=Decimal("0.05"))
    initial = Decimal("100000")

    pm.update(Decimal("100000"), date(2026, 7, 1), initial)  # 首日基线
    pm.update(Decimal("94000"), date(2026, 7, 2), initial)   # 跨日补算 D1(pnl=0)，建立 D2 基准
    # 循环结束补算最后一日（D2 日内亏 6000=6% ≥ 5%）
    pm.finalize_last_day(initial)
    assert pm.daily_pause_active is True
    assert pm.daily_loss_trigger_date == date(2026, 7, 2)

    # 次日 update → update_peak 跨日恢复 daily_pause（7/3 > 7/2）
    pm.update(Decimal("93000"), date(2026, 7, 3), initial)
    assert pm.daily_pause_active is False


def test_is_trading_halted_combines_breaker_and_daily_pause():
    """is_trading_halted = circuit_breaker_active OR daily_pause_active。"""
    pm = _pm()
    initial = Decimal("100000")
    assert pm.is_trading_halted() is False

    pm.update(Decimal("100000"), date(2026, 7, 1), initial)
    pm.update(Decimal("79000"), date(2026, 7, 2), initial)
    assert pm.is_trading_halted() is True  # 熔断


# ===========================================================================
# 实盘拆分（E5/E6, 2026-08-10 定）：update_peak 每 bar（回撤+熔断+跨日刷新 prev_close），
# update_daily 14:30 一次（日内亏损）。update = 两者合集，回测向后兼容。
# ===========================================================================
def test_update_peak_cross_day_refreshes_prev_close():
    """分钟级实盘：update_peak 每 bar 调，跨日时 prev_close_value 刷新为昨日最后一根 bar 的 total_value。

    Day1 最后一根 bar total=100000（首日无昨收基准 → prev_close 保持 None）；
    Day2 第一根 bar total=101000 → 跨日 → prev_close 刷新为 100000；
    Day2 后续 bar 不跨日 → prev_close 保持 100000（daily_pnl 基准不随分钟抖动）。"""
    pm = _pm()
    # Day1 盘中多根 bar，最后一根 100000（首日 prev_close 无值）
    pm.update_peak(Decimal("99000"), date(2026, 7, 1))
    pm.update_peak(Decimal("100000"), date(2026, 7, 1))   # 当日最后 bar
    assert pm.prev_close_value is None                     # 首日无昨收
    assert pm._last_bar_date == date(2026, 7, 1)
    assert pm._last_bar_total_value == Decimal("100000")

    # Day2 第一根：跨日 → prev_close 刷新为昨日最后值
    pm.update_peak(Decimal("101000"), date(2026, 7, 2))
    assert pm.prev_close_value == Decimal("100000")
    assert pm._last_bar_date == date(2026, 7, 2)
    assert pm.peak_value == Decimal("101000")

    # Day2 后续 bar：不跨日，prev_close 不变
    pm.update_peak(Decimal("102000"), date(2026, 7, 2))
    assert pm.prev_close_value == Decimal("100000")


def test_update_daily_uses_prev_close_and_triggers_daily_loss():
    """update_daily 用 prev_close（昨日收盘，由当首根 update_peak 跨日刷新）算日内盈亏。

    Day1 收盘 100000；Day2 首根 update_peak 跨日 → prev_close=100000；
    Day2 14:30 update_daily total=94000 → 日内亏 6000=6% ≥ 5% → daily_pause。"""
    pm = _pm(daily_loss_limit=Decimal("0.05"))
    initial = Decimal("100000")
    # Day1 收盘 100000（建立 _last_bar_total_value）
    pm.update_peak(Decimal("100000"), date(2026, 7, 1))
    # Day2 盘中每 bar update_peak，最后一根收盘 94000（触发 update_peak 记录当日最后 total）
    pm.update_peak(Decimal("100000"), date(2026, 7, 2))   # 跨日：prev_close = 100000
    pm.update_peak(Decimal("96000"), date(2026, 7, 2))    # 盘中下跌
    pm.update_peak(Decimal("94000"), date(2026, 7, 2))    # 当日最后 bar 收盘
    # Day2 14:30 一次 update_daily：日内亏 6000=6% ≥ 5% → daily_pause
    pm.update_daily(Decimal("94000"), date(2026, 7, 2), initial)
    assert pm.daily_pause_active is True
    assert pm.daily_loss_trigger_date == date(2026, 7, 2)

    # 次日：首根 update_peak 跨日刷新 prev_close = Day2 最后 bar total(94000)，再 update_daily → date > trigger → 恢复
    pm.update_peak(Decimal("93000"), date(2026, 7, 3))   # 跨日：prev_close = 94000
    pm.update_daily(Decimal("93000"), date(2026, 7, 3), initial)
    assert pm.daily_pause_active is False


def test_update_daily_no_minute_jitter():
    """分钟级不误触：update_peak 每 bar 更新 peak，但 daily_pnl 只由 update_daily 用 prev_close 算。

    盘中 minute 波动（-2%）不触发 daily_loss（否则分钟抖动会误触发）。"""
    pm = _pm(daily_loss_limit=Decimal("0.05"))
    initial = Decimal("100000")
    pm.update_peak(Decimal("100000"), date(2026, 7, 1))    # 昨日收盘
    # 今日盘中：每 bar update_peak，虽有分钟下跌但不触发 daily_pause
    pm.update_peak(Decimal("99000"), date(2026, 7, 2))
    pm.update_peak(Decimal("98500"), date(2026, 7, 2))     # 日内 -1.5%
    assert pm.daily_pause_active is False                  # 未到 14:30，不算 daily_loss
    # 14:30 一次 update_daily：日内亏 1.5% < 5% → 不触发
    pm.update_daily(Decimal("98500"), date(2026, 7, 2), initial)
    assert pm.daily_pause_active is False


def test_update_peak_breaker_minute_scale():
    """分钟级熔断：update_peak 每 bar 检测 max_drawdown（不依赖 daily）。

    peak=100000 → total=79000（回撤 21%）→ 熔断；次日恢复。"""
    pm = _pm()
    initial = Decimal("100000")
    pm.update_peak(Decimal("100000"), date(2026, 7, 1))
    pm.update_peak(Decimal("79000"), date(2026, 7, 2))
    assert pm.circuit_breaker_active is True
    assert pm.consecutive_drawdown_triggers == 1

    # 次日 value 回升 → 恢复（current_date > trigger_date）
    pm.update_peak(Decimal("99000"), date(2026, 7, 3))
    assert pm.circuit_breaker_active is False


def test_update_daily_line_cross_day_compat():
    """日线回测每 bar 跨日：update 跨日补算上一日 daily_loss + update_peak 维护峰值/熔断。

    日线语义与旧 update = update_peak + update_daily 等价（每 bar 跨日 → 补算上一日 = 逐日检查）。"""
    pm = _pm()
    initial = Decimal("100000")
    # 与现有 test_max_drawdown_triggers_breaker_next_day_recovery 相同序列
    pm.update(Decimal("100000"), date(2026, 7, 1), initial)  # 建峰 + prev_close
    pm.update(Decimal("79000"), date(2026, 7, 2), initial)   # 回撤 21% → 熔断
    assert pm.circuit_breaker_active is True
    assert pm.breaker_trigger_date == date(2026, 7, 2)
    pm.update(Decimal("99000"), date(2026, 7, 3), initial)   # 回升 → 恢复
    assert pm.circuit_breaker_active is False


# ===========================================================================
# #8 修复：分钟回测 daily_loss 时点对齐实盘
# update() 跨日补算上一日，同日仅 update_peak；循环结束 finalize_last_day 收尾。
# daily_pause 次日恢复移至 update_peak（每 bar 检测，不依赖 update_daily 调用时点）。
# ===========================================================================
def test_update_minute_same_day_no_daily_loss_jitter():
    """分钟回测同日多 bar：update 只调 update_peak，不误触 daily_loss。

    盘中下跌 6%（超 5% 阈值）不触发 daily_pause —— 日内亏损只在日终（跨日补算或收尾）算。
    """
    pm = _pm(daily_loss_limit=Decimal("0.05"))
    initial = Decimal("100000")
    # Day1 收盘 100000（建立基准）
    pm.update(Decimal("100000"), date(2026, 7, 1), initial)
    # Day2 盘中多 bar：09:30 跌到 94000（-6%）→ 不触发（同日仅 update_peak）
    pm.update(Decimal("94000"), date(2026, 7, 2), initial)  # 跨日补算 D1(pnl=0) + peak
    pm.update(Decimal("94000"), date(2026, 7, 2), initial)  # 同日：仅 peak
    assert pm.daily_pause_active is False  # 日内亏损未在盘中触发


def test_update_cross_day_finalizes_previous_daily_loss():
    """跨日时补算上一日 daily_loss：Day2 收盘 94000 → Day3 首根 update 触发 D2 日内亏损。

    D1=100000, D2 收盘=94000（-6%）→ D3 跨日补算 update_daily(94000, D2) → 触发。
    """
    pm = _pm(daily_loss_limit=Decimal("0.05"))
    initial = Decimal("100000")
    pm.update(Decimal("100000"), date(2026, 7, 1), initial)  # D1
    pm.update(Decimal("94000"), date(2026, 7, 2), initial)   # D2: 跨日补算 D1(pnl=0)
    assert pm.daily_pause_active is False  # D2 尚未补算
    pm.update(Decimal("93000"), date(2026, 7, 3), initial)   # D3: 跨日补算 D2 → 触发!
    assert pm.daily_pause_active is True
    assert pm.daily_loss_trigger_date == date(2026, 7, 2)


def test_finalize_last_day_checks_closing_day():
    """循环结束补算最后一日：无后续跨日时由 finalize_last_day 收尾。

    D1=100000, D2=94000 → update 跨日补算 D1, finalize_last_day 补算 D2（-6%）→ 触发。
    """
    pm = _pm(daily_loss_limit=Decimal("0.05"))
    initial = Decimal("100000")
    pm.update(Decimal("100000"), date(2026, 7, 1), initial)
    pm.update(Decimal("94000"), date(2026, 7, 2), initial)
    assert pm.daily_pause_active is False  # D2 尚未补算
    pm.finalize_last_day(initial)
    assert pm.daily_pause_active is True
    assert pm.daily_loss_trigger_date == date(2026, 7, 2)


def test_finalize_last_day_skips_empty():
    """无 bar（空回测）时 finalize_last_day 不报错。"""
    pm = _pm()
    pm.finalize_last_day(Decimal("100000"))  # _last_bar_date=None → 空操作


def test_daily_pause_recovers_next_day_via_cross_day_update():
    """daily_pause 次日恢复由 update_daily 跨日补算时处理（recovery_date = 当日）。

    D2 触发 daily_loss → D3 跨日 update_daily(prev=D2, recovery_date=D3) → D3 > D2 → 恢复。
    """
    pm = _pm(daily_loss_limit=Decimal("0.05"))
    initial = Decimal("100000")
    pm.update(Decimal("100000"), date(2026, 7, 1), initial)
    pm.update(Decimal("94000"), date(2026, 7, 2), initial)
    pm.finalize_last_day(initial)  # D2 触发
    assert pm.daily_pause_active is True
    # D3: update 跨日补算 D2(已触发) + update_peak 恢复 daily_pause
    pm.update(Decimal("93000"), date(2026, 7, 3), initial)
    assert pm.daily_pause_active is False  # update_peak 已恢复
