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
    initial=100000, limit=5% → 日内亏 6000(6%) 触发。"""
    pm = _pm(daily_loss_limit=Decimal("0.05"))
    initial = Decimal("100000")

    pm.update(Decimal("100000"), date(2026, 7, 1), initial)  # 首日基线
    pm.update(Decimal("94000"), date(2026, 7, 2), initial)   # 日内亏 6000=6% ≥ 5%
    assert pm.daily_pause_active is True
    assert pm.daily_loss_trigger_date == date(2026, 7, 2)

    # 次日 update → date(7/3) > trigger(7/2) → 恢复
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
