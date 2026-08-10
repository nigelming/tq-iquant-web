from decimal import Decimal
from datetime import date
from typing import Optional

from .position import Position


class StrategyRiskManager:
    def __init__(
        self,
        stop_loss_ratio: Decimal,
        take_profit_ratio: Decimal,
        trailing_stop_ratio: Decimal,
    ):
        self.stop_loss_ratio = stop_loss_ratio
        self.take_profit_ratio = take_profit_ratio
        self.trailing_stop_ratio = trailing_stop_ratio

    def check_stop_loss(self, position: Position, current_price: Decimal) -> bool:
        if position.quantity == 0:
            return False
        loss = (position.avg_cost - current_price) / position.avg_cost
        return loss >= self.stop_loss_ratio

    def check_take_profit(self, position: Position, current_price: Decimal) -> bool:
        if position.quantity == 0:
            return False
        profit = (current_price - position.avg_cost) / position.avg_cost
        return profit >= self.take_profit_ratio

    def check_trailing_stop(self, position: Position, current_price: Decimal) -> bool:
        if position.quantity == 0 or self.trailing_stop_ratio == 0:
            return False
        if current_price > position.highest_price:
            return False
        drawdown = (position.highest_price - current_price) / position.highest_price
        return drawdown >= self.trailing_stop_ratio


class PortfolioRiskManager:
    def __init__(self, max_drawdown: Decimal, daily_loss_limit: Decimal):
        self.max_drawdown = max_drawdown
        self.daily_loss_limit = daily_loss_limit
        self.consecutive_drawdown_triggers = 0
        self.circuit_breaker_active = False
        # 熔断接线新增状态（AGENTS.md §88）
        self.peak_value = Decimal("0")
        self.prev_close_value: Optional[Decimal] = None  # 上一日收盘总市值，算日内盈亏
        self.manual_recovery = False  # 累计触发 3 次后转手动
        self.breaker_trigger_date: Optional[date] = None
        self.daily_pause_active = False
        self.daily_loss_trigger_date: Optional[date] = None
        # 分钟级实盘（E5/E6）：跨日检测记录上一根 bar 的日期与 total_value
        self._last_bar_date: Optional[date] = None
        self._last_bar_total_value: Optional[Decimal] = None

    def check_max_drawdown(self, current_value: Decimal, peak_value: Decimal) -> bool:
        if peak_value == 0:
            return False
        drawdown = (peak_value - current_value) / peak_value
        return drawdown >= self.max_drawdown

    def check_daily_loss(self, daily_pnl: Decimal, initial_value: Decimal) -> bool:
        if initial_value == 0:
            return False
        loss = abs(daily_pnl) / initial_value if daily_pnl < 0 else 0
        return loss >= self.daily_loss_limit

    def daily_reset(self) -> None:
        """旧接口保留：清日级暂停。"""
        self.daily_pause_active = False

    def is_trading_halted(self) -> bool:
        """是否暂停新开仓（熔断或日内亏损暂停任一为真）。SELL 类不受影响。"""
        return self.circuit_breaker_active or self.daily_pause_active

    def update(self, total_value: Decimal, current_date: date, initial_capital: Decimal) -> None:
        """每根 bar 日终调用（回测）：= update_peak + update_daily 合集，向后兼容。

        日线回测每 bar 跨日：update_peak 刷新 prev_close = 上一 bar total_value，
        update_daily 用其算 daily_pnl = 今日-昨日，语义与旧 update 完全一致。
        """
        self.update_peak(total_value, current_date)
        self.update_daily(total_value, current_date, initial_capital)

    def update_peak(self, total_value: Decimal, current_date: date) -> None:
        """每根 bar 调用（实盘分钟级）：跨日刷新 prev_close、更新峰值、检测 max_drawdown 熔断 + 次日恢复。

        跨日检测：current_date 与上一根 bar 日期不同 → prev_close_value 刷新为
        上一根 bar 的 total_value（= 昨日最后一根 bar 总市值 = 昨日收盘基准）。
        分钟级每 bar 调：prev_close 只在跨日时刷新，不被分钟抖动覆盖（E6）。
        """
        # 1. 跨日刷新 prev_close（分钟级：prev_close = 昨日最后一根 bar 的 total_value）
        if self._last_bar_date is not None and self._last_bar_date != current_date:
            self.prev_close_value = self._last_bar_total_value
        self._last_bar_date = current_date
        self._last_bar_total_value = total_value

        # 2. 次日恢复：trigger 日的下一日起恢复（除非已转手动）
        if self.circuit_breaker_active and not self.manual_recovery:
            if self.breaker_trigger_date is not None and current_date > self.breaker_trigger_date:
                self.circuit_breaker_active = False

        # 3. 更新峰值
        if total_value > self.peak_value:
            self.peak_value = total_value

        # 4. 检测 max_drawdown（未熔断且非手动时）
        if not self.circuit_breaker_active and not self.manual_recovery:
            if self.check_max_drawdown(total_value, self.peak_value):
                self.circuit_breaker_active = True
                self.breaker_trigger_date = current_date
                self.consecutive_drawdown_triggers += 1
                if self.consecutive_drawdown_triggers >= 3:
                    self.manual_recovery = True

    def update_daily(self, total_value: Decimal, current_date: date, initial_capital: Decimal) -> None:
        """日终一次调用（实盘 14:30）：算日内盈亏、检测 daily_loss 暂停 + 次日恢复。

        日内盈亏 daily_pnl = total_value - prev_close_value（昨日收盘基准，由 update_peak 跨日刷新）。
        分钟级不误触：daily_loss 只在此处算，不被每 bar 的 update_peak 抖动影响（E6）。
        """
        # 1. 次日恢复
        if self.daily_pause_active:
            if self.daily_loss_trigger_date is not None and current_date > self.daily_loss_trigger_date:
                self.daily_pause_active = False

        # 2. 日内盈亏（首日无前值，pnl=0）
        if self.prev_close_value is None:
            daily_pnl = Decimal("0")
        else:
            daily_pnl = total_value - self.prev_close_value

        # 3. 检测 daily_loss（当日未暂停时）
        if not self.daily_pause_active:
            if self.check_daily_loss(daily_pnl, initial_capital):
                self.daily_pause_active = True
                self.daily_loss_trigger_date = current_date
