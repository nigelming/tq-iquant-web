from typing import Callable, Dict, List, Optional, Set
from decimal import Decimal

from .position import Position
from .event import SignalEvent, BarEvent
from .risk_manager import StrategyRiskManager
from .regime import REGIME_BULL
from tq_iquant_shared.constants import SignalType


class StrategyContext:
    def __init__(
        self,
        strategy_id: int,
        period: str,
        capital_ratio: Decimal,
        max_positions: int,
        *,
        single_open_ratio: Decimal = Decimal("0.1"),
        add_position_threshold: Decimal = Decimal("0.05"),
        max_add_count: int = 2,
        add_position_ratio: Decimal = Decimal("0.1"),
        reduce_position_ratio: Decimal = Decimal("0.3"),
        role: str = "independent",
        master_strategy_id: Optional[int] = None,
        stock_pool: Optional[Set[str]] = None,
        slave_exit_mode: str = "independent",
        # B4 OPEN 择优轮动（默认关）：满槽时强信号可轮动替换最弱持仓
        rotation_enabled: bool = False,
        rotation_momentum_bars: int = 20,
        rotation_hysteresis: Decimal = Decimal("0.05"),
        # B5 市场状态闸（默认关）：bull 态用 trend_* 覆盖参数，其余态用原参数
        regime_filter_enabled: bool = False,
        trend_trailing_stop_ratio: Optional[Decimal] = None,
        trend_max_positions: Optional[int] = None,
        # #3 策略级敞口上限（默认关）：BUY 审批加卡策略总持仓市值
        # ≤ capital_ratio × 组合初始资金（仅回测成交步传入生效）
        exposure_cap_enabled: bool = False,
    ):
        self.strategy_id = strategy_id
        self.period = period
        self.capital_ratio = capital_ratio
        self.max_positions = max_positions
        # 下单量参数（对应策略表字段，默认值同表默认）
        self.single_open_ratio = single_open_ratio
        self.add_position_threshold = add_position_threshold
        self.max_add_count = max_add_count
        self.add_position_ratio = add_position_ratio
        self.reduce_position_ratio = reduce_position_ratio
        # 主从角色（independent/master/slave）
        self.role = role
        self.master_strategy_id = master_strategy_id
        # 从策略卖出模式（仅 role=slave 时有意义）
        self.slave_exit_mode = slave_exit_mode
        # 本策略可交易股票池（组合的 stock_pool_id 对应成分；None=不限，回测单池/旧调用）
        self.stock_pool: Optional[Set[str]] = stock_pool
        # B4/B5 可选增强开关（全部默认关/None，关闭时行为与原逻辑一致）
        self.rotation_enabled = rotation_enabled
        self.rotation_momentum_bars = rotation_momentum_bars
        self.rotation_hysteresis = rotation_hysteresis
        self.regime_filter_enabled = regime_filter_enabled
        self.trend_trailing_stop_ratio = trend_trailing_stop_ratio
        self.trend_max_positions = trend_max_positions
        # #3 策略级敞口上限开关（默认关，关闭时审批行为与原逻辑一致）
        self.exposure_cap_enabled = exposure_cap_enabled
        self.positions: Dict[str, Position] = {}
        # 策略风控（assemble_portfolio 注入；未注入 → _check_risks 跳过并告警，不静默）
        self.strategy_risk: Optional[StrategyRiskManager] = None
        # 公式信号配置：[{"signal_name", "signal_type": SignalType, "trigger_value": int}]
        self.formula_signals: List[dict] = []

    def get_signal(
        self,
        bar: BarEvent,
        signal_cache: Optional[Dict] = None,
        tq_compute: Optional[Callable] = None,
    ) -> List[SignalEvent]:
        """取本策略在当前 bar 的公式信号。cache 优先，miss 调 tq_compute 兜底。

        cache key: (strategy_id, stock_code, bar_time)
        cache value: [{"name": str, "value": int}]
        按 formula_signals 配置（signal_name + trigger_value）匹配，转 SignalEvent。
        """
        signal_cache = signal_cache if signal_cache is not None else {}
        signals: List[SignalEvent] = []
        for stock_code in bar.stocks:
            # 股票池过滤：非池内股票不产生公式信号（实盘多组合共享行情 bar，杜绝跨池下单）。
            # 风控信号不受限（_check_risks 走持仓，已有持仓必须可卖）。
            if self.stock_pool is not None and stock_code not in self.stock_pool:
                continue
            key = (self.strategy_id, stock_code, bar.bar_time)
            if key in signal_cache:
                outputs = signal_cache[key]
            elif tq_compute is not None:
                outputs = tq_compute(stock_code, self.period, bar)
                signal_cache[key] = outputs
            else:
                outputs = []
            signals.extend(self._match_signals(outputs, stock_code, bar.bar_time))
        return signals

    def _match_signals(
        self, outputs: List[dict], stock_code: str, bar_time
    ) -> List[SignalEvent]:
        """公式输出按 formula_signals 配置匹配 trigger_value，转 SignalEvent。"""
        result: List[SignalEvent] = []
        # 公式输出名 → 值
        output_map = {o["name"]: o["value"] for o in outputs}
        for cfg in self.formula_signals:
            name = cfg["signal_name"]
            if name not in output_map:
                continue
            if output_map[name] != cfg["trigger_value"]:
                continue
            result.append(SignalEvent(
                strategy_id=self.strategy_id,
                stock_code=stock_code,
                signal_name=name,
                signal_type=cfg["signal_type"],
                bar_time=bar_time,
            ))
        return result

    def effective_max_positions(self, regime: Optional[str]) -> int:
        """当前市场状态下的生效槽位上限。仅在开关打开、状态为 bull 且配置了
        trend_max_positions 时用覆盖值，其余一律回落 max_positions（默认行为不变）。"""
        if (
            self.regime_filter_enabled
            and regime == REGIME_BULL
            and self.trend_max_positions is not None
        ):
            return self.trend_max_positions
        return self.max_positions

    def effective_trailing_stop(self, regime: Optional[str]) -> Optional[Decimal]:
        """当前市场状态下的生效移动止损比例。基准值取 strategy_risk 的
        trailing_stop_ratio；仅在开关打开、状态为 bull 且配置了覆盖值时替换。
        strategy_risk 未注入时返回 None（调用方按未配置移动止损处理）。"""
        base = (
            self.strategy_risk.trailing_stop_ratio
            if self.strategy_risk is not None
            else None
        )
        if (
            self.regime_filter_enabled
            and regime == REGIME_BULL
            and self.trend_trailing_stop_ratio is not None
        ):
            return self.trend_trailing_stop_ratio
        return base
